"""Feishu/Lark inbound message parsing helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ...models import MessageAttachment

_MENTION_RE = re.compile(r"@_all|@[^\s@]+")
_AT_TAG_RE = re.compile(r"<at[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


@dataclass
class FeishuInbound:
    chat_id: str
    from_id: str
    text: str
    chat_type: str
    message_id: str = ""
    attachments: list[MessageAttachment] | None = None
    raw: dict[str, Any] | None = None


def _chat_id(chat_type: str, chat_id: str, sender_open_id: str) -> str:
    if chat_type == "p2p":
        return f"p2p:{sender_open_id or chat_id}"
    return f"chat:{chat_id}"


def _load_content(raw_content: str) -> dict[str, Any]:
    if not raw_content:
        return {}
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        return {"text": raw_content}
    return parsed if isinstance(parsed, dict) else {"content": parsed}


def _strip_mentions(text: str) -> str:
    cleaned = _AT_TAG_RE.sub("", text or "")
    cleaned = _MENTION_RE.sub("", cleaned)
    return " ".join(cleaned.split()).strip()


def _parse_text_content(payload: dict[str, Any]) -> str:
    return _strip_mentions(str(payload.get("text") or ""))


def _parse_post_content(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    for locale in ("zh_cn", "en_us", "ja_jp"):
        block = payload.get(locale)
        if not isinstance(block, dict):
            continue
        title = str(block.get("title") or "").strip()
        if title:
            lines.append(title)
        content = block.get("content")
        if not isinstance(content, list):
            continue
        for row in content:
            if not isinstance(row, list):
                continue
            parts: list[str] = []
            for item in row:
                if not isinstance(item, dict):
                    continue
                tag = str(item.get("tag") or "")
                if tag == "text":
                    parts.append(str(item.get("text") or ""))
                elif tag == "a":
                    parts.append(str(item.get("text") or item.get("href") or ""))
                elif tag == "at":
                    parts.append(str(item.get("user_name") or "@user"))
                elif tag in {"emotion", "emoji"}:
                    label = str(item.get("text") or item.get("emoji_type") or "").strip()
                    parts.append(f"[表情: {label or '表情包'}]")
            line = _strip_mentions("".join(parts).strip())
            if line:
                lines.append(line)
        if lines:
            break
    return "\n".join(lines).strip()


def _attachments_from_message(message_type: str, payload: dict[str, Any]) -> list[MessageAttachment]:
    msg_type = (message_type or "").lower()
    if msg_type == "image":
        image_key = str(payload.get("image_key") or "").strip()
        if image_key:
            return [MessageAttachment(
                kind="image",
                path="",
                name=image_key,
                mime="image/jpeg",
                platform="feishu",
                raw={"file_key": image_key},
            )]
    if msg_type in {"file", "audio", "media"}:
        file_key = str(payload.get("file_key") or "").strip()
        name = str(payload.get("file_name") or payload.get("name") or file_key or "file")
        if msg_type == "audio":
            kind = "audio"
        elif msg_type == "media":
            kind = "audio" if str(payload.get("type") or "").lower() == "audio" else "file"
        else:
            kind = "file"
        if file_key:
            return [MessageAttachment(
                kind=kind,
                path="",
                name=name,
                platform="feishu",
                raw={"file_key": file_key, "message_type": msg_type},
            )]
    return []


def mentions_bot(message: Any, bot_open_id: str = "") -> bool:
    mentions = _field(message, "mentions")
    if not isinstance(mentions, list):
        return False
    bot_ids = {bot_open_id} if bot_open_id else set()
    for item in mentions:
        key = str(_field(item, "key") or "")
        if key == "@_all":
            return True
        mention_id = _field(item, "id")
        open_id = str(_field(mention_id, "open_id") or "")
        if bot_open_id and open_id == bot_open_id:
            return True
        if open_id in bot_ids:
            return True
    content = str(_field(message, "content") or "")
    if "@_all" in content:
        return True
    return False


def parse_message_event_data(
    data: Any,
    *,
    bot_open_id: str = "",
    require_mention_in_group: bool = True,
) -> FeishuInbound | None:
    event = _field(data, "event")
    if event is None and isinstance(data, dict) and ("message" in data or "sender" in data):
        event = data
    if event is None:
        return None
    message = _field(event, "message")
    sender = _field(event, "sender")
    if message is None or sender is None:
        return None

    sender_ids = _field(sender, "sender_id")
    from_id = str(
        _field(sender_ids, "open_id")
        or _field(sender_ids, "user_id")
        or _field(sender, "open_id")
        or ""
    ).strip()
    chat_type = str(_field(message, "chat_type") or "p2p").strip().lower()
    raw_chat_id = str(_field(message, "chat_id") or "").strip()
    message_id = str(_field(message, "message_id") or "").strip()
    message_type = str(_field(message, "message_type") or "text").strip().lower()
    payload = _load_content(str(_field(message, "content") or ""))

    if chat_type != "p2p" and require_mention_in_group and not mentions_bot(message, bot_open_id):
        return None
    if str(_field(sender, "sender_type") or "").lower() == "app":
        return None

    if message_type == "text":
        text = _parse_text_content(payload)
    elif message_type == "post":
        text = _parse_post_content(payload)
    elif message_type == "image":
        text = _parse_text_content(payload) or "[图片]"
    elif message_type == "audio":
        text = ""
    elif message_type in {"file", "media"}:
        if message_type == "media" and payload.get("file_key"):
            text = ""
        else:
            text = str(payload.get("file_name") or payload.get("name") or f"[{message_type}]")
    else:
        text = _parse_text_content(payload)

    attachments = _attachments_from_message(message_type, payload)
    if not text and not attachments:
        return None

    chat_id = _chat_id(chat_type, raw_chat_id, from_id)
    return FeishuInbound(
        chat_id=chat_id,
        from_id=from_id,
        text=text,
        chat_type=chat_type,
        message_id=message_id,
        attachments=attachments,
        raw=data,
    )


def parse_card_action_data(data: Any) -> FeishuInbound | None:
    event = _field(data, "event")
    if event is None and isinstance(data, dict):
        event = data
    if event is None:
        return None
    action = _field(event, "action")
    operator = _field(event, "operator")
    context = _field(event, "context")
    if action is None:
        return None
    operator_ids = _field(operator, "operator_id")
    from_id = str(_field(operator_ids, "open_id") or _field(operator_ids, "user_id") or "").strip()
    ctx = context if isinstance(context, dict) else context
    open_chat_id = str(_field(ctx, "open_chat_id") or _field(ctx, "chat_id") or "").strip()
    open_message_id = str(_field(ctx, "open_message_id") or _field(ctx, "message_id") or "").strip()
    value = _field(action, "value")
    button_data = ""
    if isinstance(value, dict):
        button_data = str(value.get("cmd") or value.get("text") or value.get("data") or "")
    elif value is not None:
        button_data = str(value)
    if button_data.startswith("cmd:"):
        text = button_data[4:].strip()
    elif button_data.startswith("text:"):
        text = button_data[5:].strip()
    elif button_data:
        text = f"[按钮点击: {button_data}]"
    else:
        text = "[按钮点击]"
    if not from_id:
        return None
    chat_type = "group" if open_chat_id else "p2p"
    chat_id = _chat_id(chat_type, open_chat_id, from_id)
    return FeishuInbound(
        chat_id=chat_id,
        from_id=from_id,
        text=text,
        chat_type=chat_type,
        message_id=open_message_id,
        attachments=[],
        raw=data,
    )
