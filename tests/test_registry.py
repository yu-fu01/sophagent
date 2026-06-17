import pytest
from sophclaw.db import Database

@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()

async def test_provider_crud(db):
    await db.create_provider({"name": "p1", "api_mode": "openai",
        "base_url": "https://x/v1", "api_key_enc": "ENC", "context_limit": 64000})
    rows = await db.list_providers()
    assert [r["name"] for r in rows] == ["p1"]
    await db.update_provider("p1", {"api_mode": "openai", "base_url": "https://y/v1",
        "api_key_enc": "ENC2", "context_limit": 128000})
    row = await db.get_provider("p1")
    assert row["base_url"] == "https://y/v1" and row["context_limit"] == 128000
    await db.delete_provider("p1")
    assert await db.get_provider("p1") is None

async def test_session_override_columns(db):
    uid = await db.create_user("u", "h", "admin")
    await db.ensure_groups()
    gid = (await db.get_owned_group(uid))["id"]
    aid = await db.create_agent({"name": "a", "system_prompt": "s",
        "provider": "p", "model": "m"}, uid, gid)
    await db.create_session("s1", uid, aid, gid)
    await db.set_session_overrides("s1", override_provider="p2",
        override_model="m2", thinking_mode="thinking")
    s = await db.get_session("s1")
    assert s["override_provider"] == "p2" and s["thinking_mode"] == "thinking"
