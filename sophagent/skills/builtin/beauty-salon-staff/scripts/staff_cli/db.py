from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def _skills_root() -> Path:
    p = Path(__file__).resolve()
    for parent in [p.parent, *p.parents]:
        cand = parent / "beauty-salon-suite" / "src" / "beauty_db"
        if cand.is_dir():
            return parent
    return p.parent.parent.parent.parent.parent


def _ensure_shared_db_on_path() -> None:
    root = _skills_root()
    shared = root / "beauty-salon-suite" / "src"
    if str(shared) not in sys.path:
        sys.path.insert(0, str(shared))


def connect() -> sqlite3.Connection:
    _ensure_shared_db_on_path()
    from beauty_db.db import connect as _connect

    return _connect()
