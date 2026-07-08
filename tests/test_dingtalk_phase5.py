"""DingTalk Phase 5 ops enhancements tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sophagent.im.platforms.dingtalk.adapter import DingTalkAdapter, DingTalkConfig
from sophagent.im.platforms.dingtalk.notify import send_static_webhook_text
from sophagent.im.platforms.dingtalk.transport import DingTalkBufferedTransport


@pytest.mark.asyncio
async def test_send_static_webhook_text():
    class FakeResp:
        status_code = 200

        def json(self):
            return {"errcode": 0}

    http = MagicMock()
    http.post = AsyncMock(return_value=FakeResp())
    await send_static_webhook_text(
        "https://oapi.dingtalk.com/robot/send?access_token=test",
        "hello cron",
        client=http,
    )
    http.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_fire_done_reaction_idempotent(tmp_path):
    config = DingTalkConfig(
        client_id="cid",
        client_secret="sec",
        card_template_id="",
        reply_emotion=True,
    )
    adapter = DingTalkAdapter(config, tmp_path)
    message = MagicMock(message_id="m1", conversation_id="c1", robot_code="cid")
    adapter._turn_source_messages["chat1"] = message
    adapter._get_access_token = AsyncMock(return_value="tok")

    with patch(
        "sophagent.im.platforms.dingtalk.adapter.DingTalkEmotionClient.send_emotion",
        new=AsyncMock(),
    ) as send_emotion:
        await adapter._fire_done_reaction("chat1")
        await adapter._fire_done_reaction("chat1")
        assert send_emotion.await_count == 2


@pytest.mark.asyncio
async def test_buffered_transport_calls_turn_complete():
    client = MagicMock()
    client.send_message = AsyncMock()
    on_complete = AsyncMock()
    transport = DingTalkBufferedTransport("chat1", client, on_turn_complete=on_complete)
    await transport.on_event({"type": "text_delta", "text": "hi"})
    await transport.on_event({"type": "done"})
    await transport.close()
    on_complete.assert_awaited_once_with("chat1")


@pytest.mark.asyncio
async def test_reply_emotion_disabled_skips_done(tmp_path):
    config = DingTalkConfig(
        client_id="cid",
        client_secret="sec",
        card_template_id="",
        reply_emotion=False,
    )
    adapter = DingTalkAdapter(config, tmp_path)
    adapter._emotion.send_emotion = AsyncMock()
    adapter._get_access_token = AsyncMock(return_value="tok")
    adapter._message_contexts["chat1"] = MagicMock(message_id="m1", conversation_id="c1")
    await adapter._fire_done_reaction("chat1")
    adapter._emotion.send_emotion.assert_not_awaited()
