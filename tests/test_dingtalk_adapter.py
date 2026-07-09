"""DingTalk adapter unit tests."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sophagent.im.platforms.dingtalk.adapter import (
    DingTalkAdapter,
    DingTalkConfig,
    _extract_text,
)


class TestExtractText:
    def test_plain_text_content(self):
        msg = SimpleNamespace(text=SimpleNamespace(content="hello"))
        assert _extract_text(msg) == "hello"

    def test_dict_text(self):
        msg = SimpleNamespace(text={"content": "/pair abc"})
        assert _extract_text(msg) == "/pair abc"


@pytest.mark.asyncio
async def test_on_message_pair_bypasses_allowlist():
    config = DingTalkConfig(
        client_id="cid",
        client_secret="sec",
        card_template_id="tmpl",
        dm_policy="pairing",
    )
    adapter = DingTalkAdapter(config, Path(tempfile.mkdtemp()))
    driver = MagicMock()
    driver.handle_inbound = AsyncMock()

    message = SimpleNamespace(
        message_id="m1",
        conversation_id="chat1",
        conversation_type="1",
        sender_id="u1",
        sender_staff_id="staff1",
        session_webhook="https://oapi.dingtalk.com/robot/send?access_token=x",
        session_webhook_expired_time=0,
        is_in_at_list=False,
        text=SimpleNamespace(content="/pair code123"),
    )
    await adapter._on_message(message, driver, allowed={"other-user"})
    driver.handle_inbound.assert_awaited_once()
    ev = driver.handle_inbound.await_args.args[0]
    assert ev.text == "/pair code123"
    assert ev.platform == "dingtalk"


@pytest.mark.asyncio
async def test_on_message_blocks_non_pair_when_allowlist():
    config = DingTalkConfig(
        client_id="cid",
        client_secret="sec",
        card_template_id="tmpl",
        dm_policy="pairing",
    )
    adapter = DingTalkAdapter(config, Path(tempfile.mkdtemp()))
    driver = MagicMock()
    driver.handle_inbound = AsyncMock()

    message = SimpleNamespace(
        message_id="m2",
        conversation_id="chat2",
        conversation_type="1",
        sender_id="u2",
        sender_staff_id="staff2",
        session_webhook="https://oapi.dingtalk.com/robot/send?access_token=x",
        session_webhook_expired_time=0,
        is_in_at_list=False,
        text=SimpleNamespace(content="hello"),
    )
    await adapter._on_message(message, driver, allowed={"other-user"})
    driver.handle_inbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_group_requires_mention_or_pattern():
    config = DingTalkConfig(
        client_id="cid",
        client_secret="sec",
        card_template_id="tmpl",
        dm_policy="pairing",
        require_mention=True,
        mention_patterns=("sophagent",),
    )
    adapter = DingTalkAdapter(config, Path(tempfile.mkdtemp()))
    driver = MagicMock()
    driver.handle_inbound = AsyncMock()

    blocked = SimpleNamespace(
        message_id="m3",
        conversation_id="group1",
        conversation_type="2",
        sender_id="u3",
        sender_staff_id="staff3",
        session_webhook="https://oapi.dingtalk.com/robot/send?access_token=x",
        session_webhook_expired_time=0,
        is_in_at_list=False,
        text=SimpleNamespace(content="随便聊聊"),
    )
    await adapter._on_message(blocked, driver, allowed=set())
    driver.handle_inbound.assert_not_awaited()

    allowed = SimpleNamespace(
        message_id="m4",
        conversation_id="group1",
        conversation_type="2",
        sender_id="u3",
        sender_staff_id="staff3",
        session_webhook="https://oapi.dingtalk.com/robot/send?access_token=x",
        session_webhook_expired_time=0,
        is_in_at_list=False,
        text=SimpleNamespace(content="sophagent 你好"),
    )
    await adapter._on_message(allowed, driver, allowed=set())
    driver.handle_inbound.assert_awaited_once()

