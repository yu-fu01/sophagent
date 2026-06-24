"""会话搜索（子项目②）测试：FTS5 索引同步/回填、多用户隔离、session_search
三形态（发现/浏览/翻阅）。全部使用临时库 / fake provider。"""

from __future__ import annotations

import pytest

from sophclaw import config as config_mod
from sophclaw.db import Database
from sophclaw.models import Message


async def _db(tmp_path, monkeypatch) -> Database:
    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    db = Database(tmp_path / "s.db")
    await db.connect()
    return db


async def _user(db, uid: int, name: str) -> None:
    await db._exec(
        "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
        (uid, name, "h", "t"),
    )
    await db.conn.commit()


async def _agent(db) -> int:
    cur = await db._exec(
        "INSERT INTO agents (name, description, system_prompt, provider, model, "
        "tools, created_at, updated_at) VALUES ('a','','p','test','m','[]','t','t')"
    )
    await db.conn.commit()
    return cur.lastrowid


async def _session(db, sid: str, uid: int) -> None:
    aid = await _agent(db)
    await db._exec(
        "INSERT INTO sessions (id, user_id, agent_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (sid, uid, aid, "", "t", "t"),
    )
    await db.conn.commit()


# -- 单元 1：DB 层 FTS 同步 / 回填 / 查询 ------------------------------------


@pytest.mark.asyncio
async def test_append_indexes_user_assistant_skips_tool(tmp_path, monkeypatch):
    db = await _db(tmp_path, monkeypatch)
    try:
        await _user(db, 1, "bob")
        await _session(db, "s1", 1)
        await db.append_messages("s1", [
            Message(role="user", content="how do I deploy with docker compose"),
            Message(role="assistant", content="use docker compose up minus d"),
            Message(role="tool", content="docker secret token here"),
        ])
        hits = await db.search_messages(1, "docker", limit_rows=50)
        roles = {h["role"] for h in hits}
        assert roles == {"user", "assistant"}  # tool 不入索引
        # tool 专有词搜不到
        assert await db.search_messages(1, "secret", limit_rows=50) == []
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_search_scoped_by_user(tmp_path, monkeypatch):
    db = await _db(tmp_path, monkeypatch)
    try:
        await _user(db, 1, "bob")
        await _user(db, 2, "alice")
        await _session(db, "sb", 1)
        await _session(db, "sa", 2)
        await db.append_messages("sb", [Message(role="user", content="bob kubernetes plan")])
        await db.append_messages("sa", [Message(role="user", content="alice kubernetes plan")])
        bob_hits = await db.search_messages(1, "kubernetes", limit_rows=50)
        assert {h["session_id"] for h in bob_hits} == {"sb"}  # 搜不到 alice
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_truncate_removes_from_fts(tmp_path, monkeypatch):
    db = await _db(tmp_path, monkeypatch)
    try:
        await _user(db, 1, "bob")
        await _session(db, "s1", 1)
        await db.append_messages("s1", [Message(role="user", content="ephemeral pineapple note")])
        hit = await db.search_messages(1, "pineapple", limit_rows=50)
        assert hit
        await db.truncate_from("s1", hit[0]["message_id"])
        assert await db.search_messages(1, "pineapple", limit_rows=50) == []
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_backfill_indexes_existing_messages(tmp_path, monkeypatch):
    """旧库已有 messages 但 FTS 空 → connect 回填。模拟：手工插入消息行后
    清空 FTS，再次 connect 触发回填。"""
    db = await _db(tmp_path, monkeypatch)
    await _user(db, 1, "bob")
    await _session(db, "s1", 1)
    await db.append_messages("s1", [Message(role="user", content="backfilled rhubarb fact")])
    # 清空 FTS 模拟"旧库从未建索引"
    await db.conn.execute("DELETE FROM messages_fts")
    await db.conn.commit()
    await db.close()

    db2 = Database(tmp_path / "s.db")
    await db2.connect()  # 应触发回填
    try:
        assert await db2.search_messages(1, "rhubarb", limit_rows=50)
    finally:
        await db2.close()
        config_mod.reset_config()
