from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Iterable


DEFAULT_DB_REL = Path(".config") / "beauty-salon-suite" / "beauty.sqlite3"


def default_db_path() -> Path:
    base = Path.home() / DEFAULT_DB_REL
    base.parent.mkdir(parents=True, exist_ok=True)
    return base


def get_db_path() -> Path:
    p = os.environ.get("BEAUTY_DB_PATH")
    if p:
        path = Path(p).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return default_db_path()


def _pragma(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")


def connect(*, path: Path | None = None, timeout: float = 5.0) -> sqlite3.Connection:
    db_path = path or get_db_path()
    conn = sqlite3.connect(str(db_path), timeout=timeout)
    _pragma(conn)
    migrate(conn)
    return conn


def with_retry(fn, *, retries: int = 3, base_sleep_s: float = 0.05):
    last = None
    for i in range(retries + 1):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "database is locked" not in msg and "database schema is locked" not in msg:
                raise
            last = e
            if i >= retries:
                raise
            time.sleep(base_sleep_s * (3**i))
    raise last  # pragma: no cover


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta_schema_version (
            version INTEGER NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        """
    )
    row = conn.execute("SELECT version FROM meta_schema_version ORDER BY updated_at DESC LIMIT 1").fetchone()
    cur_version = int(row["version"]) if row else 0
    if cur_version >= 1:
        return
    _apply_migrations(conn, _migrations_from_0_to_1())
    conn.execute("INSERT INTO meta_schema_version (version) VALUES (1)")
    conn.commit()


def _apply_migrations(conn: sqlite3.Connection, migrations: Iterable[str]) -> None:
    for sql in migrations:
        conn.executescript(sql)


def _migrations_from_0_to_1() -> list[str]:
    return [
        """
        -- member domain
        CREATE TABLE IF NOT EXISTS member_members (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT,
            birthday TEXT,
            skin_type TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            fields_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS member_wallet (
            member_id TEXT PRIMARY KEY REFERENCES member_members(id) ON DELETE CASCADE,
            balance REAL NOT NULL DEFAULT 0,
            total_recharge REAL NOT NULL DEFAULT 0,
            total_consume REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS member_cards (
            id TEXT PRIMARY KEY,
            member_id TEXT NOT NULL REFERENCES member_members(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            service_name TEXT,
            total_times INTEGER NOT NULL,
            used_times INTEGER NOT NULL DEFAULT 0,
            price REAL NOT NULL DEFAULT 0,
            expire_date TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS member_appointments (
            id TEXT PRIMARY KEY,
            member_id TEXT NOT NULL REFERENCES member_members(id),
            service TEXT NOT NULL,
            technician_id TEXT NOT NULL,
            start_dt TEXT NOT NULL,
            duration_min INTEGER NOT NULL DEFAULT 60,
            status TEXT NOT NULL DEFAULT 'scheduled',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_member_appt_tech_dt ON member_appointments(technician_id, start_dt, status);
        CREATE TABLE IF NOT EXISTS member_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id TEXT NOT NULL REFERENCES member_members(id),
            type TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            ref_appt_id TEXT,
            technician_id TEXT,
            meta_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS member_consumption_log (
            member_id TEXT PRIMARY KEY REFERENCES member_members(id) ON DELETE CASCADE,
            last_visit_at TEXT,
            fav_service TEXT,
            visit_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        """,
        """
        -- catalog domain
        CREATE TABLE IF NOT EXISTS catalog_services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            price REAL NOT NULL,
            duration_min INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS catalog_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            cost REAL NOT NULL DEFAULT 0,
            sale_price REAL NOT NULL DEFAULT 0,
            unit TEXT NOT NULL DEFAULT '件',
            threshold REAL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS catalog_service_materials (
            service_id INTEGER NOT NULL REFERENCES catalog_services(id) ON DELETE CASCADE,
            product_name TEXT NOT NULL,
            qty REAL NOT NULL DEFAULT 1,
            PRIMARY KEY (service_id, product_name)
        );
        CREATE INDEX IF NOT EXISTS idx_catalog_service_materials_product ON catalog_service_materials(product_name);
        """,
        """
        -- staff domain
        CREATE TABLE IF NOT EXISTS staff_staff (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT,
            join_date TEXT,
            base_salary REAL NOT NULL DEFAULT 0,
            fields_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS staff_fields (
            name TEXT PRIMARY KEY,
            type TEXT NOT NULL DEFAULT 'text'
        );
        CREATE TABLE IF NOT EXISTS staff_commission_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id TEXT NOT NULL REFERENCES staff_staff(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK(kind IN ('service','product')),
            target_name TEXT,
            ratio REAL NOT NULL,
            UNIQUE(staff_id, kind, target_name)
        );
        CREATE TABLE IF NOT EXISTS staff_schedules (
            staff_id TEXT NOT NULL REFERENCES staff_staff(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'work',
            slots_json TEXT,
            PRIMARY KEY (staff_id, date)
        );
        CREATE TABLE IF NOT EXISTS staff_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id TEXT NOT NULL REFERENCES staff_staff(id) ON DELETE CASCADE,
            ymd TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'service',
            target_name TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL DEFAULT 0,
            source_appt_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_staff_perf_staff_ymd ON staff_performance(staff_id, ymd);
        """,
        """
        -- inventory domain
        CREATE TABLE IF NOT EXISTS inv_items (
            key TEXT PRIMARY KEY,
            stock_qty REAL NOT NULL DEFAULT 0,
            unit TEXT NOT NULL DEFAULT '',
            cost REAL NOT NULL DEFAULT 0,
            sale_price REAL NOT NULL DEFAULT 0,
            low_stock_threshold REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS inv_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_key TEXT NOT NULL REFERENCES inv_items(key),
            direction TEXT NOT NULL CHECK(direction IN ('in','out')),
            type TEXT NOT NULL DEFAULT '',
            quantity REAL NOT NULL,
            before_qty REAL NOT NULL,
            after_qty REAL NOT NULL,
            ref_appt_id TEXT,
            remark TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_inv_logs_item_time ON inv_logs(item_key, created_at);
        """,
    ]

