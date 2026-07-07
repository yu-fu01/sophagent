"""TelegramTransport: 内部事件 -> edit-in-place 流式（限频）。"""
import asyncio
import pytest
from sophagent.im.transport import BufferedSendTransport, TelegramTransport


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


class FailingClient:
    """send/edit always raise — transport must not propagate."""
    def __init__(self): self.sends, self.edits = 0, 0
    async def send_message(self, chat_id, text):
        self.sends += 1; raise RuntimeError("network down")
    async def edit_message(self, chat_id, message_id, text):
        self.edits += 1; raise RuntimeError("network down")


@pytest.mark.asyncio
async def test_send_failure_does_not_kill_turn():
    c = FailingClient()
    t = TelegramTransport("42", c, min_edit_interval=0)
    # text_delta + done：send 都失败，但 on_event 绝不抛
    await t.on_event({"type": "text_delta", "text": "hi"})
    await t.on_event({"type": "text_delta", "text": "!"})
    await t.on_event({"type": "done", "usage": {}, "context_length": 0, "context_limit": 0})
    assert c.sends >= 1           # 重试过
    # turn 没崩（到这里就证明）


@pytest.mark.asyncio
async def test_edit_failure_does_not_kill_turn():
    # 先用正常 client 建起 message_id，再换成 failing 测 edit 容错
    class OkThenFail:
        def __init__(self): self.mid = 0
        async def send_message(self, chat_id, text):
            self.mid += 1; return self.mid
        async def edit_message(self, chat_id, message_id, text):
            raise RuntimeError("edit down")
    c = OkThenFail()
    t = TelegramTransport("42", c, min_edit_interval=0)
    await t.on_event({"type": "text_delta", "text": "a"})   # send ok -> message_id=1
    await t.on_event({"type": "text_delta", "text": "b"})   # edit fails -> 不抛
    await t.on_event({"type": "done", "usage": {}, "context_length": 0, "context_limit": 0})  # edit fails -> 不抛
    assert t.message_id is None  # done 清了


@pytest.mark.asyncio
async def test_buffered_transport_sends_once_on_done():
    client = FakeClient()
    t = BufferedSendTransport("qq:c2c", client)
    await t.on_event({"type": "text_delta", "text": "hel"})
    await t.on_event({"type": "text_delta", "text": "lo"})
    assert client.sends == []
    await t.on_event({"type": "done"})
    assert client.sends == [("qq:c2c", "hello")]
