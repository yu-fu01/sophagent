"""SQLite persistence (aiosqlite, WAL). A single connection serializes writes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from .models import Message

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  system_prompt TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  tools TEXT NOT NULL DEFAULT '[]',
  skills TEXT,
  max_iterations INTEGER DEFAULT 30,
  temperature REAL,
  group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(group_id, name)
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  title TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS memory (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  target TEXT NOT NULL DEFAULT 'memory',
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_user ON memory(user_id);
CREATE TABLE IF NOT EXISTS groups (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  is_admin_group INTEGER NOT NULL DEFAULT 0,
  is_cron_group INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  can_manage INTEGER NOT NULL DEFAULT 0,
  joined_at TEXT NOT NULL,
  PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_members_user ON group_members(user_id);
CREATE TABLE IF NOT EXISTS group_join (
  id INTEGER PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,          -- 'request' | 'invite'
  created_at TEXT NOT NULL,
  UNIQUE(group_id, user_id, kind)
);
CREATE TABLE IF NOT EXISTS providers (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  api_mode TEXT NOT NULL,
  base_url TEXT,
  api_key_enc TEXT,
  context_limit INTEGER NOT NULL DEFAULT 100000,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT,
  updated_by INTEGER
);
CREATE TABLE IF NOT EXISTS im_pair_codes (
  code       TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  agent_id   INTEGER NOT NULL,
  expires_at TEXT NOT NULL,
  used       INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS im_bindings (
  platform   TEXT NOT NULL,
  chat_id    TEXT NOT NULL,
  user_id    INTEGER NOT NULL,
  agent_id   INTEGER NOT NULL,
  session_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (platform, chat_id)
);
-- Staged memory writes awaiting user approval (when write_approval is on).
CREATE TABLE IF NOT EXISTS pending_writes (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'memory',
  op TEXT NOT NULL,                          -- add | replace | remove
  target TEXT NOT NULL DEFAULT 'memory',     -- memory | user
  content TEXT NOT NULL DEFAULT '',
  memory_id INTEGER,                         -- target row for replace/remove
  origin TEXT NOT NULL DEFAULT 'foreground', -- foreground | review
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_user ON pending_writes(user_id, id);
-- Full-text index over user/assistant message text for session_search.
-- Standalone (not external-content) because messages.content is JSON; we sync
-- it in Python from the message write paths. UNINDEXED columns are stored but
-- not tokenised, so they can be combined with MATCH in a WHERE clause.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text,
  message_id UNINDEXED,
  session_id UNINDEXED,
  user_id UNINDEXED,
  role UNINDEXED,
  tokenize = 'unicode61'
);
-- 定时任务（cron）。next_run_at 存服务器本地 naive ISO 时间。
CREATE TABLE IF NOT EXISTS cron_jobs (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  prompt TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'agent',          -- agent | text
  schedule TEXT NOT NULL,                       -- JSON: {kind, ...}
  schedule_display TEXT NOT NULL DEFAULT '',
  repeat_times INTEGER,                         -- NULL = 无限
  repeat_completed INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  state TEXT NOT NULL DEFAULT 'scheduled',      -- scheduled | paused | completed | error
  last_run_at TEXT,
  next_run_at TEXT,
  last_status TEXT,                             -- ok | error
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cron_jobs_due ON cron_jobs(enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_cron_jobs_session ON cron_jobs(session_id);
"""


_FTS_ROLES = ("user", "assistant")


def _fts_text(role: str, content: str) -> Optional[str]:
    """Text to index for session_search, or None to skip. Only user/assistant
    turns are indexed (tool output is usually noise); empty content is skipped."""
    if role not in _FTS_ROLES:
        return None
    text = (content or "").strip()
    return text or None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()
        await self._migrate_structure()
        await self._backfill_fts()

    async def _columns(self, table: str) -> set[str]:
        rows = await self._all(f"PRAGMA table_info({table})")
        return {r["name"] for r in rows}

    async def _migrate_structure(self) -> None:
        """Upgrade pre-multiuser schemas in place (idempotent). New DBs already
        match SCHEMA, so these guards are no-ops there."""
        agent_cols = await self._columns("agents")
        if agent_cols and "group_id" not in agent_cols:
            # rebuild agents to add group_id and switch UNIQUE(name) -> UNIQUE(group_id, name)
            await self.conn.execute("PRAGMA foreign_keys=OFF")
            await self.conn.executescript(
                """
                CREATE TABLE agents_new (
                  id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
                  system_prompt TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                  tools TEXT NOT NULL DEFAULT '[]', skills TEXT, max_iterations INTEGER DEFAULT 30,
                  temperature REAL, group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
                  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL, UNIQUE(group_id, name)
                );
                INSERT INTO agents_new (id, name, description, system_prompt, provider, model, tools,
                  skills, max_iterations, temperature, group_id, created_by, created_at, updated_at)
                SELECT id, name, description, system_prompt, provider, model, tools, skills,
                  max_iterations, temperature, NULL, created_by, created_at, updated_at FROM agents;
                DROP TABLE agents;
                ALTER TABLE agents_new RENAME TO agents;
                """
            )
            await self.conn.commit()
            await self.conn.execute("PRAGMA foreign_keys=ON")
        session_cols = await self._columns("sessions")
        if session_cols and "group_id" not in session_cols:
            await self._exec("ALTER TABLE sessions ADD COLUMN group_id INTEGER REFERENCES groups(id)")
        session_cols = await self._columns("sessions")
        for col in ("override_provider", "override_model", "thinking_mode"):
            if session_cols and col not in session_cols:
                await self._exec(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
        group_cols = await self._columns("groups")
        if group_cols and "is_cron_group" not in group_cols:
            await self._exec("ALTER TABLE groups ADD COLUMN is_cron_group INTEGER NOT NULL DEFAULT 0")
        memory_cols = await self._columns("memory")
        if memory_cols and "target" not in memory_cols:
            # dual-store: legacy single-store rows become agent-notes ('memory')
            await self._exec(
                "ALTER TABLE memory ADD COLUMN target TEXT NOT NULL DEFAULT 'memory'"
            )

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def _exec(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur

    async def _one(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()

    # -- users ---------------------------------------------------------------

    async def create_user(self, username: str, password_hash: str, role: str = "user") -> int:
        cur = await self._exec(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
            (username, password_hash, role, now()),
        )
        return cur.lastrowid

    async def get_user(self, user_id: int) -> Optional[aiosqlite.Row]:
        return await self._one("SELECT * FROM users WHERE id=?", (user_id,))

    async def get_user_by_username(self, username: str) -> Optional[aiosqlite.Row]:
        return await self._one("SELECT * FROM users WHERE username=?", (username,))

    async def list_users(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT id, username, role, created_at FROM users ORDER BY id")

    async def update_user(self, user_id: int, *, password_hash: str | None = None, role: str | None = None) -> None:
        if password_hash is not None:
            await self._exec("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
        if role is not None:
            await self._exec("UPDATE users SET role=? WHERE id=?", (role, user_id))

    async def delete_user(self, user_id: int) -> None:
        await self._exec("DELETE FROM users WHERE id=?", (user_id,))

    async def count_users(self) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM users")
        return row["n"]

    async def count_admins(self) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM users WHERE role='admin'")
        return row["n"]

    # -- agents --------------------------------------------------------------

    async def create_agent(self, fields: dict[str, Any], created_by: int, group_id: int) -> int:
        cur = await self._exec(
            "INSERT INTO agents (name, description, system_prompt, provider, model, tools, skills,"
            " max_iterations, temperature, group_id, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fields["name"], fields.get("description", ""), fields["system_prompt"],
                fields["provider"], fields["model"], json.dumps(fields.get("tools", [])),
                json.dumps(fields["skills"]) if fields.get("skills") is not None else None,
                fields.get("max_iterations", 30), fields.get("temperature"),
                group_id, created_by, now(), now(),
            ),
        )
        return cur.lastrowid

    async def get_agent(self, agent_id: int) -> Optional[aiosqlite.Row]:
        return await self._one("SELECT * FROM agents WHERE id=?", (agent_id,))

    async def get_agent_by_name(self, name: str) -> Optional[aiosqlite.Row]:
        return await self._one("SELECT * FROM agents WHERE name=?", (name,))

    async def list_agents(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM agents ORDER BY id")

    async def list_agents_for_user(self, user_id: int) -> list[aiosqlite.Row]:
        """Agents in every group the user is a member of."""
        return await self._all(
            "SELECT a.* FROM agents a JOIN group_members gm ON gm.group_id=a.group_id"
            " WHERE gm.user_id=? ORDER BY a.id",
            (user_id,),
        )

    async def update_agent(self, agent_id: int, fields: dict[str, Any]) -> None:
        await self._exec(
            "UPDATE agents SET name=?, description=?, system_prompt=?, provider=?, model=?,"
            " tools=?, skills=?, max_iterations=?, temperature=?, updated_at=? WHERE id=?",
            (
                fields["name"], fields.get("description", ""), fields["system_prompt"],
                fields["provider"], fields["model"], json.dumps(fields.get("tools", [])),
                json.dumps(fields["skills"]) if fields.get("skills") is not None else None,
                fields.get("max_iterations", 30), fields.get("temperature"), now(), agent_id,
            ),
        )

    async def delete_agent(self, agent_id: int) -> None:
        await self._exec("DELETE FROM agents WHERE id=?", (agent_id,))

    async def list_session_ids_for_agent(self, agent_id: int) -> list[str]:
        rows = await self._all(
            "SELECT id FROM sessions WHERE agent_id=?", (agent_id,),
        )
        return [str(r["id"]) for r in rows]

    async def delete_agent_cascade(self, agent_id: int) -> None:
        """Delete all sessions (messages cascade) then the agent row."""
        await self._exec("DELETE FROM sessions WHERE agent_id=?", (agent_id,))
        await self._exec("DELETE FROM agents WHERE id=?", (agent_id,))

    # -- sessions ------------------------------------------------------------

    async def create_session(self, session_id: str, user_id: int, agent_id: int,
                             group_id: int, title: str = "") -> None:
        await self._exec(
            "INSERT INTO sessions (id, user_id, agent_id, group_id, title, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (session_id, user_id, agent_id, group_id, title, now(), now()),
        )

    async def get_session(self, session_id: str, user_id: int | None = None) -> Optional[aiosqlite.Row]:
        if user_id is None:
            return await self._one("SELECT * FROM sessions WHERE id=?", (session_id,))
        return await self._one("SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, user_id))

    async def list_sessions(self, user_id: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT s.*, a.name AS agent_name, COALESCE(g.is_cron_group, 0) AS is_cron"
            " FROM sessions s JOIN agents a ON a.id=s.agent_id"
            " LEFT JOIN groups g ON g.id=s.group_id"
            " WHERE s.user_id=? ORDER BY s.updated_at DESC",
            (user_id,),
        )

    async def list_sessions_by_group(self, gid: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT s.*, a.name AS agent_name, u.username AS creator"
            " FROM sessions s JOIN agents a ON a.id=s.agent_id JOIN users u ON u.id=s.user_id"
            " WHERE s.group_id=? ORDER BY s.updated_at DESC",
            (gid,),
        )

    async def touch_session(self, session_id: str, title: str | None = None) -> None:
        if title:
            await self._exec("UPDATE sessions SET updated_at=?, title=? WHERE id=?", (now(), title, session_id))
        else:
            await self._exec("UPDATE sessions SET updated_at=? WHERE id=?", (now(), session_id))

    async def delete_session(self, session_id: str) -> None:
        await self._exec("DELETE FROM sessions WHERE id=?", (session_id,))

    async def set_session_overrides(self, session_id: str, *, override_provider=None,
                                    override_model=None, thinking_mode=None) -> None:
        await self._exec(
            "UPDATE sessions SET override_provider=?, override_model=?, thinking_mode=?"
            " WHERE id=?",
            (override_provider, override_model, thinking_mode, session_id),
        )

    # -- messages ------------------------------------------------------------

    async def _session_user_id(self, session_id: str) -> Optional[int]:
        row = await self._one("SELECT user_id FROM sessions WHERE id=?", (session_id,))
        return row["user_id"] if row else None

    async def _index_message(
        self, message_id: int, session_id: str, user_id: Optional[int], role: str, content: str
    ) -> None:
        """Add a row to messages_fts if this message is searchable. No commit."""
        text = _fts_text(role, content)
        if text is None or user_id is None:
            return
        await self.conn.execute(
            "INSERT INTO messages_fts (text, message_id, session_id, user_id, role) "
            "VALUES (?,?,?,?,?)",
            (text, message_id, session_id, user_id, role),
        )

    async def append_messages(self, session_id: str, messages: list[Message]) -> None:
        user_id = await self._session_user_id(session_id)
        for m in messages:
            cur = await self.conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                (session_id, m.role, m.to_json(), now()),
            )
            await self._index_message(cur.lastrowid, session_id, user_id, m.role, m.content)
        await self.conn.commit()

    async def load_messages(self, session_id: str) -> list[Message]:
        rows = await self._all(
            "SELECT content FROM messages WHERE session_id=? AND archived=0 ORDER BY id", (session_id,)
        )
        return [Message.from_json(r["content"]) for r in rows]

    async def load_messages_with_ids(self, session_id: str) -> list[tuple[int, Message, str]]:
        """Same as load_messages but also returns each row's id and created_at
        (id for restore/re-edit；created_at 给前端渲染气泡时间戳)。"""
        rows = await self._all(
            "SELECT id, content, created_at FROM messages WHERE session_id=? AND archived=0 ORDER BY id",
            (session_id,),
        )
        return [(r["id"], Message.from_json(r["content"]), r["created_at"]) for r in rows]

    async def truncate_from(self, session_id: str, message_id: int) -> int:
        """Delete the live (archived=0) message with id>=message_id and everything after it.
        Used by restore (rewind to before a user message) and re-edit (send-time replace).
        Returns the number of deleted rows."""
        # collect the ids first so we can drop their FTS rows in lock-step
        doomed = await self._all(
            "SELECT id FROM messages WHERE session_id=? AND archived=0 AND id>=?",
            (session_id, message_id),
        )
        cur = await self.conn.execute(
            "DELETE FROM messages WHERE session_id=? AND archived=0 AND id>=?",
            (session_id, message_id),
        )
        for r in doomed:
            await self.conn.execute(
                "DELETE FROM messages_fts WHERE message_id=?", (r["id"],)
            )
        await self.conn.commit()
        return cur.rowcount

    async def compact_session(self, session_id: str, live_history: list[Message]) -> None:
        """After in-memory compression: archive current rows (kept for audit, and
        kept in the FTS index so session_search can still recall the originals)
        and re-insert the compressed history as the live message log."""
        user_id = await self._session_user_id(session_id)
        await self.conn.execute("UPDATE messages SET archived=1 WHERE session_id=? AND archived=0", (session_id,))
        for m in live_history:
            cur = await self.conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                (session_id, m.role, m.to_json(), now()),
            )
            await self._index_message(cur.lastrowid, session_id, user_id, m.role, m.content)
        await self.conn.commit()

    # -- session search (FTS5) -------------------------------------------------

    async def _backfill_fts(self) -> None:
        """One-time index population for DBs that predate messages_fts. Idempotent:
        guarded by the FTS table being empty."""
        row = await self._one("SELECT count(*) AS c FROM messages_fts")
        if row and row["c"]:
            return
        rows = await self._all(
            "SELECT m.id, m.session_id, m.role, m.content, s.user_id "
            "FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE m.role IN ('user','assistant')"
        )
        for r in rows:
            content = Message.from_json(r["content"]).content
            await self._index_message(r["id"], r["session_id"], r["user_id"], r["role"], content)
        await self.conn.commit()

    async def search_messages(self, user_id: int, query: str, limit_rows: int = 50) -> list[aiosqlite.Row]:
        """FTS5 ranked matches scoped to one user. Returns message_id, session_id,
        role and a highlighted snippet, best matches first."""
        return await self._all(
            "SELECT message_id, session_id, role, "
            "snippet(messages_fts, 0, '[', ']', '…', 12) AS snippet "
            "FROM messages_fts WHERE messages_fts MATCH ? AND user_id=? "
            "ORDER BY rank LIMIT ?",
            (query, user_id, limit_rows),
        )

    async def session_texts(self, session_id: str) -> list[tuple[int, str, str]]:
        """Ordered (id, role, text) of indexable user/assistant messages in a
        session — the unit session_search windows over."""
        rows = await self._all(
            "SELECT id, role, content FROM messages WHERE session_id=? "
            "AND role IN ('user','assistant') ORDER BY id",
            (session_id,),
        )
        out: list[tuple[int, str, str]] = []
        for r in rows:
            text = (Message.from_json(r["content"]).content or "").strip()
            if text:
                out.append((r["id"], r["role"], text))
        return out

    async def recent_sessions(self, user_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT id, title, updated_at FROM sessions WHERE user_id=? "
            "ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        )

    # -- memory ----------------------------------------------------------------

    async def memory_list(
        self, user_id: int, target: str | None = None
    ) -> list[aiosqlite.Row]:
        if target is None:
            return await self._all(
                "SELECT * FROM memory WHERE user_id=? ORDER BY id", (user_id,)
            )
        return await self._all(
            "SELECT * FROM memory WHERE user_id=? AND target=? ORDER BY id",
            (user_id, target),
        )

    async def memory_add(self, user_id: int, content: str, target: str = "memory") -> int:
        cur = await self._exec(
            "INSERT INTO memory (user_id, target, content, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (user_id, target, content, now(), now()),
        )
        return cur.lastrowid

    async def memory_replace(self, memory_id: int, user_id: int, content: str) -> bool:
        cur = await self._exec(
            "UPDATE memory SET content=?, updated_at=? WHERE id=? AND user_id=?",
            (content, now(), memory_id, user_id),
        )
        return cur.rowcount > 0

    async def memory_remove(self, memory_id: int, user_id: int) -> bool:
        cur = await self._exec("DELETE FROM memory WHERE id=? AND user_id=?", (memory_id, user_id))
        return cur.rowcount > 0

    # -- pending writes (write-approval staging) -----------------------------

    async def pending_add(
        self, user_id: int, *, op: str, target: str = "memory", content: str = "",
        memory_id: int | None = None, origin: str = "foreground",
    ) -> int:
        cur = await self._exec(
            "INSERT INTO pending_writes (user_id, op, target, content, memory_id, origin, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, op, target, content, memory_id, origin, now()),
        )
        return cur.lastrowid

    async def pending_list(self, user_id: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT * FROM pending_writes WHERE user_id=? ORDER BY id", (user_id,)
        )

    async def pending_get(self, pending_id: int, user_id: int) -> Optional[aiosqlite.Row]:
        return await self._one(
            "SELECT * FROM pending_writes WHERE id=? AND user_id=?", (pending_id, user_id)
        )

    async def pending_remove(self, pending_id: int, user_id: int) -> bool:
        cur = await self._exec(
            "DELETE FROM pending_writes WHERE id=? AND user_id=?", (pending_id, user_id)
        )
        return cur.rowcount > 0

    async def pending_clear(self, user_id: int) -> int:
        cur = await self._exec("DELETE FROM pending_writes WHERE user_id=?", (user_id,))
        return cur.rowcount

    # -- providers -----------------------------------------------------------

    async def create_provider(self, fields: dict[str, Any]) -> int:
        cur = await self._exec(
            "INSERT INTO providers (name, api_mode, base_url, api_key_enc, context_limit,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (fields["name"], fields["api_mode"], fields.get("base_url"), fields.get("api_key_enc"),
             int(fields.get("context_limit", 100_000)), now(), now()),
        )
        return cur.lastrowid

    async def get_provider(self, name: str) -> Optional[aiosqlite.Row]:
        return await self._one("SELECT * FROM providers WHERE name=?", (name,))

    async def list_providers(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM providers ORDER BY name")

    async def update_provider(self, name: str, fields: dict[str, Any]) -> None:
        # api_key_enc=None means "leave unchanged"
        if fields.get("api_key_enc") is None:
            await self._exec(
                "UPDATE providers SET api_mode=?, base_url=?, context_limit=?, updated_at=?"
                " WHERE name=?",
                (fields["api_mode"], fields.get("base_url"), int(fields["context_limit"]), now(), name),
            )
        else:
            await self._exec(
                "UPDATE providers SET api_mode=?, base_url=?, api_key_enc=?, context_limit=?,"
                " updated_at=? WHERE name=?",
                (fields["api_mode"], fields.get("base_url"), fields["api_key_enc"],
                 int(fields["context_limit"]), now(), name),
            )

    async def delete_provider(self, name: str) -> None:
        await self._exec("DELETE FROM providers WHERE name=?", (name,))

    # -- settings (generic runtime key/value) --------------------------------

    async def get_setting(self, key: str) -> Optional[str]:
        row = await self._one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str, user_id: int) -> None:
        await self._exec(
            "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (key, value, now(), user_id),
        )

    # -- groups --------------------------------------------------------------

    async def create_group(self, name: str, owner_id: int, is_admin_group: bool = False,
                           is_cron_group: bool = False) -> int:
        cur = await self._exec(
            "INSERT INTO groups (name, owner_id, is_admin_group, is_cron_group, created_at)"
            " VALUES (?,?,?,?,?)",
            (name, owner_id, 1 if is_admin_group else 0, 1 if is_cron_group else 0, now()),
        )
        gid = cur.lastrowid
        await self._exec(
            "INSERT OR IGNORE INTO group_members (group_id, user_id, can_manage, joined_at) VALUES (?,?,1,?)",
            (gid, owner_id, now()),
        )
        return gid

    async def create_personal_group(self, user_id: int, username: str) -> int:
        return await self.create_group(f"{username} 的组", user_id)

    async def get_or_create_cron_group(self, user_id: int) -> int:
        """该用户专属的「定时任务」组 id；不存在则建。幂等。"""
        row = await self._one(
            "SELECT id FROM groups WHERE owner_id=? AND is_cron_group=1 LIMIT 1", (user_id,)
        )
        if row is not None:
            return row["id"]
        return await self.create_group("定时任务", user_id, is_cron_group=True)

    async def get_group(self, gid: int) -> Optional[aiosqlite.Row]:
        return await self._one("SELECT * FROM groups WHERE id=?", (gid,))

    async def get_admin_group(self) -> Optional[aiosqlite.Row]:
        return await self._one("SELECT * FROM groups WHERE is_admin_group=1 LIMIT 1")

    async def get_owned_group(self, user_id: int) -> Optional[aiosqlite.Row]:
        """The user's primary group: admin group for the root admin, else personal."""
        return await self._one("SELECT * FROM groups WHERE owner_id=? ORDER BY id LIMIT 1", (user_id,))

    async def list_groups(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM groups ORDER BY id")

    async def list_user_groups(self, user_id: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT g.*, gm.can_manage FROM groups g JOIN group_members gm ON gm.group_id=g.id"
            " WHERE gm.user_id=? ORDER BY g.id",
            (user_id,),
        )

    async def rename_group(self, gid: int, name: str) -> None:
        await self._exec("UPDATE groups SET name=? WHERE id=?", (name, gid))

    async def delete_group(self, gid: int) -> None:
        await self._exec("DELETE FROM groups WHERE id=?", (gid,))

    # -- group members -------------------------------------------------------

    async def get_member(self, gid: int, user_id: int) -> Optional[aiosqlite.Row]:
        return await self._one("SELECT * FROM group_members WHERE group_id=? AND user_id=?", (gid, user_id))

    async def is_member(self, gid: int, user_id: int) -> bool:
        return await self.get_member(gid, user_id) is not None

    async def list_members(self, gid: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT gm.user_id, gm.can_manage, gm.joined_at, u.username"
            " FROM group_members gm JOIN users u ON u.id=gm.user_id"
            " WHERE gm.group_id=? ORDER BY gm.user_id",
            (gid,),
        )

    async def add_member(self, gid: int, user_id: int, can_manage: bool = False) -> None:
        await self._exec(
            "INSERT OR IGNORE INTO group_members (group_id, user_id, can_manage, joined_at) VALUES (?,?,?,?)",
            (gid, user_id, 1 if can_manage else 0, now()),
        )

    async def set_can_manage(self, gid: int, user_id: int, can_manage: bool) -> None:
        await self._exec(
            "UPDATE group_members SET can_manage=? WHERE group_id=? AND user_id=?",
            (1 if can_manage else 0, gid, user_id),
        )

    async def remove_member(self, gid: int, user_id: int) -> None:
        await self._exec("DELETE FROM group_members WHERE group_id=? AND user_id=?", (gid, user_id))

    async def is_admin(self, user_id: int) -> bool:
        ag = await self.get_admin_group()
        return ag is not None and await self.is_member(ag["id"], user_id)

    async def get_root_admin_id(self) -> Optional[int]:
        ag = await self.get_admin_group()
        return ag["owner_id"] if ag else None

    # -- group join requests / invitations -----------------------------------

    async def create_join(self, gid: int, user_id: int, kind: str) -> int:
        cur = await self._exec(
            "INSERT OR IGNORE INTO group_join (group_id, user_id, kind, created_at) VALUES (?,?,?,?)",
            (gid, user_id, kind, now()),
        )
        return cur.lastrowid

    async def get_join(self, join_id: int) -> Optional[aiosqlite.Row]:
        return await self._one("SELECT * FROM group_join WHERE id=?", (join_id,))

    async def list_group_requests(self, gid: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT j.*, u.username FROM group_join j JOIN users u ON u.id=j.user_id"
            " WHERE j.group_id=? AND j.kind='request' ORDER BY j.id",
            (gid,),
        )

    async def list_user_invitations(self, user_id: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT j.*, g.name AS group_name FROM group_join j JOIN groups g ON g.id=j.group_id"
            " WHERE j.user_id=? AND j.kind='invite' ORDER BY j.id",
            (user_id,),
        )

    async def delete_join(self, join_id: int) -> None:
        await self._exec("DELETE FROM group_join WHERE id=?", (join_id,))

    # -- bootstrap / migration reconciler -------------------------------------

    async def sync_roles(self) -> None:
        """Keep users.role in sync with admin-group membership (cache only)."""
        ag = await self.get_admin_group()
        if ag is None:
            return
        await self._exec("UPDATE users SET role='user' WHERE role='admin'")
        await self._exec(
            "UPDATE users SET role='admin' WHERE id IN (SELECT user_id FROM group_members WHERE group_id=?)",
            (ag["id"],),
        )

    async def ensure_groups(self) -> None:
        """Idempotent: create the admin group, give every user a personal group,
        and keep users.role synced. Safe to run on every startup."""
        if await self.get_admin_group() is None:
            row = await self._one("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1")
            if row is not None:
                await self.create_group("admin group", row["id"], is_admin_group=True)
        ownerless = await self._all("SELECT id, username FROM users WHERE id NOT IN (SELECT owner_id FROM groups)")
        for r in ownerless:
            await self.create_personal_group(r["id"], r["username"])
        await self.sync_roles()
        # backfill resources orphaned by a pre-multiuser DB
        ag = await self.get_admin_group()
        if ag is not None:
            await self._exec("UPDATE agents SET group_id=? WHERE group_id IS NULL", (ag["id"],))
        await self._exec(
            "UPDATE sessions SET group_id=(SELECT id FROM groups WHERE owner_id=sessions.user_id LIMIT 1)"
            " WHERE group_id IS NULL"
        )

    # -- IM pairing / bindings ---------------------------------------------

    async def create_pair_code(self, user_id: int, agent_id: int, ttl_seconds: int = 600) -> str:
        import secrets as _secrets
        from datetime import datetime, timezone, timedelta
        code = _secrets.token_hex(4)  # 8 字符
        expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        await self._exec(
            "INSERT INTO im_pair_codes (code, user_id, agent_id, expires_at, used) VALUES (?,?,?,?,0)",
            (code, user_id, agent_id, expires),
        )
        return code

    async def consume_pair_code(self, code: str) -> Optional[tuple[int, int]]:
        """返回 (user_id, agent_id) 或 None（不存在/已用/已过期）。命中即标记 used。"""
        from datetime import datetime, timezone
        row = await self._one(
            "SELECT user_id, agent_id, expires_at, used FROM im_pair_codes WHERE code=?",
            (code,),
        )
        if row is None or row["used"]:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            return None
        await self._exec("UPDATE im_pair_codes SET used=1 WHERE code=?", (code,))
        return row["user_id"], row["agent_id"]

    async def upsert_binding(self, platform: str, chat_id: str, user_id: int,
                             agent_id: int, session_id: str) -> None:
        await self._exec(
            "INSERT INTO im_bindings (platform, chat_id, user_id, agent_id, session_id, created_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(platform, chat_id) DO UPDATE SET"
            " user_id=excluded.user_id, agent_id=excluded.agent_id, session_id=excluded.session_id",
            (platform, chat_id, user_id, agent_id, session_id, now()),
        )

    async def get_binding(self, platform: str, chat_id: str) -> Optional[aiosqlite.Row]:
        return await self._one(
            "SELECT * FROM im_bindings WHERE platform=? AND chat_id=?",
            (platform, chat_id),
        )

    # -- cron jobs ----------------------------------------------------------
    # schedule 以 JSON 文本存；next_run_at / last_run_at 存本地 naive ISO。
    # 单连接串行写，无需额外锁（与 hermes 的 flock 等价由 aiosqlite 提供）。

    async def create_cron_job(self, job: dict[str, Any]) -> str:
        ts = now()
        await self._exec(
            "INSERT INTO cron_jobs (id, session_id, user_id, name, prompt, mode, schedule,"
            " schedule_display, repeat_times, repeat_completed, enabled, state,"
            " last_run_at, next_run_at, last_status, last_error, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,0,1,'scheduled',NULL,?,NULL,NULL,?,?)",
            (
                job["id"], job["session_id"], job["user_id"], job["name"], job["prompt"],
                job.get("mode", "agent"), json.dumps(job["schedule"]), job.get("schedule_display", ""),
                job.get("repeat_times"), job.get("next_run_at"), ts, ts,
            ),
        )
        return job["id"]

    async def get_cron_job(self, job_id: str) -> Optional[aiosqlite.Row]:
        return await self._one("SELECT * FROM cron_jobs WHERE id=?", (job_id,))

    async def list_cron_jobs(self, user_id: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT * FROM cron_jobs WHERE user_id=? ORDER BY enabled DESC, next_run_at ASC",
            (user_id,),
        )

    async def list_cron_jobs_by_session(self, session_id: str) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT * FROM cron_jobs WHERE session_id=? ORDER BY enabled DESC, next_run_at ASC",
            (session_id,),
        )

    async def list_due_cron_jobs(self, now_iso: str) -> list[aiosqlite.Row]:
        """enabled=1 且 state!='paused'/'completed' 且 next_run_at<=now。"""
        return await self._all(
            "SELECT * FROM cron_jobs WHERE enabled=1 AND state IN ('scheduled','error')"
            " AND next_run_at IS NOT NULL AND next_run_at<=? ORDER BY next_run_at ASC",
            (now_iso,),
        )

    async def advance_cron_next_run(self, job_id: str, next_run_at: Optional[str]) -> None:
        """at-most-once：执行前先把 next_run_at 推进，崩溃重启不会连发。"""
        await self._exec(
            "UPDATE cron_jobs SET next_run_at=?, updated_at=? WHERE id=?",
            (next_run_at, now(), job_id),
        )

    async def mark_cron_run(
        self, job_id: str, *, success: bool, error: Optional[str],
        next_run_at: Optional[str], completed: bool, repeat_completed: Optional[int] = None,
    ) -> None:
        """执行后更新状态。completed=True 则 state='completed'；recurring 的
        next_run_at 在执行前已 advance，这里只同步状态字段。"""
        sets = ["last_run_at=?", "last_status=?", "last_error=?", "next_run_at=?", "updated_at=?"]
        if completed:
            sets.append("state='completed'")
        elif not success:
            sets.append("state='error'")
        else:
            sets.append("state='scheduled'")
        params: list[Any] = [now(), "ok" if success else "error", error, next_run_at, now()]
        if repeat_completed is not None:
            sets.append("repeat_completed=?")
            params.append(repeat_completed)
        params.append(job_id)
        await self._exec(f"UPDATE cron_jobs SET {', '.join(sets)} WHERE id=?", tuple(params))

    async def set_cron_job_state(self, job_id: str, state: str, user_id: int) -> bool:
        """owner-scoped 状态切换（pause/resume）。返回是否命中。"""
        cur = await self._exec(
            "UPDATE cron_jobs SET state=?, updated_at=? WHERE id=? AND user_id=?",
            (state, now(), job_id, user_id),
        )
        return cur.rowcount > 0

    async def delete_cron_job(self, job_id: str, user_id: int) -> bool:
        cur = await self._exec(
            "DELETE FROM cron_jobs WHERE id=? AND user_id=?", (job_id, user_id)
        )
        return cur.rowcount > 0
