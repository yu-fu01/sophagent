"""Feishu adapter parsing and webhook tests."""

import json
from types import SimpleNamespace

import pytest

from sophagent.im.platforms.feishu_parse import (
    parse_card_action_data,
    parse_message_event_data,
)


def _sample_text_event(*, chat_type="p2p", content='{"text":"hello"}', mentions=None):
    return {
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_user", "user_id": "u1"},
                "sender_type": "user",
            },
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_chat",
                "chat_type": chat_type,
                "message_type": "text",
                "content": content,
                "mentions": mentions or [],
            },
        }
    }


def test_parse_feishu_text_message_p2p():
    inbound = parse_message_event_data(_sample_text_event())
    assert inbound is not None
    assert inbound.chat_id == "p2p:ou_user"
    assert inbound.from_id == "ou_user"
    assert inbound.text == "hello"
    assert inbound.chat_type == "p2p"


def test_parse_feishu_group_requires_mention_by_default():
    inbound = parse_message_event_data(_sample_text_event(chat_type="group"))
    assert inbound is None


def test_parse_feishu_group_with_bot_mention():
    inbound = parse_message_event_data(
        _sample_text_event(
            chat_type="group",
            mentions=[{"key": "@bot", "id": {"open_id": "ou_bot"}}],
        ),
        bot_open_id="ou_bot",
    )
    assert inbound is not None
    assert inbound.chat_id == "chat:oc_chat"
    assert inbound.text == "hello"


def test_parse_feishu_image_attachment():
    inbound = parse_message_event_data({
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user"}, "sender_type": "user"},
            "message": {
                "message_id": "om_img",
                "chat_id": "oc_chat",
                "chat_type": "p2p",
                "message_type": "image",
                "content": '{"image_key":"img_123"}',
            },
        }
    })
    assert inbound is not None
    assert inbound.text == "[图片]"
    assert inbound.attachments[0].kind == "image"
    assert inbound.attachments[0].raw["file_key"] == "img_123"


def test_parse_feishu_audio_attachment():
    inbound = parse_message_event_data({
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user"}, "sender_type": "user"},
            "message": {
                "message_id": "om_audio",
                "chat_id": "oc_chat",
                "chat_type": "p2p",
                "message_type": "audio",
                "content": '{"file_key":"aud_123","duration":2}',
            },
        }
    })
    assert inbound is not None
    assert inbound.text == ""
    assert inbound.attachments[0].kind == "audio"
    assert inbound.attachments[0].raw["file_key"] == "aud_123"


def test_parse_feishu_post_emotion_tag():
    inbound = parse_message_event_data({
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user"}, "sender_type": "user"},
            "message": {
                "message_id": "om_post",
                "chat_id": "oc_chat",
                "chat_type": "p2p",
                "message_type": "post",
                "content": json.dumps({
                    "zh_cn": {
                        "title": "",
                        "content": [[
                            {"tag": "emotion", "emoji_type": "OK"},
                            {"tag": "text", "text": "这个表情包是啥意思"},
                        ]],
                    }
                }),
            },
        }
    })
    assert inbound is not None
    assert "[表情: OK]" in inbound.text
    assert "这个表情包是啥意思" in inbound.text


def test_parse_feishu_card_action_command():
    inbound = parse_card_action_data({
        "event": {
            "operator": {"operator_id": {"open_id": "ou_user"}},
            "context": {"open_chat_id": "oc_group"},
            "action": {"value": {"cmd": "cmd:/new"}},
        }
    })
    assert inbound is not None
    assert inbound.text == "/new"
    assert inbound.chat_id == "chat:oc_group"


def test_parse_feishu_sdk_like_message_event():
    data = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_user", user_id="u1"),
                sender_type="user",
            ),
            message=SimpleNamespace(
                message_id="om_1",
                chat_id="oc_chat",
                chat_type="p2p",
                message_type="text",
                content='{"text":"<at user_id=\\"ou_bot\\">hermes</at> /pair b4caa2d2"}',
                mentions=[],
            ),
        )
    )
    inbound = parse_message_event_data(data)
    assert inbound is not None
    assert inbound.text == "/pair b4caa2d2"
    assert inbound.from_id == "ou_user"


def test_parse_feishu_sdk_like_group_mention():
    data = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_user"),
                sender_type="user",
            ),
            message=SimpleNamespace(
                message_id="om_2",
                chat_id="oc_group",
                chat_type="group",
                message_type="text",
                content='{"text":"hello"}',
                mentions=[
                    SimpleNamespace(
                        key="@bot",
                        id=SimpleNamespace(open_id="ou_bot"),
                    )
                ],
            ),
        )
    )
    inbound = parse_message_event_data(data, bot_open_id="ou_bot")
    assert inbound is not None
    assert inbound.chat_id == "chat:oc_group"


@pytest.mark.asyncio
async def test_feishu_webhook_url_verification(client):
    r = client.post("/api/feishu/webhook", json={"type": "url_verification", "challenge": "abc123"})
    assert r.status_code == 200
    assert r.json()["challenge"] == "abc123"
