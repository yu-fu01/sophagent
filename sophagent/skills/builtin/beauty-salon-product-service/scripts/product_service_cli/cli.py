from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from product_service_cli import db

def skills_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent.parent


def member_card_count_by_service(conn, service_name: str) -> int:
    svc = service_name
    n = conn.execute(
        """SELECT COUNT(*) FROM member_cards WHERE status='active'
           AND used_times < total_times
           AND (expire_date IS NULL OR expire_date = '' OR expire_date >= date('now'))
           AND (service_name=? OR (service_name IS NULL AND instr(name,?)>0))""",
        (svc, svc),
    ).fetchone()[0]
    return int(n or 0)


def print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def service_display_id_map(conn) -> dict[int, int]:
    rows = conn.execute(
        "SELECT id FROM catalog_services WHERE status='active' ORDER BY id"
    ).fetchall()
    return {int(row["id"]): idx for idx, row in enumerate(rows, start=1)}


def with_service_display_id(row: dict[str, Any], display_ids: dict[int, int]) -> dict[str, Any]:
    sid = int(row["id"])
    return {"display_id": display_ids.get(sid), **row}


def cmd_service_add(conn, args: argparse.Namespace) -> int:
    materials = {}
    if args.materials:
        materials = json.loads(args.materials)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO catalog_services (name, price, duration_min) VALUES (?,?,?)",
        (args.name, float(args.price), int(args.duration)),
    )
    sid = cur.lastrowid
    for pname, qty in materials.items():
        cur.execute(
            "INSERT INTO catalog_service_materials (service_id, product_name, qty) VALUES (?,?,?)",
            (sid, str(pname), float(qty)),
        )
    conn.commit()
    print_json({"ok": True, "id": sid, "name": args.name})
    return 0


def cmd_service_query(conn, args: argparse.Namespace) -> int:
    cur = conn.cursor()
    display_ids = service_display_id_map(conn)
    if args.name:
        row = cur.execute(
            "SELECT * FROM catalog_services WHERE name=? AND status='active'",
            (args.name,),
        ).fetchone()
        rows = [with_service_display_id(dict(row), display_ids)] if row else []
    else:
        rows = [
            with_service_display_id(dict(r), display_ids)
            for r in cur.execute(
                "SELECT * FROM catalog_services WHERE status='active' ORDER BY id"
            )
        ]
    for r in rows:
        sid = r["id"]
        mats = cur.execute(
            "SELECT product_name, qty FROM catalog_service_materials WHERE service_id=? ORDER BY product_name",
            (sid,),
        ).fetchall()
        r["materials"] = {m["product_name"]: m["qty"] for m in mats}
    print_json({"services": rows})
    return 0


def cmd_service_update(conn, args: argparse.Namespace) -> int:
    payload = json.loads(args.set)
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id FROM catalog_services WHERE name=? AND status='active'",
        (args.name,),
    ).fetchone()
    if not row:
        print_json({"ok": False, "error": "service_not_found"})
        return 1
    sid = row["id"]
    sets = []
    vals: list[Any] = []
    if "price" in payload:
        sets.append("price=?")
        vals.append(float(payload["price"]))
    if "duration" in payload or "duration_min" in payload:
        sets.append("duration_min=?")
        vals.append(int(payload.get("duration_min", payload.get("duration"))))
    if "name" in payload:
        sets.append("name=?")
        vals.append(str(payload["name"]))
    if sets:
        sets.append("updated_at=datetime('now','localtime')")
        vals.append(sid)
        cur.execute(f"UPDATE catalog_services SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_service_delete(conn, args: argparse.Namespace) -> int:
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id FROM catalog_services WHERE name=? AND status='active'",
        (args.name,),
    ).fetchone()
    if not row:
        print_json({"ok": False, "error": "service_not_found"})
        return 1
    cnt = member_card_count_by_service(conn, args.name)
    if cnt > 0:
        print_json({"ok": False, "error": "cards_reference_service", "count": cnt})
        return 1
    if not args.yes:
        print_json({"ok": False, "error": "need_confirm", "hint": "请加 --yes 确认删除"})
        return 1
    cur.execute(
        "UPDATE catalog_services SET status='deleted', updated_at=datetime('now','localtime') WHERE id=?",
        (row["id"],),
    )
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_product_add(conn, args: argparse.Namespace) -> int:
    cur = conn.cursor()
    threshold = args.threshold
    cur.execute(
        """INSERT INTO catalog_products (name, cost, sale_price, unit, threshold)
           VALUES (?,?,?,?,?)""",
        (args.name, float(args.cost), float(args.price), args.unit or "件", float(threshold) if threshold is not None else None),
    )
    conn.commit()
    pid = cur.lastrowid
    print_json({"ok": True, "id": pid, "name": args.name})
    maybe_sync_inventory_row(conn, args.name, args.unit or "件", float(args.cost), float(args.price), threshold)
    return 0


def maybe_sync_inventory_row(conn, name: str, unit: str, cost: float, sale: float, threshold: float | None) -> None:
    import os

    if os.environ.get("BEAUTY_SKIP_INVENTORY_SYNC"):
        return
    thr = 0.0 if threshold is None else float(threshold)
    existing = conn.execute("SELECT key FROM inv_items WHERE key=?", (name,)).fetchone()
    if existing:
        conn.execute(
            """UPDATE inv_items SET unit=?, cost=?, sale_price=?, low_stock_threshold=?,
               updated_at=datetime('now','localtime') WHERE key=?""",
            (unit, float(cost), float(sale), float(thr), name),
        )
    else:
        conn.execute(
            """INSERT INTO inv_items (key, stock_qty, unit, cost, sale_price, low_stock_threshold)
               VALUES (?,?,?,?,?,?)""",
            (name, 0.0, unit, float(cost), float(sale), float(thr)),
        )

def cmd_product_query(conn, args: argparse.Namespace) -> int:
    cur = conn.cursor()
    if args.name:
        row = cur.execute(
            "SELECT * FROM catalog_products WHERE name=? AND status='active'",
            (args.name,),
        ).fetchone()
        rows = [dict(row)] if row else []
    else:
        rows = [dict(r) for r in cur.execute("SELECT * FROM catalog_products WHERE status='active' ORDER BY name")]
    print_json({"products": rows})
    return 0


def cmd_product_update(conn, args: argparse.Namespace) -> int:
    payload = json.loads(args.set)
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id FROM catalog_products WHERE name=? AND status='active'",
        (args.name,),
    ).fetchone()
    if not row:
        print_json({"ok": False, "error": "product_not_found"})
        return 1
    mapping = {
        "cost": "cost",
        "price": "sale_price",
        "sale_price": "sale_price",
        "unit": "unit",
        "threshold": "threshold",
        "name": "name",
    }
    sets = []
    vals: list[Any] = []
    for k, col in mapping.items():
        if k in payload:
            sets.append(f"{col}=?")
            vals.append(payload[k])
    if sets:
        sets.append("updated_at=datetime('now','localtime')")
        vals.append(row["id"])
        cur.execute(f"UPDATE catalog_products SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_product_delete(conn, args: argparse.Namespace) -> int:
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id FROM catalog_products WHERE name=? AND status='active'",
        (args.name,),
    ).fetchone()
    if not row:
        print_json({"ok": False, "error": "product_not_found"})
        return 1
    if not args.yes:
        print_json({"ok": False, "error": "need_confirm", "hint": "请加 --yes 确认删除"})
        return 1
    cur.execute(
        "UPDATE catalog_products SET status='deleted', updated_at=datetime('now','localtime') WHERE id=?",
        (row["id"],),
    )
    conn.commit()
    print_json({"ok": True})
    return 0


def cmd_service_material(conn, args: argparse.Namespace) -> int:
    cur = conn.cursor()
    svc = cur.execute(
        "SELECT id FROM catalog_services WHERE name=? AND status='active'",
        (args.service,),
    ).fetchone()
    if not svc:
        print_json({"ok": False, "error": "service_not_found"})
        return 1
    sid = svc["id"]
    if args.query:
        mats = cur.execute(
            "SELECT product_name, qty FROM catalog_service_materials WHERE service_id=? ORDER BY product_name",
            (sid,),
        ).fetchall()
        out = {m["product_name"]: m["qty"] for m in mats}
        print_json({"ok": True, "service": args.service, "materials": out})
        return 0
    materials = json.loads(args.materials or "{}")
    cur.execute("DELETE FROM catalog_service_materials WHERE service_id=?", (sid,))
    for pname, qty in materials.items():
        cur.execute(
            "INSERT INTO catalog_service_materials (service_id, product_name, qty) VALUES (?,?,?)",
            (sid, str(pname), float(qty)),
        )
    conn.commit()
    print_json({"ok": True, "service": args.service, "materials": materials})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="美容院产品与服务 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("service-add", help="新增服务项目")
    s.add_argument("--name", required=True)
    s.add_argument("--price", type=float, required=True)
    s.add_argument("--duration", type=int, required=True)
    s.add_argument("--materials", default=None, help='耗材 JSON，如 {"面膜":1}')
    s.set_defaults(func=cmd_service_add)

    s = sub.add_parser("service-query", help="查询服务")
    s.add_argument("--name", default=None)
    s.set_defaults(func=cmd_service_query)

    s = sub.add_parser("service-update", help="更新服务")
    s.add_argument("--name", required=True)
    s.add_argument("--set", required=True, help="JSON，如 price/duration_min/name")
    s.set_defaults(func=cmd_service_update)

    s = sub.add_parser("service-delete", help="删除服务")
    s.add_argument("--name", required=True)
    s.add_argument("--yes", action="store_true")
    s.add_argument("--force", action="store_true", help="会员 skill 不可用时仍删除（慎用）")
    s.set_defaults(func=cmd_service_delete)

    s = sub.add_parser("product-add", help="新增产品")
    s.add_argument("--name", required=True)
    s.add_argument("--cost", type=float, required=True)
    s.add_argument("--price", type=float, required=True)
    s.add_argument("--unit", default="件")
    s.add_argument("--threshold", type=float, default=None)
    s.set_defaults(func=cmd_product_add)

    s = sub.add_parser("product-query", help="查询产品")
    s.add_argument("--name", default=None)
    s.set_defaults(func=cmd_product_query)

    s = sub.add_parser("product-update", help="更新产品")
    s.add_argument("--name", required=True)
    s.add_argument("--set", required=True, help="JSON")
    s.set_defaults(func=cmd_product_update)

    s = sub.add_parser("product-delete", help="删除产品")
    s.add_argument("--name", required=True)
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_product_delete)

    s = sub.add_parser("service-material", help="设置或查询服务耗材")
    s.add_argument("--service", required=True)
    s.add_argument("--materials", default=None, help="JSON")
    s.add_argument("--query", action="store_true")
    s.set_defaults(func=cmd_service_material)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    conn = db.connect()
    try:
        return args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
