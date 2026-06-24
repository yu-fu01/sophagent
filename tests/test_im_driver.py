"""IMDriver: 入站消息 -> 命令 or run_turns(TelegramTransport) with mock client."""
import pytest
from sophclaw.im.driver import IMDriver
from sophclaw.im import pairing
from sophclaw.im.adapter import MessageEvent


class FakeTelegramClient:
    def __init__(self):
        self.sends, self.edits = [], []
    async def send_message(self, chat_id, text):
        mid = len(self.sends) + 1
        self.sends.append((chat_id, text)); return mid
    async def edit_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))


def _state(client):
    return client.app.state


def _me_id(client, bob):
    return client.get("/api/auth/me", headers=bob).json()["id"]


async def _bind(client, bob, agent_id, chat_id):
    s = _state(client)
    uid = _me_id(client, bob)
    code = await pairing.issue_code(s.db, user_id=uid, agent_id=agent_id)
    await pairing.consume_code(s.db, code)  # 模拟 IM 端 /pair 已消费
    return await pairing.bind(s.db, chat_id, user_id=uid, agent_id=agent_id)


def _ev(text, chat_id):
    return MessageEvent(text=text, chat_id=chat_id, from_id="1")


@pytest.mark.asyncio
async def test_help_command(client, bob, agent_id):
    await _bind(client, bob, agent_id, "chat-1")
    driver = IMDriver(db=_state(client).db, manager=_state(client).manager,
                      skill_store=_state(client).skill_store)
    tc = FakeTelegramClient()
    await driver.handle_inbound(_ev("/help", "chat-1"), client=tc)
    assert any("pair" in s[1] for s in tc.sends)


@pytest.mark.asyncio
async def test_unbound_chat_replies_with_pair_hint(client, bob, agent_id):
    driver = IMDriver(db=_state(client).db, manager=_state(client).manager,
                      skill_store=_state(client).skill_store)
    tc = FakeTelegramClient()
    await driver.handle_inbound(_ev("hi", "unbound-chat"), client=tc)
    assert any("/pair" in s[1] for s in tc.sends)


@pytest.mark.asyncio
async def test_bound_chat_runs_turn_and_edits(client, bob, agent_id):
    await _bind(client, bob, agent_id, "chat-2")
    s = _state(client)
    driver = IMDriver(db=s.db, manager=s.manager, skill_store=s.skill_store)
    tc = FakeTelegramClient()
    await driver.handle_inbound(_ev("hello", "chat-2"), client=tc)
    # handle_inbound 起后台 turn（fire-and-forget，供 /stop）；测试需等它跑完再断言。
    row = await pairing.lookup(s.db, "chat-2")
    task = s.manager._tasks.get(row["session_id"])
    if task is not None:
        await task
    # EchoProvider 回 "echo: hello"：首帧 sendMessage + done 落全文 edit
    assert tc.sends[0][1] == "echo: hello" or any("echo: hello" in e[2] for e in tc.edits)
