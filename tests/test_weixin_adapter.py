"""Tests for Weixin (personal WeChat) adapter."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sophagent.im.platforms.weixin import (
    ContextTokenStore,
    WeixinClient,
    WeixinConfig,
    extract_text,
    resolve_config,
    truncate_message,
)
from sophagent.im.platforms.weixin.adapter import MessageDeduplicator
from sophagent.im.platforms.weixin.store import load_account, save_account, load_sync_buf, save_sync_buf


class TestExtractText:
    def test_plain_text(self):
        items = [{"type": 1, "text_item": {"text": "你好"}}]
        assert extract_text(items) == "你好"

    def test_voice_transcript(self):
        items = [{"type": 3, "voice_item": {"text": "语音转写内容"}}]
        assert extract_text(items) == "语音转写内容"

    def test_quoted_message(self):
        items = [{
            "type": 1,
            "text_item": {"text": "回复"},
            "ref_msg": {
                "title": "原消息",
                "message_item": {"type": 1, "text_item": {"text": "原文"}},
            },
        }]
        assert extract_text(items) == "[引用: 原消息 | 原文]\n回复"


class TestTruncateMessage:
    def test_short_text(self):
        assert truncate_message("hi", max_length=10) == ["hi"]

    def test_long_text(self):
        text = "a" * 25
        chunks = truncate_message(text, max_length=10)
        assert chunks == ["a" * 10, "a" * 10, "a" * 5]


class TestContextTokenStore:
    def test_persist_and_restore(self, tmp_path: Path):
        store = ContextTokenStore(tmp_path)
        store.set("acct1", "user1", "token-abc")
        store2 = ContextTokenStore(tmp_path)
        store2.restore("acct1")
        assert store2.get("acct1", "user1") == "token-abc"

    def test_clear(self, tmp_path: Path):
        store = ContextTokenStore(tmp_path)
        store.set("acct1", "user1", "token-abc")
        store.clear("acct1", "user1")
        assert store.get("acct1", "user1") is None


class TestMessageDeduplicator:
    def test_duplicate_within_ttl(self):
        dedup = MessageDeduplicator(ttl_seconds=300)
        assert dedup.is_duplicate("msg-1") is False
        assert dedup.is_duplicate("msg-1") is True


class TestResolveConfig:
    def test_from_env_args(self, tmp_path: Path):
        cfg = resolve_config(
            account_id="acct",
            token="tok",
            base_url="https://ilink.example.com",
            data_dir=tmp_path,
        )
        assert cfg is not None
        assert cfg.account_id == "acct"
        assert cfg.token == "tok"

    def test_from_persisted_account(self, tmp_path: Path):
        save_account(
            tmp_path,
            account_id="acct",
            token="saved-tok",
            base_url="https://ilink.example.com",
        )
        cfg = resolve_config(
            account_id="acct",
            token="",
            base_url="",
            data_dir=tmp_path,
        )
        assert cfg is not None
        assert cfg.token == "saved-tok"

    def test_missing_credentials(self, tmp_path: Path):
        assert resolve_config(account_id="", token="", base_url="", data_dir=tmp_path) is None


class TestSyncBuf:
    def test_roundtrip(self, tmp_path: Path):
        save_sync_buf(tmp_path, "acct", "buf-123")
        assert load_sync_buf(tmp_path, "acct") == "buf-123"


@pytest.mark.asyncio
async def test_send_message_uses_context_token(tmp_path: Path):
    config = WeixinConfig(account_id="acct", token="tok", base_url="https://ilink.example.com")
    client = WeixinClient(config, tmp_path)
    client._token_store.set("acct", "user1", "ctx-token")

    captured: dict = {}

    async def fake_send(session, *, base_url, token, to, text, context_token, client_id):
        captured.update({
            "to": to,
            "text": text,
            "context_token": context_token,
            "token": token,
        })
        return {"ret": 0, "errcode": 0}

    with patch("sophagent.im.platforms.weixin.adapter._send_message_api", new=AsyncMock(side_effect=fake_send)):
        await client.send_message("user1", "hello")

    assert captured["to"] == "user1"
    assert captured["text"] == "hello"
    assert captured["context_token"] == "ctx-token"
    await client.aclose()


@pytest.mark.asyncio
async def test_process_message_dispatches_text(tmp_path: Path):
    config = WeixinConfig(account_id="bot-acct", token="tok")
    client = WeixinClient(config, tmp_path)
    driver = MagicMock()
    driver.handle_inbound = AsyncMock()

    message = {
        "from_user_id": "user-abc",
        "message_id": "m1",
        "context_token": "ctx-1",
        "item_list": [{"type": 1, "text_item": {"text": "/pair code123"}}],
    }
    await client._process_message(driver, message, allowed=set())

    driver.handle_inbound.assert_awaited_once()
    ev = driver.handle_inbound.await_args.args[0]
    assert ev.platform == "weixin"
    assert ev.text == "/pair code123"
    assert ev.chat_id == "user-abc"
    assert client._token_store.get("bot-acct", "user-abc") == "ctx-1"
    await client.aclose()


@pytest.mark.asyncio
async def test_process_message_batches_plain_text(tmp_path: Path, monkeypatch):
    config = WeixinConfig(account_id="bot-acct", token="tok")
    client = WeixinClient(config, tmp_path)
    driver = MagicMock()
    driver.handle_inbound = AsyncMock()
    monkeypatch.setattr("sophagent.im.platforms.weixin.adapter.TEXT_BATCH_DELAY_SECONDS", 0.05)

    message = {
        "from_user_id": "user-abc",
        "message_id": "m-batch",
        "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
    }
    await client._process_message(driver, message, allowed=set())
    await asyncio.sleep(0.15)
    driver.handle_inbound.assert_awaited_once()
    ev = driver.handle_inbound.await_args.args[0]
    assert ev.text == "你好"
    await client.aclose()


@pytest.mark.asyncio
async def test_process_message_respects_allowed_users(tmp_path: Path):
    config = WeixinConfig(account_id="bot-acct", token="tok")
    client = WeixinClient(config, tmp_path)
    driver = MagicMock()
    driver.handle_inbound = AsyncMock()

    message = {
        "from_user_id": "blocked-user",
        "message_id": "m2",
        "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
    }
    await client._process_message(driver, message, allowed={"allowed-user"})

    driver.handle_inbound.assert_not_awaited()
    await client.aclose()


@pytest.mark.asyncio
async def test_process_message_pair_command_bypasses_allowlist(tmp_path: Path):
    config = WeixinConfig(account_id="bot-acct", token="tok")
    client = WeixinClient(config, tmp_path)
    driver = MagicMock()
    driver.handle_inbound = AsyncMock()

    message = {
        "from_user_id": "new-user",
        "message_id": "m-pair",
        "item_list": [{"type": 1, "text_item": {"text": "/pair abc123"}}],
    }
    await client._process_message(driver, message, allowed={"other-user"})

    driver.handle_inbound.assert_awaited_once()
    ev = driver.handle_inbound.await_args.args[0]
    assert ev.text == "/pair abc123"
    await client.aclose()
