#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def skills_root() -> Path:
    p = Path(__file__).resolve()
    for parent in [p.parent, *p.parents]:
        cand = parent / "beauty-salon-suite" / "src" / "beauty_db"
        if cand.is_dir():
            return parent
    # Fallback to repo layout.
    return p.parent.parent.parent.parent


def _connect():
    shared = skills_root() / "beauty-salon-suite" / "src"
    if str(shared) not in sys.path:
        sys.path.insert(0, str(shared))
    from beauty_db.db import connect

    return connect()


def print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _get_item(conn, key: str):
    return conn.execute("SELECT * FROM inv_items WHERE key=? AND status='active'", (key,)).fetchone()


def cmd_stock_query(conn, args: argparse.Namespace) -> int:
    cur = conn.cursor()
    if args.key:
        row = cur.execute("SELECT * FROM inv_items WHERE key=? AND status='active'", (args.key,)).fetchone()
        if not row:
            print_json({"ok": False, "error": "not_found", "key": args.key})
            return 1
        d = dict(row)
        alert = bool(float(d.get("stock_qty") or 0) <= float(d.get("low_stock_threshold") or 0))
        print_json({"ok": True, "item": d, "low_stock": alert})
        return 0

    q = "SELECT * FROM inv_items WHERE status='active'"
    params: list[Any] = []
    if args.low_stock:
        q += " AND stock_qty <= low_stock_threshold"
    q += " ORDER BY stock_qty ASC, key ASC"
    rows = [dict(r) for r in cur.execute(q, params).fetchall()]
    print_json({"ok": True, "items": rows})
    return 0


def _log(conn, *, key: str, direction: str, typ: str, qty: float, before: float, after: float, remark: str, ref_appt_id: str | None = None) -> None:
    conn.execute(
        """INSERT INTO inv_logs (item_key, direction, type, quantity, before_qty, after_qty, ref_appt_id, remark)
           VALUES (?,?,?,?,?,?,?,?)""",
        (key, direction, typ, float(qty), float(before), float(after), ref_appt_id, remark or ""),
    )


def cmd_stock_in(conn, args: argparse.Namespace) -> int:
    key = args.key
    qty = float(args.quantity)
    if qty <= 0:
        print_json({"ok": False, "error": "invalid_quantity"})
        return 1
    row = _get_item(conn, key)
    if not row:
        print_json({"ok": False, "error": "not_found", "key": key})
        return 1
    before = float(row["stock_qty"] or 0)
    after = before + qty
    with conn:
        conn.execute("UPDATE inv_items SET stock_qty=?, updated_at=datetime('now','localtime') WHERE key=?", (after, key))
        _log(conn, key=key, direction="in", typ=args.type or "purchase", qty=qty, before=before, after=after, remark=args.remark or "")
    print_json({"ok": True, "key": key, "before": before, "after": after})
    return 0


def cmd_stock_out(conn, args: argparse.Namespace) -> int:
    key = args.key
    qty = float(args.quantity)
    if qty <= 0:
        print_json({"ok": False, "error": "invalid_quantity"})
        return 1
    row = _get_item(conn, key)
    if not row:
        print_json({"ok": False, "error": "not_found", "key": key})
        return 1
    before = float(row["stock_qty"] or 0)
    if qty > before:
        print_json({"ok": False, "error": "insufficient_stock", "key": key, "have": before, "need": qty})
        return 1
    after = before - qty
    with conn:
        conn.execute("UPDATE inv_items SET stock_qty=?, updated_at=datetime('now','localtime') WHERE key=?", (after, key))
        _log(conn, key=key, direction="out", typ=args.type or "sale", qty=qty, before=before, after=after, remark=args.remark or "")
    print_json({"ok": True, "key": key, "before": before, "after": after})
    return 0


def cmd_stock_log(conn, args: argparse.Namespace) -> int:
    q = "SELECT * FROM inv_logs WHERE 1=1"
    params: list[Any] = []
    if args.key:
        q += " AND item_key=?"
        params.append(args.key)
    if args.direction:
        q += " AND direction=?"
        params.append(args.direction)
    if args.from_date:
        q += " AND created_at>=?"
        params.append(args.from_date)
    if args.to_date:
        q += " AND created_at<=?"
        params.append(args.to_date + " 23:59:59")
    q += " ORDER BY created_at DESC LIMIT 200"
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    print_json({"ok": True, "logs": rows})
    return 0


def cmd_item_upsert(conn, args: argparse.Namespace) -> int:
    payload = json.loads(args.data)
    key = str(payload.get("key") or payload.get("产品名") or payload.get("name") or "").strip()
    if not key:
        print_json({"ok": False, "error": "missing_key"})
        return 1
    unit = str(payload.get("unit") or payload.get("单位") or "")
    cost = float(payload.get("cost") or payload.get("成本") or 0)
    sale = float(payload.get("sale_price") or payload.get("售价") or payload.get("price") or 0)
    thr = float(payload.get("low_stock_threshold") or payload.get("预警阈值") or payload.get("threshold") or 0)
    stock = float(payload.get("stock_qty") or payload.get("库存数量") or 0)

    existing = conn.execute("SELECT key FROM inv_items WHERE key=?", (key,)).fetchone()
    with conn:
        if existing:
            conn.execute(
                """UPDATE inv_items SET stock_qty=?, unit=?, cost=?, sale_price=?, low_stock_threshold=?,
                   status='active', updated_at=datetime('now','localtime') WHERE key=?""",
                (stock, unit, cost, sale, thr, key),
            )
        else:
            conn.execute(
                """INSERT INTO inv_items (key, stock_qty, unit, cost, sale_price, low_stock_threshold)
                   VALUES (?,?,?,?,?,?)""",
                (key, stock, unit, cost, sale, thr),
            )
    print_json({"ok": True, "key": key})
    return 0


def cmd_export(conn, args: argparse.Namespace) -> int:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = args.output or f"inv_export_{args.type}_{ts}.csv"
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if args.type == "items":
        rows = conn.execute("SELECT * FROM inv_items WHERE status='active' ORDER BY key").fetchall()
        fields = [d[1] for d in conn.execute("PRAGMA table_info(inv_items)").fetchall()]
        with out_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(dict(r))
        print_json({"ok": True, "output": str(out_path), "count": len(rows)})
        return 0
    if args.type == "logs":
        rows = conn.execute("SELECT * FROM inv_logs ORDER BY created_at DESC").fetchall()
        fields = [d[1] for d in conn.execute("PRAGMA table_info(inv_logs)").fetchall()]
        with out_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(dict(r))
        print_json({"ok": True, "output": str(out_path), "count": len(rows)})
        return 0
    print_json({"ok": False, "error": "invalid_type"})
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="美容院耗材库存（单库 beauty.sqlite3）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stock-query")
    s.add_argument("--key", default=None)
    s.add_argument("--low-stock", action="store_true")
    s.set_defaults(func=cmd_stock_query)

    s = sub.add_parser("stock-in")
    s.add_argument("--key", required=True)
    s.add_argument("--quantity", type=float, required=True)
    s.add_argument("--type", default="purchase")
    s.add_argument("--remark", default="")
    s.set_defaults(func=cmd_stock_in)

    s = sub.add_parser("stock-out")
    s.add_argument("--key", required=True)
    s.add_argument("--quantity", type=float, required=True)
    s.add_argument("--type", default="sale")
    s.add_argument("--remark", default="")
    s.set_defaults(func=cmd_stock_out)

    s = sub.add_parser("stock-log")
    s.add_argument("--key", default=None)
    s.add_argument("--direction", default=None, choices=["in", "out"])
    s.add_argument("--from", dest="from_date", default=None)
    s.add_argument("--to", dest="to_date", default=None)
    s.set_defaults(func=cmd_stock_log)

    s = sub.add_parser("item-upsert", help="新增/更新耗材（key=产品名）")
    s.add_argument("--data", required=True, help='JSON，支持 key/unit/cost/sale_price/stock_qty/low_stock_threshold（兼容中文字段）')
    s.set_defaults(func=cmd_item_upsert)

    s = sub.add_parser("export")
    s.add_argument("--type", choices=["items", "logs"], default="items")
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_export)

    return p


def main() -> int:
    args = build_parser().parse_args()
    conn = _connect()
    try:
        return int(args.func(conn, args))
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

