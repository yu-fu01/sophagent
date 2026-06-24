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


# -- 单元 2：session_search 工具三形态 + 安全 -------------------------------


def _ctx(db, user_id: int):
    from sophclaw.models import AgentDef
    from sophclaw.tools.registry import ToolContext

    agent = AgentDef(
        id=1, name="t", description="", system_prompt="p",
        provider="test", model="m", tools=["session_search"], skills=None,
    )
    from pathlib import Path
    return ToolContext(user_id=user_id, workspace=Path("/tmp"), agent=agent, db=db)


@pytest.mark.asyncio
async def test_tool_discovery_returns_match_and_window(tmp_path, monkeypatch):
    from sophclaw.tools.session_search import session_search

    db = await _db(tmp_path, monkeypatch)
    try:
        await _user(db, 1, "bob")
        await _session(db, "s1", 1)
        await db.append_messages("s1", [
            Message(role="user", content="first unrelated message"),
            Message(role="assistant", content="ok noted"),
            Message(role="user", content="how to configure nginx reverse proxy"),
            Message(role="assistant", content="set proxy_pass in the location block"),
        ])
        out = await session_search(_ctx(db, 1), query="nginx")
        assert "s1" in out
        assert "nginx" in out  # snippet 含命中词
        assert "proxy_pass" in out  # 命中点附近窗口
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_tool_browse_lists_recent_sessions(tmp_path, monkeypatch):
    from sophclaw.tools.session_search import session_search

    db = await _db(tmp_path, monkeypatch)
    try:
        await _user(db, 1, "bob")
        await _session(db, "s1", 1)
        await db.append_messages("s1", [Message(role="user", content="hello world topic")])
        out = await session_search(_ctx(db, 1))  # 无参 → 浏览
        assert "s1" in out
        assert "hello world topic" in out  # 预览
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_tool_scroll_returns_window(tmp_path, monkeypatch):
    from sophclaw.tools.session_search import session_search

    db = await _db(tmp_path, monkeypatch)
    try:
        await _user(db, 1, "bob")
        await _session(db, "s1", 1)
        await db.append_messages("s1", [
            Message(role="user", content=f"line {i}") for i in range(10)
        ])
        texts = await db.session_texts("s1")
        anchor = texts[5][0]
        out = await session_search(_ctx(db, 1), session_id="s1", around_message_id=anchor, window=2)
        assert "line 5" in out
        assert "line 3" in out and "line 7" in out  # ±2 窗口
        assert "line 0" not in out  # 窗口外
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_tool_scroll_rejects_other_users_session(tmp_path, monkeypatch):
    from sophclaw.tools.session_search import session_search

    db = await _db(tmp_path, monkeypatch)
    try:
        await _user(db, 1, "bob")
        await _user(db, 2, "alice")
        await _session(db, "sa", 2)
        await db.append_messages("sa", [Message(role="user", content="alice private note")])
        texts = await db.session_texts("sa")
        # bob (user 1) 试图翻阅 alice 的 session
        out = await session_search(_ctx(db, 1), session_id="sa", around_message_id=texts[0][0])
        assert "not found" in out.lower()
        assert "private note" not in out
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_tool_handles_invalid_fts_query(tmp_path, monkeypatch):
    from sophclaw.tools.session_search import session_search

    db = await _db(tmp_path, monkeypatch)
    try:
        await _user(db, 1, "bob")
        await _session(db, "s1", 1)
        await db.append_messages("s1", [Message(role="user", content="normal text")])
        out = await session_search(_ctx(db, 1), query='"unbalanced')  # 非法 FTS 语法
        assert "error" in out.lower()  # 优雅报错而非抛异常
    finally:
        await db.close()
        config_mod.reset_config()
