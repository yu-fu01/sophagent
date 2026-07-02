from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta
from typing import Any

from member_appt_cli import db

DT_FMT = "%Y-%m-%d %H:%M"
DATE_FMT = "%Y-%m-%d"


def print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def query_service_price_and_duration(conn, service_name: str) -> tuple[float, int]:
    row = conn.execute(
        "SELECT price, duration_min FROM catalog_services WHERE name=? AND status='active'",
        (service_name,),
    ).fetchone()
    if not row:
        return 0.0, 60
    return float(row["price"] or 0), int(row["duration_min"] or 60)


def query_service_materials(conn, service_name: str) -> dict[str, float]:
    svc = conn.execute(
        "SELECT id FROM catalog_services WHERE name=? AND status='active'",
        (service_name,),
    ).fetchone()
    if not svc:
        return {}
    mats = conn.execute(
        "SELECT product_name, qty FROM catalog_service_materials WHERE service_id=? ORDER BY product_name",
        (svc["id"],),
    ).fetchall()
    return {str(m["product_name"]): float(m["qty"]) for m in mats}


def staff_schedule_query(conn, staff_id: str, date_yyyy_mm_dd: str) -> dict:
    row = conn.execute(
        "SELECT status, slots_json FROM staff_schedules WHERE staff_id=? AND date=?",
        (staff_id, date_yyyy_mm_dd),
    ).fetchone()
    if not row:
        return {"ok": True, "staff_id": staff_id, "date": date_yyyy_mm_dd, "status": "work", "slots": None}
    try:
        slots = json.loads(row["slots_json"] or "null")
    except json.JSONDecodeError:
        slots = None
    return {"ok": True, "staff_id": staff_id, "date": date_yyyy_mm_dd, "status": row["status"], "slots": slots}


def staff_list_for_skill(conn, skill_keyword: str) -> list[dict]:
    needle = skill_keyword or ""
    out: list[dict] = []
    for r in conn.execute("SELECT * FROM staff_staff WHERE status='active' ORDER BY id").fetchall():
        d = dict(r)
        try:
            fj = json.loads(d.get("fields_json") or "{}")
        except json.JSONDecodeError:
            fj = {}
        blob = json.dumps(fj, ensure_ascii=False)
        if needle in blob or needle in (d.get("name") or ""):
            d["fields"] = fj
            out.append(d)
    return out


def staff_performance_add(conn, staff_id: str, service_name: str, amount: float, appt_id: str, ymd: str) -> None:
    conn.execute(
        """INSERT INTO staff_performance (staff_id, ymd, kind, target_name, amount, source_appt_id)
           VALUES (?,?,?,?,?,?)""",
        (staff_id, ymd, "service", service_name or "", float(amount), appt_id),
    )


def inventory_stock_out(conn, product_key: str, quantity: float, appt_id: str) -> None:
    row = conn.execute("SELECT stock_qty FROM inv_items WHERE key=? AND status='active'", (product_key,)).fetchone()
    if not row:
        raise RuntimeError(f"inventory_item_not_found:{product_key}")
    before = float(row["stock_qty"] or 0)
    qty = float(quantity)
    if qty <= 0:
        return
    if qty > before:
        raise RuntimeError(f"insufficient_stock:{product_key}:{before}:{qty}")
    after = before - qty
    conn.execute(
        "UPDATE inv_items SET stock_qty=?, updated_at=datetime('now','localtime') WHERE key=?",
        (after, product_key),
    )
    conn.execute(
        """INSERT INTO inv_logs (item_key, direction, type, quantity, before_qty, after_qty, ref_appt_id, remark)
           VALUES (?,?,?,?,?,?,?,?)""",
        (product_key, "out", "service", qty, before, after, appt_id, f"service appt:{appt_id}"),
    )


def next_id(conn, prefix: str, table: str) -> str:
    rows = conn.execute(f"SELECT id FROM {table} WHERE id GLOB ?", (f"{prefix}[0-9]*",)).fetchall()
    max_n = 0
    for r in rows:
        m = re.match(rf"^{prefix}(\d+)$", r["id"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{prefix}{max_n + 1:03d}"


def parse_dt(s: str) -> datetime:
    return datetime.strptime(s.strip(), DT_FMT)


def within_slots(start_dt: str, slots: list | None) -> bool:
    if not slots:
        return True
    t = start_dt.split()[1][:5]
    for slot in slots:
        if "-" not in slot:
            continue
        a, b = slot.split("-", 1)
        a, b = a.strip()[:5], b.strip()[:5]
        if a <= t <= b:
            return True
    return False


def appt_interval(conn, technician_id: str, start_dt: str, duration_min: int, exclude_appt_id: str | None = None):
    s = parse_dt(start_dt)
    e = s + timedelta(minutes=duration_min)
    q = """SELECT id, start_dt, duration_min FROM member_appointments
           WHERE technician_id=? AND status='scheduled'"""
    params: list[Any] = [technician_id]
    if exclude_appt_id:
        q += " AND id!=?"
        params.append(exclude_appt_id)
    for row in conn.execute(q, params):
        os = parse_dt(row["start_dt"])
        oe = os + timedelta(minutes=int(row["duration_min"]))
        if s < oe and os < e:
            return row["id"]
    return None


def cmd_member_add(conn, args: argparse.Namespace) -> int:
    mid = args.member_id or next_id(conn, "M", "member_members")
    conn.execute(
        """INSERT INTO member_members (id, name, phone, birthday, skin_type)
           VALUES (?,?,?,?,?)""",
        (mid, args.name, args.phone or "", args.birthday or "", args.skin_type or ""),
    )
    conn.execute("INSERT INTO member_wallet (member_id, balance) VALUES (?,0)", (mid,))
    conn.execute(
        """INSERT INTO member_consumption_log (member_id, visit_count) VALUES (?,0)
           ON CONFLICT(member_id) DO NOTHING""",
        (mid,),
    )
    conn.commit()
    print_json({"ok": True, "member_id": mid})
    return 0


def cmd_member_query(conn, args: argparse.Namespace) -> int:
    cur = conn.cursor()
    limit = args.limit or 500
    if args.member_id:
        row = cur.execute(
            "SELECT * FROM member_members WHERE id=? AND status='active'",
            (args.member_id,),
        ).fetchone()
        rows = [dict(row)] if row else []
    elif args.keyword:
        kw = f"%{args.keyword}%"
        rows = [
            dict(r)
            for r in cur.execute(
                """SELECT * FROM member_members WHERE status='active'
                   AND (name LIKE ? OR phone LIKE ?) LIMIT ?""",
                (kw, kw, limit),
            )
        ]
    elif args.birthday_month is not None:
        m = int(args.birthday_month)
        rows = [
            dict(r)
            for r in cur.execute(
                """SELECT * FROM member_members WHERE status='active'
                   AND birthday IS NOT NULL AND birthday != ''
                   AND CAST(substr(birthday, 6, 2) AS INTEGER)=?
                   LIMIT ?""",
                (m, limit),
            )
        ]
    else:
        rows = [
            dict(r)
            for r in cur.execute(
                "SELECT * FROM member_members WHERE status='active' ORDER BY id LIMIT ?",
                (limit,),
            )
        ]
    if args.with_wallet:
        for r in rows:
            w = cur.execute(
                "SELECT balance, total_recharge, total_consume FROM member_wallet WHERE member_id=?",
                (r["id"],),
            ).fetchone()
            r["wallet"] = dict(w) if w else {"balance": 0, "total_recharge": 0, "total_consume": 0}
    if getattr(args, "with_profile", False):
        for r in rows:
            mid = r["id"]
            c = cur.execute(
                "SELECT last_visit_at, fav_service, visit_count FROM member_consumption_log WHERE member_id=?",
                (mid,),
            ).fetchone()
            r["profile"] = dict(c) if c else {"last_visit_at": None, "fav_service": None, "visit_count": 0}
    print_json({"members": rows})
    return 0


def cmd_member_update(conn, args: argparse.Namespace) -> int:
    payload = json.loads(args.set)
    row = conn.execute(
        "SELECT id FROM member_members WHERE id=? AND status='active'", (args.member_id,)
    ).fetchone()
    if not row:
        print_json({"ok": False, "error": "not_found"})
        return 1
    sets = []
    vals: list[Any] = []
    simple = {"name", "phone", "birthday", "skin_type"}
    for k in simple:
        if k in payload:
            sets.append(f"{k}=?")
            vals.append(payload[k])
    if "tags" in payload:
        sets.append("tags_json=?")
        vals.append(json.dumps(payload["tags"], ensure_ascii=False))
    if "tags_json" in payload:
        v = payload["tags_json"]
        sets.append("tags_json=?")
        vals.append(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
    if "fields" in payload:
        sets.append("fields_json=?")
        vals.append(json.dumps(payload["fields"], ensure_ascii=False))
    if "fields_json" in payload:
        v = payload["fields_json"]
        sets.append("fields_json=?")
        vals.append(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
    if sets:
        sets.append("updated_at=datetime('now','localtime')")
        vals.append(args.member_id)
        conn.execute(f"UPDATE member_members SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_member_delete(conn, args: argparse.Namespace) -> int:
    if not args.yes:
        print_json({"ok": False, "error": "need_confirm", "hint": "--yes"})
        return 1
    conn.execute(
        "UPDATE member_members SET status='deleted', updated_at=datetime('now','localtime') WHERE id=?",
        (args.member_id,),
    )
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_balance_recharge(conn, args: argparse.Namespace) -> int:
    bonus = float(args.bonus or 0)
    amt = float(args.amount)
    total_credit = amt + bonus
    conn.execute(
        """UPDATE member_wallet SET balance=balance+?, total_recharge=total_recharge+?,
           updated_at=datetime('now','localtime') WHERE member_id=?""",
        (total_credit, amt, args.member_id),
    )
    conn.execute(
        """INSERT INTO member_transactions (member_id, type, amount, meta_json)
           VALUES (?,?,?,?)""",
        (args.member_id, "recharge", amt, json.dumps({"bonus": bonus}, ensure_ascii=False)),
    )
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_balance_query(conn, args: argparse.Namespace) -> int:
    row = conn.execute("SELECT * FROM member_wallet WHERE member_id=?", (args.member_id,)).fetchone()
    print_json({"ok": True, "wallet": dict(row) if row else None})
    return 0


def cmd_balance_consume(conn, args: argparse.Namespace) -> int:
    amt = float(args.amount)
    row = conn.execute("SELECT balance FROM member_wallet WHERE member_id=?", (args.member_id,)).fetchone()
    if not row or row["balance"] < amt:
        print_json({"ok": False, "error": "insufficient_balance"})
        return 1
    conn.execute(
        """UPDATE member_wallet SET balance=balance-?, total_consume=total_consume+?,
           updated_at=datetime('now','localtime') WHERE member_id=?""",
        (amt, amt, args.member_id),
    )
    conn.execute(
        """INSERT INTO member_transactions (member_id, type, amount, technician_id, meta_json)
           VALUES (?,?,?,?,?)""",
        (
            args.member_id,
            "consume",
            amt,
            args.technician or "",
            json.dumps({"project": args.project}, ensure_ascii=False),
        ),
    )
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_card_create(conn, args: argparse.Namespace) -> int:
    cid = args.card_id or next_id(conn, "C", "member_cards")
    conn.execute(
        """INSERT INTO member_cards (id, member_id, name, service_name, total_times, used_times, price, expire_date)
           VALUES (?,?,?,?,?,0,?,?)""",
        (
            cid,
            args.member_id,
            args.name,
            args.service or None,
            int(args.times),
            float(args.price),
            args.expire or None,
        ),
    )
    conn.commit()
    print_json({"ok": True, "card_id": cid})
    return 0


def cmd_card_query(conn, args: argparse.Namespace) -> int:
    cur = conn.cursor()
    if args.card:
        rows = [
            dict(r)
            for r in cur.execute(
                """SELECT * FROM member_cards WHERE member_id=? AND name=? AND status='active'""",
                (args.member_id, args.card),
            )
        ]
    elif args.service:
        rows = [
            dict(r)
            for r in cur.execute(
                """SELECT * FROM member_cards WHERE member_id=? AND status='active'
                   AND (service_name=? OR instr(name,?)>0)""",
                (args.member_id, args.service, args.service),
            )
        ]
    else:
        rows = [
            dict(r)
            for r in cur.execute(
                "SELECT * FROM member_cards WHERE member_id=? AND status='active'", (args.member_id,)
            )
        ]
    print_json({"cards": rows})
    return 0


def cmd_card_use(conn, args: argparse.Namespace) -> int:
    today = datetime.now().strftime(DATE_FMT)
    row = conn.execute(
        """SELECT * FROM member_cards WHERE member_id=? AND name=? AND status='active'""",
        (args.member_id, args.card),
    ).fetchone()
    if not row:
        print_json({"ok": False, "error": "card_not_found"})
        return 1
    if row["expire_date"] and row["expire_date"] < today:
        print_json({"ok": False, "error": "card_expired"})
        return 1
    if row["used_times"] >= row["total_times"]:
        print_json({"ok": False, "error": "no_times_left"})
        return 1
    conn.execute(
        "UPDATE member_cards SET used_times=used_times+1 WHERE id=?",
        (row["id"],),
    )
    conn.execute(
        """INSERT INTO member_transactions (member_id, type, amount, technician_id, meta_json)
           VALUES (?,?,?,?,?)""",
        (
            args.member_id,
            "card_use",
            0,
            args.technician or "",
            json.dumps({"card": args.card}, ensure_ascii=False),
        ),
    )
    conn.commit()
    print_json({"ok": True, "remaining": row["total_times"] - row["used_times"] - 1})
    return 0


def cmd_card_expiring(conn, args: argparse.Namespace) -> int:
    days = int(args.days or 30)
    end = (datetime.now() + timedelta(days=days)).strftime(DATE_FMT)
    today = datetime.now().strftime(DATE_FMT)
    rows = [
        dict(r)
        for r in conn.execute(
            """SELECT * FROM member_cards WHERE status='active' AND expire_date IS NOT NULL
               AND expire_date >= ? AND expire_date <= ?
               AND used_times < total_times""",
            (today, end),
        )
    ]
    print_json({"cards": rows})
    return 0


def cmd_card_count_by_service(conn, args: argparse.Namespace) -> int:
    svc = args.service
    n = conn.execute(
        """SELECT COUNT(*) FROM member_cards WHERE status='active'
           AND used_times < total_times
           AND (expire_date IS NULL OR expire_date >= date('now'))
           AND (service_name=? OR (service_name IS NULL AND instr(name,?)>0))""",
        (svc, svc),
    ).fetchone()[0]
    out = {"count": int(n)}
    if getattr(args, "json", False):
        print_json(out)
    else:
        print_json(out)
    return 0


def validate_appt_create(conn, args: argparse.Namespace, duration_min: int) -> tuple[bool, str]:
    date_part = args.datetime[:10]
    sch = staff_schedule_query(conn, args.technician_id, date_part)
    if sch.get("status") == "off":
        return False, "technician_off"
    slots = sch.get("slots")
    if not within_slots(args.datetime, slots):
        return False, "outside_work_slots"
    clash = appt_interval(conn, args.technician_id, args.datetime, duration_min)
    if clash:
        return False, f"schedule_conflict:{clash}"
    return True, ""


def cmd_appt_create(conn, args: argparse.Namespace) -> int:
    price, dur = query_service_price_and_duration(conn, args.project)
    duration_min = int(args.duration) if args.duration else dur
    ok, err = validate_appt_create(conn, args, duration_min)
    if not ok:
        print_json({"ok": False, "error": err})
        return 1
    aid = args.appt_id or next_id(conn, "A", "member_appointments")
    conn.execute(
        """INSERT INTO member_appointments (id, member_id, service, technician_id, start_dt, duration_min, status)
           VALUES (?,?,?,?,?,?, 'scheduled')""",
        (aid, args.member_id, args.project, args.technician_id, args.datetime, duration_min),
    )
    conn.commit()
    print_json({"ok": True, "appt_id": aid, "duration_min": duration_min})
    return 0


def cmd_appt_query(conn, args: argparse.Namespace) -> int:
    cur = conn.cursor()
    if args.appt_id:
        row = cur.execute("SELECT * FROM member_appointments WHERE id=?", (args.appt_id,)).fetchone()
        print_json({"appointments": [dict(row)] if row else []})
        return 0
    status = args.status or None
    if args.technician_id and args.date:
        if status:
            rows = [
                dict(r)
                for r in cur.execute(
                    """SELECT * FROM member_appointments WHERE technician_id=? AND substr(start_dt,1,10)=?
                       AND status=? ORDER BY start_dt""",
                    (args.technician_id, args.date, status),
                )
            ]
        else:
            rows = [
                dict(r)
                for r in cur.execute(
                    """SELECT * FROM member_appointments WHERE technician_id=? AND substr(start_dt,1,10)=?
                       ORDER BY start_dt""",
                    (args.technician_id, args.date),
                )
            ]
    elif args.date:
        q = "SELECT * FROM member_appointments WHERE substr(start_dt,1,10)=?"
        params: list[Any] = [args.date]
        if status:
            q += " AND status=?"
            params.append(status)
        q += " ORDER BY start_dt"
        rows = [dict(r) for r in cur.execute(q, params)]
    else:
        rows = [dict(r) for r in cur.execute("SELECT * FROM member_appointments ORDER BY start_dt DESC LIMIT 200")]
    print_json({"appointments": rows})
    return 0


def cmd_appt_update(conn, args: argparse.Namespace) -> int:
    row = conn.execute("SELECT * FROM member_appointments WHERE id=?", (args.appt_id,)).fetchone()
    if not row or row["status"] != "scheduled":
        print_json({"ok": False, "error": "invalid_appt"})
        return 1
    new_dt = args.datetime
    duration_min = int(row["duration_min"])
    clash = appt_interval(conn, row["technician_id"], new_dt, duration_min, exclude_appt_id=args.appt_id)
    if clash:
        print_json({"ok": False, "error": f"schedule_conflict:{clash}"})
        return 1
    sch = staff_schedule_query(conn, row["technician_id"], new_dt[:10])
    if sch.get("status") == "off":
        print_json({"ok": False, "error": "technician_off"})
        return 1
    if not within_slots(new_dt, sch.get("slots")):
        print_json({"ok": False, "error": "outside_work_slots"})
        return 1
    conn.execute(
        "UPDATE member_appointments SET start_dt=?, updated_at=datetime('now','localtime') WHERE id=?",
        (new_dt, args.appt_id),
    )
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_appt_cancel(conn, args: argparse.Namespace) -> int:
    if not args.yes:
        print_json({"ok": False, "error": "need_confirm"})
        return 1
    conn.execute(
        "UPDATE member_appointments SET status='cancelled', updated_at=datetime('now','localtime') WHERE id=?",
        (args.appt_id,),
    )
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_appt_complete(conn, args: argparse.Namespace) -> int:
    row = conn.execute("SELECT * FROM member_appointments WHERE id=?", (args.appt_id,)).fetchone()
    if not row or row["status"] != "scheduled":
        print_json({"ok": False, "error": "invalid_appt"})
        return 1
    member_id = row["member_id"]
    service = row["service"]
    tech = row["technician_id"]
    pay = args.pay or "stored_value"
    price, _ = query_service_price_and_duration(conn, service)
    today = datetime.now().strftime(DATE_FMT)

    try:
        with conn:
            if pay == "card":
                card_name = args.card_name
                if not card_name:
                    raise RuntimeError("need_card_name")
                cr = conn.execute(
                    "SELECT * FROM member_cards WHERE member_id=? AND name=? AND status='active'",
                    (member_id, card_name),
                ).fetchone()
                if not cr:
                    raise RuntimeError("card_not_found")
                if cr["expire_date"] and cr["expire_date"] < today:
                    raise RuntimeError("card_expired")
                if cr["used_times"] >= cr["total_times"]:
                    raise RuntimeError("no_times_left")
                conn.execute("UPDATE member_cards SET used_times=used_times+1 WHERE id=?", (cr["id"],))
                conn.execute(
                    """INSERT INTO member_transactions (member_id, type, amount, ref_appt_id, technician_id, meta_json)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        member_id,
                        "card_use",
                        0,
                        args.appt_id,
                        tech,
                        json.dumps({"card": card_name, "service": service}, ensure_ascii=False),
                    ),
                )
            elif pay == "stored_value":
                w = conn.execute("SELECT balance FROM member_wallet WHERE member_id=?", (member_id,)).fetchone()
                bal = float(w["balance"]) if w else 0
                if bal < price:
                    raise RuntimeError("insufficient_balance")
                conn.execute(
                    """UPDATE member_wallet SET balance=balance-?, total_consume=total_consume+?,
                       updated_at=datetime('now','localtime') WHERE member_id=?""",
                    (price, price, member_id),
                )
                conn.execute(
                    """INSERT INTO member_transactions (member_id, type, amount, ref_appt_id, technician_id, meta_json)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        member_id,
                        "consume",
                        price,
                        args.appt_id,
                        tech,
                        json.dumps({"project": service}, ensure_ascii=False),
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO member_transactions (member_id, type, amount, ref_appt_id, technician_id, meta_json)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        member_id,
                        "cash",
                        price,
                        args.appt_id,
                        tech,
                        json.dumps({"project": service}, ensure_ascii=False),
                    ),
                )

            conn.execute(
                "UPDATE member_appointments SET status='completed', updated_at=datetime('now','localtime') WHERE id=?",
                (args.appt_id,),
            )
            conn.execute(
                """INSERT INTO member_consumption_log (member_id, last_visit_at, fav_service, visit_count, updated_at)
                   VALUES (?,?,?,?,datetime('now','localtime'))
                   ON CONFLICT(member_id) DO UPDATE SET
                     last_visit_at=excluded.last_visit_at,
                     fav_service=excluded.fav_service,
                     visit_count=member_consumption_log.visit_count+1,
                     updated_at=datetime('now','localtime')""",
                (member_id, today, service, 1),
            )

            mats = query_service_materials(conn, service)
            for pname, qty in mats.items():
                inventory_stock_out(conn, pname, qty, args.appt_id)

            perf_ymd = (row["start_dt"] or "")[:10] or today
            staff_performance_add(conn, tech, service, price, args.appt_id, perf_ymd)

        print_json({"ok": True, "appt_id": args.appt_id, "payment": pay})
        return 0
    except Exception as e:
        print_json({"ok": False, "error": str(e), "appt_id": args.appt_id})
        return 1


def cmd_appt_count_by_staff(conn, args: argparse.Namespace) -> int:
    n = conn.execute(
        """SELECT COUNT(*) FROM member_appointments WHERE technician_id=? AND status=?""",
        (args.staff_id, args.status),
    ).fetchone()[0]
    out = {"count": int(n)}
    print_json(out)
    return 0


def cmd_tech_recommend(conn, args: argparse.Namespace) -> int:
    _, dur = query_service_price_and_duration(conn, args.project)
    candidates = staff_list_for_skill(conn, args.project)
    date_part = args.datetime[:10]
    good = []
    for st in candidates:
        sid = st["id"]
        sch = staff_schedule_query(conn, sid, date_part)
        if sch.get("status") == "off":
            continue
        if not within_slots(args.datetime, sch.get("slots")):
            continue
        clash = appt_interval(conn, sid, args.datetime, dur)
        if clash:
            continue
        good.append({"staff_id": sid, "name": st.get("name"), "fields": st.get("fields")})
    print_json({"ok": True, "technicians": good})
    return 0


def cmd_tech_auto_assign(conn, args: argparse.Namespace) -> int:
    _, dur = query_service_price_and_duration(conn, args.project)
    candidates = staff_list_for_skill(conn, args.project)
    date_part = args.datetime[:10]
    for st in candidates:
        sid = st["id"]
        sch = staff_schedule_query(conn, sid, date_part)
        if sch.get("status") == "off":
            continue
        if not within_slots(args.datetime, sch.get("slots")):
            continue
        clash = appt_interval(conn, sid, args.datetime, dur)
        if clash:
            continue
        print_json({"ok": True, "assigned": {"staff_id": sid, "name": st.get("name")}})
        return 0
    print_json({"ok": False, "error": "no_technician_available"})
    return 1


def cmd_insight_analyze(conn, args: argparse.Namespace) -> int:
    m = conn.execute("SELECT * FROM member_members WHERE id=?", (args.member_id,)).fetchone()
    c = conn.execute("SELECT * FROM member_consumption_log WHERE member_id=?", (args.member_id,)).fetchone()
    txs = [
        dict(r)
        for r in conn.execute(
            """SELECT type, amount, created_at, meta_json FROM member_transactions
               WHERE member_id=? ORDER BY id DESC LIMIT 20""",
            (args.member_id,),
        )
    ]
    print_json(
        {
            "ok": True,
            "member": dict(m) if m else None,
            "consumption": dict(c) if c else None,
            "recent_transactions": txs,
        }
    )
    return 0


def cmd_insight_birthday(conn, args: argparse.Namespace) -> int:
    args.birthday_month = args.month
    args.keyword = None
    args.member_id = None
    args.with_wallet = False
    args.limit = 500
    return cmd_member_query(conn, args)


def cmd_insight_low_balance(conn, args: argparse.Namespace) -> int:
    thr = float(args.threshold or 200)
    rows = [
        dict(r)
        for r in conn.execute(
            """SELECT m.*, w.balance FROM member_members m
               JOIN member_wallet w ON w.member_id=m.id
               WHERE m.status='active' AND w.balance < ? LIMIT 500""",
            (thr,),
        )
    ]
    print_json({"members": rows})
    return 0


def cmd_insight_due(conn, args: argparse.Namespace) -> int:
    args.days = args.days or 30
    return cmd_card_expiring(conn, args)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="美容院会员与预约 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("member-add")
    s.add_argument("--name", required=True)
    s.add_argument("--phone", default=None)
    s.add_argument("--birthday", default=None)
    s.add_argument("--skin-type", default=None)
    s.add_argument("--member-id", default=None)
    s.set_defaults(func=cmd_member_add)

    s = sub.add_parser("member-query")
    s.add_argument("--keyword", default=None)
    s.add_argument("--member-id", default=None)
    s.add_argument("--birthday-month", type=int, default=None)
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--with-wallet", action="store_true")
    s.add_argument("--with-profile", action="store_true", help="含 consumption_log 摘要（到店/偏好）")
    s.set_defaults(func=cmd_member_query)

    s = sub.add_parser("member-update")
    s.add_argument("--member-id", required=True)
    s.add_argument("--set", required=True)
    s.set_defaults(func=cmd_member_update)

    s = sub.add_parser("member-delete")
    s.add_argument("--member-id", required=True)
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_member_delete)

    s = sub.add_parser("balance-recharge")
    s.add_argument("--member-id", required=True)
    s.add_argument("--amount", type=float, required=True)
    s.add_argument("--bonus", type=float, default=0)
    s.set_defaults(func=cmd_balance_recharge)

    s = sub.add_parser("balance-query")
    s.add_argument("--member-id", required=True)
    s.set_defaults(func=cmd_balance_query)

    s = sub.add_parser("balance-consume")
    s.add_argument("--member-id", required=True)
    s.add_argument("--amount", type=float, required=True)
    s.add_argument("--project", default="")
    s.add_argument("--technician", default="")
    s.set_defaults(func=cmd_balance_consume)

    s = sub.add_parser("card-create")
    s.add_argument("--member-id", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--times", type=int, required=True)
    s.add_argument("--price", type=float, required=True)
    s.add_argument("--expire", default=None)
    s.add_argument("--service", default=None)
    s.add_argument("--card-id", default=None)
    s.set_defaults(func=cmd_card_create)

    s = sub.add_parser("card-query")
    s.add_argument("--member-id", required=True)
    s.add_argument("--card", default=None)
    s.add_argument("--service", default=None)
    s.set_defaults(func=cmd_card_query)

    s = sub.add_parser("card-use")
    s.add_argument("--member-id", required=True)
    s.add_argument("--card", required=True)
    s.add_argument("--technician", default="")
    s.set_defaults(func=cmd_card_use)

    s = sub.add_parser("card-expiring")
    s.add_argument("--days", type=int, default=30)
    s.set_defaults(func=cmd_card_expiring)

    s = sub.add_parser("card-count-by-service")
    s.add_argument("--service", required=True)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_card_count_by_service)

    s = sub.add_parser("appt-create")
    s.add_argument("--member-id", required=True)
    s.add_argument("--project", required=True)
    s.add_argument("--technician-id", required=True)
    s.add_argument("--datetime", required=True)
    s.add_argument("--duration", type=int, default=None)
    s.add_argument("--appt-id", default=None)
    s.set_defaults(func=cmd_appt_create)

    s = sub.add_parser("appt-query")
    s.add_argument("--date", default=None)
    s.add_argument("--technician-id", default=None)
    s.add_argument("--status", default=None)
    s.add_argument("--appt-id", default=None)
    s.set_defaults(func=cmd_appt_query)

    s = sub.add_parser("appt-update")
    s.add_argument("--appt-id", required=True)
    s.add_argument("--datetime", required=True)
    s.set_defaults(func=cmd_appt_update)

    s = sub.add_parser("appt-cancel")
    s.add_argument("--appt-id", required=True)
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_appt_cancel)

    s = sub.add_parser("appt-complete")
    s.add_argument("--appt-id", required=True)
    s.add_argument("--pay", default="stored_value", choices=["stored_value", "card", "cash"])
    s.add_argument("--card-name", default=None)
    s.set_defaults(func=cmd_appt_complete)

    s = sub.add_parser("appt-count-by-staff")
    s.add_argument("--staff-id", required=True)
    s.add_argument("--status", default="scheduled")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_appt_count_by_staff)

    s = sub.add_parser("tech-recommend")
    s.add_argument("--project", required=True)
    s.add_argument("--datetime", required=True)
    s.set_defaults(func=cmd_tech_recommend)

    s = sub.add_parser("tech-auto-assign")
    s.add_argument("--project", required=True)
    s.add_argument("--datetime", required=True)
    s.set_defaults(func=cmd_tech_auto_assign)

    s = sub.add_parser("insight-analyze")
    s.add_argument("--member-id", required=True)
    s.set_defaults(func=cmd_insight_analyze)

    s = sub.add_parser("insight-birthday")
    s.add_argument("--month", type=int, required=True)
    s.set_defaults(func=cmd_insight_birthday)

    s = sub.add_parser("insight-low-balance")
    s.add_argument("--threshold", type=float, default=200)
    s.set_defaults(func=cmd_insight_low_balance)

    s = sub.add_parser("insight-due")
    s.add_argument("--days", type=int, default=30)
    s.set_defaults(func=cmd_insight_due)

    return p


def main() -> int:
    args = build_parser().parse_args()
    conn = db.connect()
    try:
        return args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
