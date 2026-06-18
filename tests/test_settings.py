"""运行时设置（settings 表）与可调上传上限的测试。

覆盖 DB 键值 upsert、effective_max_upload_bytes 解析、/api/settings 读写权限与
校验，以及上限改动对 /api/files 上传与读取的端到端生效。"""

import base64

import pytest

from sophclaw import config as config_mod
from sophclaw.config import effective_max_upload_bytes, get_config
from sophclaw.db import Database

DEFAULT = 10 * 1024 * 1024  # 内置默认 10MB


def data_url(content: bytes, mime: str = "text/plain") -> str:
    return f"data:{mime};base64," + base64.b64encode(content).decode()


# -- DB 层：settings 键值 upsert 往返 ----------------------------------------


@pytest.mark.asyncio
async def test_setting_upsert_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    db = Database(tmp_path / "s.db")
    await db.connect()
    try:
        assert await db.get_setting("k") is None
        await db.set_setting("k", "v1", user_id=1)
        assert await db.get_setting("k") == "v1"
        # 覆盖更新
        await db.set_setting("k", "v2", user_id=1)
        assert await db.get_setting("k") == "v2"
    finally:
        await db.close()
        config_mod.reset_config()


# -- 生效解析 effective_max_upload_bytes -------------------------------------


@pytest.mark.asyncio
async def test_effective_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    db = Database(tmp_path / "s.db")
    await db.connect()
    try:
        assert await effective_max_upload_bytes(db) == get_config().max_upload_bytes
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_effective_uses_db_value(tmp_path, monkeypatch):
    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    db = Database(tmp_path / "s.db")
    await db.connect()
    try:
        await db.set_setting("max_upload_bytes", "2048", user_id=1)
        assert await effective_max_upload_bytes(db) == 2048
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_effective_ignores_garbage_db_value(tmp_path, monkeypatch):
    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    db = Database(tmp_path / "s.db")
    await db.connect()
    try:
        await db.set_setting("max_upload_bytes", "not-a-number", user_id=1)
        assert await effective_max_upload_bytes(db) == get_config().max_upload_bytes
    finally:
        await db.close()
        config_mod.reset_config()


# -- API：GET /api/settings（任意登录用户可读）-------------------------------


def test_get_settings_any_user(client, bob):
    resp = client.get("/api/settings", headers=bob)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["max_upload_bytes"] == DEFAULT
    assert body["default_max_upload_bytes"] == DEFAULT
    assert body["min_bytes"] == 1024
    assert body["max_bytes"] == 1024 * 1024 * 1024


# -- API：PUT 仅 admin + 校验 ------------------------------------------------


def test_put_setting_admin_ok(client, admin, bob):
    resp = client.put("/api/settings/max_upload_bytes",
                      json={"max_upload_bytes": 5 * 1024 * 1024}, headers=admin)
    assert resp.status_code == 200, resp.text
    # 读回生效
    got = client.get("/api/settings", headers=bob).json()
    assert got["max_upload_bytes"] == 5 * 1024 * 1024
    assert got["default_max_upload_bytes"] == DEFAULT  # 默认值不变


def test_put_setting_non_admin_forbidden(client, bob):
    resp = client.put("/api/settings/max_upload_bytes",
                      json={"max_upload_bytes": 2048}, headers=bob)
    assert resp.status_code == 403


@pytest.mark.parametrize("value", [0, -1, 1023, 1024 * 1024 * 1024 + 1])
def test_put_setting_out_of_bounds(client, admin, value):
    resp = client.put("/api/settings/max_upload_bytes",
                      json={"max_upload_bytes": value}, headers=admin)
    assert resp.status_code == 400, resp.text


# -- 端到端：改上限后 /upload 与 /read 按新上限触发 413 ----------------------


def test_new_limit_enforced_on_upload(client, admin, bob):
    client.put("/api/settings/max_upload_bytes",
               json={"max_upload_bytes": 1024}, headers=admin)
    # 1024 以内放行
    ok = client.post("/api/files/upload",
                     json={"path": "small.txt", "data_url": data_url(b"x" * 500)},
                     headers=bob)
    assert ok.status_code == 200, ok.text
    # 超过 1024 拒绝
    too_big = client.post("/api/files/upload",
                          json={"path": "big.txt", "data_url": data_url(b"x" * 2000)},
                          headers=bob)
    assert too_big.status_code == 413


def test_new_limit_enforced_on_read(client, admin, bob):
    from conftest import uid

    client.put("/api/settings/max_upload_bytes",
               json={"max_upload_bytes": 1024}, headers=admin)
    # 直接往工作目录写一个 2KB 文件（绕过上传上限），再读应 413
    bob_id = uid(client, admin, "bob")
    ws = get_config().workspace_for(bob_id)
    (ws / "huge.bin").write_bytes(b"x" * 2000)
    resp = client.get("/api/files/read", params={"path": "huge.bin"}, headers=bob)
    assert resp.status_code == 413
