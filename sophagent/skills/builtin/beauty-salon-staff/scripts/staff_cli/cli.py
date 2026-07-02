from __future__ import annotations

import argparse
import json
import re
from calendar import monthrange
from datetime import date, datetime
from pathlib import Path
from typing import Any

from staff_cli import db


def skills_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent.parent


def member_pending_appts(conn, staff_id: str) -> int:
    n = conn.execute(
        "SELECT COUNT(*) FROM member_appointments WHERE technician_id=? AND status='scheduled'",
        (staff_id,),
    ).fetchone()[0]
    return int(n or 0)


def print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def next_staff_id(conn) -> str:
    rows = conn.execute("SELECT id FROM staff_staff WHERE id GLOB 'S[0-9]*'").fetchall()
    max_n = 0
    for r in rows:
        m = re.match(r"^S(\d+)$", r["id"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"S{max_n + 1:03d}"


def cmd_staff_add(conn, args: argparse.Namespace) -> int:
    sid = args.staff_id or next_staff_id(conn)
    fields = {}
    if args.fields:
        fields = json.loads(args.fields)
    conn.execute(
        """INSERT INTO staff_staff (id, name, phone, join_date, fields_json)
           VALUES (?,?,?,?,?)""",
        (sid, args.name, args.phone or "", args.join_date or "", json.dumps(fields, ensure_ascii=False)),
    )
    conn.commit()
    print_json({"ok": True, "staff_id": sid})
    return 0


def cmd_staff_query(conn, args: argparse.Namespace) -> int:
    cur = conn.cursor()
    if args.staff_id:
        row = cur.execute("SELECT * FROM staff_staff WHERE id=? AND status='active'", (args.staff_id,)).fetchone()
        rows = [dict(row)] if row else []
    elif args.name:
        row = cur.execute(
            "SELECT * FROM staff_staff WHERE name=? AND status='active'", (args.name,)
        ).fetchall()
        rows = [dict(r) for r in row]
    elif args.skill:
        needle = args.skill
        out = []
        for r in cur.execute("SELECT * FROM staff_staff WHERE status='active'"):
            d = dict(r)
            fj = json.loads(d.get("fields_json") or "{}")
            blob = json.dumps(fj, ensure_ascii=False)
            if needle in blob or needle in (d.get("name") or ""):
                out.append(d)
        rows = out
    else:
        rows = [dict(r) for r in cur.execute("SELECT * FROM staff_staff WHERE status='active' ORDER BY id")]
    for r in rows:
        r["fields"] = json.loads(r.pop("fields_json") or "{}")
    print_json({"staff": rows})
    return 0


def cmd_staff_update(conn, args: argparse.Namespace) -> int:
    payload = json.loads(args.set)
    row = conn.execute(
        "SELECT * FROM staff_staff WHERE id=? AND status='active'", (args.staff_id,)
    ).fetchone()
    if not row:
        print_json({"ok": False, "error": "not_found"})
        return 1
    d = dict(row)
    fields = json.loads(d["fields_json"] or "{}")
    if "phone" in payload:
        conn.execute(
            "UPDATE staff_staff SET phone=?, updated_at=datetime('now','localtime') WHERE id=?",
            (payload["phone"], args.staff_id),
        )
    if "name" in payload:
        conn.execute(
            "UPDATE staff_staff SET name=?, updated_at=datetime('now','localtime') WHERE id=?",
            (payload["name"], args.staff_id),
        )
    if "fields" in payload:
        fields.update(payload["fields"])
        conn.execute(
            "UPDATE staff_staff SET fields_json=?, updated_at=datetime('now','localtime') WHERE id=?",
            (json.dumps(fields, ensure_ascii=False), args.staff_id),
        )
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_staff_delete(conn, args: argparse.Namespace) -> int:
    row = conn.execute("SELECT id FROM staff_staff WHERE id=? AND status='active'", (args.staff_id,)).fetchone()
    if not row:
        print_json({"ok": False, "error": "not_found"})
        return 1
    cnt = member_pending_appts(conn, args.staff_id)
    if cnt and cnt > 0:
        print_json({"ok": False, "error": "has_pending_appointments", "count": cnt})
        return 1
    if not args.yes:
        print_json({"ok": False, "error": "need_confirm", "hint": "加 --yes"})
        return 1
    conn.execute(
        "UPDATE staff_staff SET status='deleted', updated_at=datetime('now','localtime') WHERE id=?",
        (args.staff_id,),
    )
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_staff_field_add(conn, args: argparse.Namespace) -> int:
    conn.execute(
        "INSERT OR REPLACE INTO staff_fields (name, type) VALUES (?,?)",
        (args.name, args.type or "text"),
    )
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_staff_field_query(conn, args: argparse.Namespace) -> int:
    rows = [dict(r) for r in conn.execute("SELECT * FROM staff_fields ORDER BY name")]
    print_json({"fields": rows})
    return 0


def cmd_staff_salary(conn, args: argparse.Namespace) -> int:
    if args.query:
        row = conn.execute(
            "SELECT id, name, base_salary FROM staff_staff WHERE id=? AND status='active'", (args.staff_id,)
        ).fetchone()
        if not row:
            print_json({"ok": False, "error": "not_found"})
            return 1
        print_json({"ok": True, "staff_id": row["id"], "name": row["name"], "base_salary": row["base_salary"]})
        return 0
    conn.execute(
        "UPDATE staff_staff SET base_salary=?, updated_at=datetime('now','localtime') WHERE id=? AND status='active'",
        (float(args.base_salary), args.staff_id),
    )
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_staff_commission(conn, args: argparse.Namespace) -> int:
    if args.query:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT kind, target_name, ratio FROM staff_commission_rules WHERE staff_id=? ORDER BY kind, target_name",
                (args.staff_id,),
            )
        ]
        print_json({"ok": True, "rules": rows})
        return 0
    if args.product_commission is not None:
        conn.execute("DELETE FROM staff_commission_rules WHERE staff_id=? AND kind='product'", (args.staff_id,))
        conn.execute(
            "INSERT INTO staff_commission_rules (staff_id, kind, target_name, ratio) VALUES (?,?,NULL,?)",
            (args.staff_id, "product", float(args.product_commission)),
        )
    if args.service_commission:
        payload = json.loads(args.service_commission)
        conn.execute(
            "DELETE FROM staff_commission_rules WHERE staff_id=? AND kind='service'", (args.staff_id,)
        )
        for svc, ratio in payload.items():
            conn.execute(
                "INSERT INTO staff_commission_rules (staff_id, kind, target_name, ratio) VALUES (?,?,?,?)",
                (args.staff_id, "service", str(svc), float(ratio)),
            )
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_staff_schedule(conn, args: argparse.Namespace) -> int:
    if args.query_all:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT staff_id, date, status, slots_json FROM staff_schedules WHERE date=? ORDER BY staff_id",
                (args.date,),
            )
        ]
        for r in rows:
            r["slots"] = json.loads(r.pop("slots_json") or "null")
        print_json({"schedules": rows})
        return 0
    if args.query:
        row = conn.execute(
            "SELECT * FROM staff_schedules WHERE staff_id=? AND date=?",
            (args.staff_id, args.date),
        ).fetchone()
        if not row:
            print_json(
                {
                    "ok": True,
                    "staff_id": args.staff_id,
                    "date": args.date,
                    "status": "work",
                    "slots": None,
                    "note": "default_full_day",
                }
            )
            return 0
        d = dict(row)
        d["slots"] = json.loads(d.pop("slots_json") or "null")
        print_json({"ok": True, **d})
        return 0
    slots_json = None
    if args.slots:
        slots_json = json.dumps(json.loads(args.slots), ensure_ascii=False)
    status = args.status or "work"
    conn.execute(
        """INSERT INTO staff_schedules (staff_id, date, status, slots_json)
           VALUES (?,?,?,?)
           ON CONFLICT(staff_id, date) DO UPDATE SET
           status=excluded.status,
           slots_json=excluded.slots_json""",
        (args.staff_id, args.date, status, slots_json),
    )
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_staff_performance(conn, args: argparse.Namespace) -> int:
    month = args.month or datetime.now().strftime("%Y-%m")
    y1, m1 = map(int, month.split("-"))
    start = f"{month}-01"
    last = monthrange(y1, m1)[1]
    end = f"{month}-{last:02d}"
    rows = [
        dict(r)
        for r in conn.execute(
            """SELECT * FROM staff_performance WHERE staff_id=? AND ymd >= ? AND ymd <= ? ORDER BY ymd, id""",
            (args.staff_id, start, end),
        )
    ]
    print_json({"ok": True, "performances": rows})
    return 0


def cmd_staff_performance_add(conn, args: argparse.Namespace) -> int:
    ymd = args.ymd or datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        """INSERT INTO staff_performance (staff_id, ymd, kind, target_name, amount, source_appt_id)
           VALUES (?,?,?,?,?,?)""",
        (
            args.staff_id,
            ymd,
            args.kind or "service",
            args.service or args.target or "",
            float(args.amount),
            args.appt_id or None,
        ),
    )
    conn.commit()
    print_json({"ok": True})
    return 0


def ratio_for_service(conn, staff_id: str, service_name: str) -> float:
    row = conn.execute(
        """SELECT ratio FROM staff_commission_rules
           WHERE staff_id=? AND kind='service' AND target_name=?""",
        (staff_id, service_name),
    ).fetchone()
    return float(row["ratio"]) if row else 0.0


def ratio_for_product(conn, staff_id: str) -> float:
    row = conn.execute(
        """SELECT ratio FROM staff_commission_rules
           WHERE staff_id=? AND kind='product' AND target_name IS NULL""",
        (staff_id,),
    ).fetchone()
    return float(row["ratio"]) if row else 0.0


def cmd_staff_salary_calc(conn, args: argparse.Namespace) -> int:
    month = args.month or datetime.now().strftime("%Y-%m")
    staff = conn.execute(
        "SELECT id, name, base_salary FROM staff_staff WHERE id=? AND status='active'", (args.staff_id,)
    ).fetchone()
    if not staff:
        print_json({"ok": False, "error": "not_found"})
        return 1
    y1, m1 = map(int, month.split("-"))
    start = f"{month}-01"
    last = monthrange(y1, m1)[1]
    end = f"{month}-{last:02d}"
    perfs = conn.execute(
        """SELECT kind, target_name, amount FROM staff_performance
           WHERE staff_id=? AND ymd >= ? AND ymd <= ?""",
        (args.staff_id, start, end),
    ).fetchall()
    base = float(staff["base_salary"])
    commission_detail = []
    commission_total = 0.0
    pr_ratio = ratio_for_product(conn, args.staff_id)
    for p in perfs:
        kind = p["kind"]
        amt = float(p["amount"])
        if kind == "service":
            r = ratio_for_service(conn, args.staff_id, p["target_name"] or "")
            c = amt * r
            commission_detail.append(
                {"kind": "service", "target": p["target_name"], "amount": amt, "ratio": r, "commission": c}
            )
            commission_total += c
        elif kind == "product":
            c = amt * pr_ratio
            commission_detail.append(
                {"kind": "product", "target": p["target_name"], "amount": amt, "ratio": pr_ratio, "commission": c}
            )
            commission_total += c
    total = base + commission_total
    print_json(
        {
            "ok": True,
            "staff_id": staff["id"],
            "name": staff["name"],
            "month": month,
            "base_salary": base,
            "commission_total": round(commission_total, 2),
            "total_salary": round(total, 2),
            "detail": commission_detail,
        }
    )
    return 0


def cmd_staff_ranking(conn, args: argparse.Namespace) -> int:
    month = args.month or datetime.now().strftime("%Y-%m")
    y1, m1 = map(int, month.split("-"))
    start = f"{month}-01"
    last = monthrange(y1, m1)[1]
    end = f"{month}-{last:02d}"
    rows = conn.execute(
        """
        SELECT s.id, s.name,
               COALESCE(SUM(p.amount), 0) AS revenue
        FROM staff_staff s
        LEFT JOIN staff_performance p ON p.staff_id = s.id AND p.ymd >= ? AND p.ymd <= ?
        WHERE s.status = 'active'
        GROUP BY s.id
        ORDER BY revenue DESC
        """,
        (start, end),
    ).fetchall()
    out = [{"rank": i + 1, "staff_id": r["id"], "name": r["name"], "revenue": float(r["revenue"])} for i, r in enumerate(rows)]
    print_json({"ok": True, "month": month, "ranking": out})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="美容院员工 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("staff-add")
    s.add_argument("--name", required=True)
    s.add_argument("--phone", default=None)
    s.add_argument("--join-date", default=None)
    s.add_argument("--staff-id", default=None)
    s.add_argument("--fields", default=None, help="JSON 自定义属性")
    s.set_defaults(func=cmd_staff_add)

    s = sub.add_parser("staff-query")
    s.add_argument("--staff-id", default=None)
    s.add_argument("--name", default=None)
    s.add_argument("--skill", default=None, help="匹配 fields / 姓名中包含的项目关键词")
    s.set_defaults(func=cmd_staff_query)

    s = sub.add_parser("staff-update")
    s.add_argument("--staff-id", required=True)
    s.add_argument("--set", required=True, help="JSON: phone / name / fields")
    s.set_defaults(func=cmd_staff_update)

    s = sub.add_parser("staff-delete")
    s.add_argument("--staff-id", required=True)
    s.add_argument("--yes", action="store_true")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_staff_delete)

    s = sub.add_parser("staff-field-add")
    s.add_argument("--name", required=True)
    s.add_argument("--type", default="text")
    s.set_defaults(func=cmd_staff_field_add)

    s = sub.add_parser("staff-field-query")
    s.set_defaults(func=cmd_staff_field_query)

    s = sub.add_parser("staff-salary")
    s.add_argument("--staff-id", required=True)
    s.add_argument("--base-salary", type=float, default=None)
    s.add_argument("--query", action="store_true")
    s.set_defaults(func=cmd_staff_salary)

    s = sub.add_parser("staff-commission")
    s.add_argument("--staff-id", required=True)
    s.add_argument("--service-commission", default=None, help='JSON，项目名→比例，如 {"面部护理":0.15}')
    s.add_argument("--product-commission", type=float, default=None)
    s.add_argument("--query", action="store_true")
    s.set_defaults(func=cmd_staff_commission)

    s = sub.add_parser("staff-schedule")
    s.add_argument("--staff-id", default=None)
    s.add_argument("--date", required=True)
    s.add_argument("--status", default=None, help="off / work")
    s.add_argument("--slots", default=None, help='JSON 数组，如 ["09:00-12:00"]')
    s.add_argument("--query", action="store_true")
    s.add_argument("--query-all", action="store_true")
    s.set_defaults(func=cmd_staff_schedule)

    s = sub.add_parser("staff-performance")
    s.add_argument("--staff-id", required=True)
    s.add_argument("--month", default=None, help="YYYY-MM")
    s.set_defaults(func=cmd_staff_performance)

    s = sub.add_parser("staff-performance-add")
    s.add_argument("--staff-id", required=True)
    s.add_argument("--amount", type=float, required=True)
    s.add_argument("--service", default=None)
    s.add_argument("--target", default=None)
    s.add_argument("--kind", default="service")
    s.add_argument("--appt-id", default=None)
    s.add_argument("--ymd", default=None)
    s.set_defaults(func=cmd_staff_performance_add)

    s = sub.add_parser("staff-salary-calc")
    s.add_argument("--staff-id", required=True)
    s.add_argument("--month", default=None)
    s.set_defaults(func=cmd_staff_salary_calc)

    s = sub.add_parser("staff-ranking")
    s.add_argument("--month", default=None)
    s.set_defaults(func=cmd_staff_ranking)

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
