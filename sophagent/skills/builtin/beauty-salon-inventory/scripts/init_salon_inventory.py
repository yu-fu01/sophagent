#!/usr/bin/env python3
"""初始化美容院库存（单库 beauty.sqlite3）。"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# .../skills/beauty-salon-inventory/src/scripts -> parent^3 = skills
SKILLS_DIR = SCRIPT_DIR.parent.parent.parent


def _connect():
    # Robust search upward for `beauty-salon-suite/src/beauty_db` (zip installs may differ).
    shared = None
    for parent in [SCRIPT_DIR, *SCRIPT_DIR.parents]:
        cand = parent / "beauty-salon-suite" / "src"
        if (cand / "beauty_db").is_dir():
            shared = cand
            break
    if shared is None:
        shared = SKILLS_DIR / "beauty-salon-suite" / "src"
    if str(shared) not in sys.path:
        sys.path.insert(0, str(shared))
    from beauty_db.db import connect

    return connect()


def main() -> int:
    # Just ensure the shared DB exists and schema migrated.
    conn = _connect()
    conn.close()
    print("OK: beauty.sqlite3 初始化完成（如需录入耗材，请使用 salon_inventory_cli.py item-upsert）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
