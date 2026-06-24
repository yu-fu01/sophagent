"""IM pairing + binding DB ops."""
import pytest
from datetime import datetime, timezone, timedelta
from sophagent.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_create_and_consume_pair_code(db):
    code = await db.create_pair_code(user_id=1, agent_id=2)
    assert len(code) == 8
    got = await db.consume_pair_code(code)
    assert got == (1, 2)
    # 一次性：再用无效
    assert await db.consume_pair_code(code) is None


@pytest.mark.asyncio
async def test_consume_unknown_returns_none(db):
    assert await db.consume_pair_code("deadbeef") is None


@pytest.mark.asyncio
async def test_expired_code_returns_none(db):
    code = await db.create_pair_code(user_id=1, agent_id=2, ttl_seconds=-1)
    assert await db.consume_pair_code(code) is None


@pytest.mark.asyncio
async def test_upsert_and_get_binding(db):
    await db.upsert_binding("telegram", "chat-1", user_id=1, agent_id=2, session_id="s1")
    row = await db.get_binding("telegram", "chat-1")
    assert row["user_id"] == 1 and row["agent_id"] == 2 and row["session_id"] == "s1"
    # 覆盖（换 agent/session）
    await db.upsert_binding("telegram", "chat-1", user_id=1, agent_id=3, session_id="s2")
    row = await db.get_binding("telegram", "chat-1")
    assert row["agent_id"] == 3 and row["session_id"] == "s2"


@pytest.mark.asyncio
async def test_get_binding_missing(db):
    assert await db.get_binding("telegram", "nope") is None
