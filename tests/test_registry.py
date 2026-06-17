import pytest
from sophclaw.config import ProviderConfig
from sophclaw.crypto import encrypt
from sophclaw.db import Database
from sophclaw.providers.registry import ProviderRegistry, ResolvedProvider

SECRET = "0" * 64


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
    # 不传 api_key_enc 时保留原 key（REQ: 编辑留空=不变）
    before = (await db.get_provider("p1"))["api_key_enc"]
    await db.update_provider("p1", {"api_mode": "openai",
        "base_url": "https://z/v1", "context_limit": 32000})
    row = await db.get_provider("p1")
    assert row["api_key_enc"] == before and row["base_url"] == "https://z/v1"
    await db.delete_provider("p1")
    assert await db.get_provider("p1") is None

async def test_registry_merges_db_over_builtin(db):
    builtin = {"p1": ProviderConfig(name="p1", api_mode="openai",
                                    base_url="https://builtin/v1", source="builtin")}
    await db.create_provider({"name": "p1", "api_mode": "openai",
        "base_url": "https://db/v1", "api_key_enc": encrypt("sk-db", SECRET),
        "context_limit": 50000})
    reg = ProviderRegistry(db, builtin_providers=builtin, secret=SECRET)
    await reg.refresh()
    r = reg.resolve("p1")
    assert r.source == "db" and r.base_url == "https://db/v1" and r.api_key == "sk-db"

async def test_registry_client_cache_rebuilds_on_change(db):
    await db.create_provider({"name": "p1", "api_mode": "openai", "base_url": None,
        "api_key_enc": encrypt("sk-1", SECRET), "context_limit": 100000})
    reg = ProviderRegistry(db, builtin_providers={}, secret=SECRET)
    await reg.refresh()
    c1 = reg.client("p1")
    assert reg.client("p1") is c1               # same fingerprint -> cached
    await db.update_provider("p1", {"api_mode": "openai", "base_url": "https://new/v1",
        "api_key_enc": encrypt("sk-2", SECRET), "context_limit": 100000})
    await reg.refresh()
    assert reg.client("p1") is not c1           # fingerprint changed -> rebuilt

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
