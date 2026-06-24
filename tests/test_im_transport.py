"""TelegramTransport: 内部事件 -> edit-in-place 流式（限频）。"""
import asyncio
import pytest
from sophclaw.im.transport import TelegramTransport


class FakeClient:
    def __init__(self):
        self.sends = []      # [(chat_id, text)]
        self.edits = []      # [(chat_id, msg_id, text)]

    async def send_message(self, chat_id, text):
        msg_id = len(self.sends) + 1
        self.sends.append((chat_id, text))
        return msg_id

    async def edit_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))


@pytest.mark.asyncio
async def test_first_delta_sends_then_edits_on_done():
    client = FakeClient()
    t = TelegramTransport(chat_id="42", client=client, min_edit_interval=0)
    await t.on_event({"type": "text_delta", "text": "hel"})
    await t.on_event({"type": "text_delta", "text": "lo"})
    await t.on_event({"type": "done", "usage": {}, "context_length": 0, "context_limit": 0})
    assert client.sends == [("42", "hel")]
    assert len(client.edits) == 1
    assert client.edits[0][2] == "hello"


@pytest.mark.asyncio
async def test_reasoning_and_tool_ignored():
    client = FakeClient()
    t = TelegramTransport("42", client, min_edit_interval=0)
    await t.on_event({"type": "reasoning_delta", "text": "thinking"})
    await t.on_event({"type": "tool_call", "id": "c1", "name": "x", "arguments": {}})
    await t.on_event({"type": "tool_result", "id": "c1", "name": "x", "preview": "r"})
    await t.on_event({"type": "done", "usage": {}, "context_length": 0, "context_limit": 0})
    assert client.sends == []  # 没有任何正文
    assert client.edits == []


@pytest.mark.asyncio
async def test_chained_turn_starts_new_message():
    client = FakeClient()
    t = TelegramTransport("42", client, min_edit_interval=0)
    # turn 1
    await t.on_event({"type": "text_delta", "text": "a"})
    await t.on_event({"type": "done", "usage": {}, "context_length": 0, "context_limit": 0})
    # queued_next + turn 2
    await t.on_event({"type": "queued_next", "content": "q"})
    await t.on_event({"type": "text_delta", "text": "b"})
    await t.on_event({"type": "done", "usage": {}, "context_length": 0, "context_limit": 0})
    assert [s[1] for s in client.sends] == ["a", "b"]  # 两条独立消息
