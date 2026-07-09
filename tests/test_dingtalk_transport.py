"""DingTalk AI Card transport tests."""

from unittest.mock import AsyncMock

import pytest

from sophagent.im.platforms.dingtalk.transport import DingTalkCardTransport


@pytest.mark.asyncio
async def test_transport_streams_via_edit():
    client = AsyncMock()
    client.send_message.return_value = "card-1"
    transport = DingTalkCardTransport("chat1", client, min_edit_interval=0)

    await transport.on_event({"type": "text_delta", "text": "Hello"})
    await transport.on_event({"type": "text_delta", "text": " world"})
    await transport.on_event({"type": "done"})

    client.send_message.assert_awaited_once()
    assert client.edit_message.await_count >= 1
    last_edit = client.edit_message.await_args_list[-1]
    assert last_edit.kwargs.get("finalize") is True
    assert "Hello world" in last_edit.args[2]
