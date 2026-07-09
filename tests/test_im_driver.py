"""IMDriver: 入站消息 -> 命令 or run_turns(TelegramTransport) with mock client."""
import pytest
from sophagent.im.driver import IMDriver
from sophagent.im import pairing
from sophagent.im.adapter import MessageEvent
from sophagent.im.transport import BufferedSendTransport
from sophagent.models import MessageAttachment
from sophagent.models import Message
from sophagent.im.platforms.qqbot import QQBotClient, _handle_interaction, _parse_face_tags
from sophagent.im.platforms.qqbot_keyboard import build_help_keyboard, parse_interaction_event


class FakeTelegramClient:
    def __init__(self):
        self.sends, self.edits = [], []
    async def send_message(self, chat_id, text):
        mid = len(self.sends) + 1
        self.sends.append((chat_id, text)); return mid
    async def edit_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))


class FakeQQClient(FakeTelegramClient):
    def __init__(self):
        super().__init__()
        self.keyboards = []
        self.acks = []
    async def send_with_keyboard(self, chat_id, text, keyboard):
        self.keyboards.append((chat_id, text, keyboard.to_dict()))
        return len(self.keyboards)
    async def ack_interaction(self, interaction_id, code=0):
        self.acks.append((interaction_id, code))


class FakeFeishuClient(FakeTelegramClient):
    def __init__(self):
        super().__init__()
        self.cards = []
    async def send_with_card(self, chat_id, text, card):
        self.cards.append((chat_id, text, card))
        return f"msg-{len(self.cards)}"


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


def _qq_ev(text, chat_id):
    return MessageEvent(text=text, chat_id=chat_id, from_id="qq-user", platform="qqbot")


def _feishu_ev(text, chat_id):
    return MessageEvent(text=text, chat_id=chat_id, from_id="ou-user", platform="feishu")


def test_message_attachment_json_roundtrip():
    msg = Message(
        role="user",
        content="hi",
        attachments=[MessageAttachment(kind="image", path="im/qqbot/pic.png", mime="image/png")],
    )
    restored = Message.from_json(msg.to_json())
    assert restored.attachments[0].kind == "image"
    assert restored.attachments[0].path == "im/qqbot/pic.png"


def test_qq_face_tag_is_decoded_for_model_and_ui():
    text, attachments = _parse_face_tags('这表情包<faceType=1,faceId="5",ext="eyJ0ZXh0Ijoi5rWB5rOqIn0=">表达的情绪是')
    assert text == "这表情包[表情: 流泪]表达的情绪是"
    assert attachments[0].kind == "emoji"
    assert attachments[0].name == "流泪"
    assert attachments[0].raw["face_id"] == "5"


def test_qq_markdown_and_keyboard_payloads():
    client = QQBotClient("appid", "secret")
    body = client._message_body("c2c:openid", "## 标题")
    assert body["msg_type"] == 2
    assert body["markdown"]["content"] == "## 标题"
    body = client._message_body("c2c:openid", "帮助", keyboard=build_help_keyboard(), force_text=True)
    assert body["msg_type"] == 0
    assert body["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["data"] == "cmd:/new"


def test_parse_qq_interaction_event():
    ev = parse_interaction_event({
        "id": "interaction-1",
        "chat_type": 2,
        "user_openid": "user-openid",
        "data": {"resolved": {"button_data": "cmd:/help", "button_id": "help"}},
    })
    assert ev.id == "interaction-1"
    assert ev.scene == "c2c"
    assert ev.chat_id == "user-openid"
    assert ev.button_data == "cmd:/help"


@pytest.mark.asyncio
async def test_help_command(client, bob, agent_id):
    await _bind(client, bob, agent_id, "chat-1")
    driver = IMDriver(db=_state(client).db, manager=_state(client).manager,
                      skill_store=_state(client).skill_store)
    tc = FakeTelegramClient()
    await driver.handle_inbound(_ev("/help", "chat-1"), client=tc)
    assert any("pair" in s[1] for s in tc.sends)


@pytest.mark.asyncio
async def test_qq_help_command_sends_keyboard(client, bob, agent_id):
    s = _state(client)
    uid = _me_id(client, bob)
    await pairing.bind(
        s.db,
        "c2c:help-openid",
        user_id=uid,
        agent_id=agent_id,
        platform="qqbot",
    )
    driver = IMDriver(db=_state(client).db, manager=_state(client).manager,
                      skill_store=_state(client).skill_store)
    tc = FakeQQClient()
    await driver.handle_inbound(_qq_ev("/help", "c2c:help-openid"), client=tc)
    assert tc.keyboards[0][0] == "c2c:help-openid"
    assert tc.keyboards[0][2]["content"]["rows"][0]["buttons"][0]["action"]["data"] == "cmd:/new"


@pytest.mark.asyncio
async def test_feishu_help_command_sends_card(client, bob, agent_id):
    s = _state(client)
    uid = _me_id(client, bob)
    await pairing.bind(
        s.db,
        "p2p:ou-help",
        user_id=uid,
        agent_id=agent_id,
        platform="feishu",
    )
    driver = IMDriver(db=_state(client).db, manager=_state(client).manager,
                      skill_store=_state(client).skill_store)
    tc = FakeFeishuClient()
    await driver.handle_inbound(_feishu_ev("/help", "p2p:ou-help"), client=tc)
    assert tc.cards[0][0] == "p2p:ou-help"
    assert tc.cards[0][2]["header"]["title"]["content"] == "Sophagent 帮助"


@pytest.mark.asyncio
async def test_feishu_bound_chat_uses_buffered_send(client, bob, agent_id):
    s = _state(client)
    uid = _me_id(client, bob)
    await pairing.bind(
        s.db,
        "p2p:ou-1",
        user_id=uid,
        agent_id=agent_id,
        platform="feishu",
    )
    driver = IMDriver(
        db=s.db,
        manager=s.manager,
        skill_store=s.skill_store,
        transport_factory=lambda ev, c: BufferedSendTransport(ev.chat_id, c),
    )
    tc = FakeTelegramClient()
    await driver.handle_inbound(_feishu_ev("hello", "p2p:ou-1"), client=tc)
    row = await pairing.lookup(s.db, "p2p:ou-1", platform="feishu")
    task = s.manager._tasks.get(row["session_id"])
    if task is not None:
        await task
    assert tc.sends[-1] == ("p2p:ou-1", "echo: hello")


@pytest.mark.asyncio
async def test_feishu_attachment_persisted_in_platform_dir(client, bob, agent_id, tmp_path):
    s = _state(client)
    uid = _me_id(client, bob)
    await pairing.bind(
        s.db,
        "p2p:ou-2",
        user_id=uid,
        agent_id=agent_id,
        platform="feishu",
    )
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4")
    driver = IMDriver(
        db=s.db,
        manager=s.manager,
        skill_store=s.skill_store,
        transport_factory=lambda ev, c: BufferedSendTransport(ev.chat_id, c),
    )
    tc = FakeTelegramClient()
    ev = MessageEvent(
        text="[文件: doc.pdf]",
        chat_id="p2p:ou-2",
        from_id="ou-user",
        platform="feishu",
        attachments=[
            MessageAttachment(
                kind="file",
                path=str(src),
                name="doc.pdf",
                mime="application/pdf",
                size=8,
                platform="feishu",
            )
        ],
    )
    await driver.handle_inbound(ev, client=tc)
    row = await pairing.lookup(s.db, "p2p:ou-2", platform="feishu")
    task = s.manager._tasks.get(row["session_id"])
    if task is not None:
        await task
    messages = await s.db.load_messages(row["session_id"])
    user_msg = next(m for m in messages if m.role == "user")
    assert "[附加文件: im/feishu/doc.pdf]" in user_msg.content
    assert user_msg.attachments[0].path == "im/feishu/doc.pdf"


@pytest.mark.asyncio
async def test_qq_interaction_callback_runs_button_command(client, bob, agent_id):
    driver = IMDriver(db=_state(client).db, manager=_state(client).manager,
                      skill_store=_state(client).skill_store)
    tc = FakeQQClient()
    await _handle_interaction(tc, driver, {
        "id": "interaction-1",
        "chat_type": 2,
        "user_openid": "callback-openid",
        "data": {"resolved": {"button_data": "cmd:/help", "button_id": "help"}},
    })
    assert tc.acks == [("interaction-1", 0)]
    assert tc.keyboards[0][0] == "c2c:callback-openid"


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


@pytest.mark.asyncio
async def test_qq_bound_chat_uses_platform_binding_and_buffered_send(client, bob, agent_id):
    s = _state(client)
    uid = _me_id(client, bob)
    await pairing.bind(
        s.db,
        "c2c:openid-1",
        user_id=uid,
        agent_id=agent_id,
        platform="qqbot",
    )
    driver = IMDriver(
        db=s.db,
        manager=s.manager,
        skill_store=s.skill_store,
        transport_factory=lambda ev, c: BufferedSendTransport(ev.chat_id, c),
    )
    tc = FakeTelegramClient()
    await driver.handle_inbound(_qq_ev("hello", "c2c:openid-1"), client=tc)
    row = await pairing.lookup(s.db, "c2c:openid-1", platform="qqbot")
    task = s.manager._tasks.get(row["session_id"])
    if task is not None:
        await task
    assert tc.sends[-1] == ("c2c:openid-1", "echo: hello")


@pytest.mark.asyncio
async def test_im_attachment_persisted_in_message(client, bob, agent_id, tmp_path):
    s = _state(client)
    uid = _me_id(client, bob)
    await pairing.bind(
        s.db,
        "c2c:openid-2",
        user_id=uid,
        agent_id=agent_id,
        platform="qqbot",
    )
    src = tmp_path / "pic.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n")
    driver = IMDriver(
        db=s.db,
        manager=s.manager,
        skill_store=s.skill_store,
        transport_factory=lambda ev, c: BufferedSendTransport(ev.chat_id, c),
    )
    tc = FakeTelegramClient()
    ev = MessageEvent(
        text="[图片: pic.png]",
        chat_id="c2c:openid-2",
        from_id="qq-user",
        platform="qqbot",
        attachments=[
            MessageAttachment(
                kind="image",
                path=str(src),
                name="pic.png",
                mime="image/png",
                size=8,
                platform="qqbot",
            )
        ],
    )
    await driver.handle_inbound(ev, client=tc)
    row = await pairing.lookup(s.db, "c2c:openid-2", platform="qqbot")
    task = s.manager._tasks.get(row["session_id"])
    if task is not None:
        await task
    messages = await s.db.load_messages(row["session_id"])
    user_msg = next(m for m in messages if m.role == "user")
    assert "[附加文件: im/qqbot/pic.png]" in user_msg.content
    assert user_msg.attachments[0].kind == "image"
    assert user_msg.attachments[0].path == "im/qqbot/pic.png"
