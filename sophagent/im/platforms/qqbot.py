"""Minimal QQ Bot API v2 adapter.

Text-only MVP:
- receive C2C/group/guild text events over QQ Bot WebSocket Gateway
- send plain-text replies over QQ REST API
- hand events to the shared IMDriver used by Telegram
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import mimetypes
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..adapter import MessageEvent
from ...models import MessageAttachment
from .qqbot_keyboard import InlineKeyboard, interaction_text, parse_interaction_event

log = logging.getLogger(__name__)

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_PATH = "/gateway"

INTENT_C2C_GROUP_AT = 1 << 30
INTENT_PUBLIC_GUILD = 1 << 12
INTENT_DIRECT_MESSAGE = 1 << 26
INTENT_INTERACTION = 1 << 25
MSG_TYPE_TEXT = 0
MSG_TYPE_MARKDOWN = 2
FACE_TAG_RE = re.compile(r"<(?P<body>faceType=[^>]*)>")
FACE_FIELD_RE = re.compile(r'(?P<key>[A-Za-z_][\w]*)=(?:"(?P<quoted>[^"]*)"|(?P<bare>[^,>]+))')


class QQBotError(Exception):
    pass


@dataclass
class QQInbound:
    chat_id: str
    from_id: str
    text: str
    chat_type: str
    attachments: list[MessageAttachment]
    raw: dict[str, Any]


class QQBotClient:
    def __init__(
        self,
        app_id: str,
        client_secret: str,
        *,
        timeout: float = 30.0,
        markdown_support: bool = True,
    ) -> None:
        self.app_id = app_id
        self.client_secret = client_secret
        self.markdown_support = markdown_support
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=True)
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._seq: dict[str, int] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        resp = await self._client.post(
            TOKEN_URL,
            json={"appId": self.app_id, "clientSecret": self.client_secret},
        )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise QQBotError(f"QQ token response missing access_token: {data}")
        self._access_token = str(token)
        self._token_expires_at = time.time() + int(data.get("expires_in", 7200))
        return self._access_token

    async def _api(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        token = await self._ensure_token()
        resp = await self._client.request(
            method,
            f"{API_BASE}{path}",
            headers={"Authorization": f"QQBot {token}"},
            json=payload,
        )
        if resp.status_code >= 400:
            raise QQBotError(f"QQ API {method} {path} failed: {resp.status_code} {resp.text}")
        if not resp.content:
            return {}
        return resp.json()

    async def gateway_url(self) -> str:
        data = await self._api("GET", GATEWAY_PATH)
        url = data.get("url")
        if not url:
            raise QQBotError(f"QQ gateway response missing url: {data}")
        return str(url)

    def _media_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._access_token:
            headers["Authorization"] = f"QQBot {self._access_token}"
        return headers

    async def download_attachment(self, url: str, content_type: str, filename: str) -> MessageAttachment | None:
        if not url:
            return None
        if url.startswith("//"):
            url = "https:" + url
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return None
        try:
            resp = await self._client.get(url, headers=self._media_headers(), follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("qqbot attachment download failed url=%s error=%s", url[:80], exc)
            return None
        data = resp.content
        mime = (content_type or resp.headers.get("content-type") or "").split(";", 1)[0].strip()
        name = filename or Path(parsed.path).name or "qq_attachment"
        suffix = Path(name).suffix or mimetypes.guess_extension(mime) or ".bin"
        tmp = tempfile.NamedTemporaryFile(prefix="qqbot_", suffix=suffix, delete=False)
        try:
            tmp.write(data)
            tmp.close()
        except Exception:
            tmp.close()
            Path(tmp.name).unlink(missing_ok=True)
            raise
        return MessageAttachment(
            kind=_attachment_kind(mime, name),
            path=tmp.name,
            name=name,
            mime=mime or "application/octet-stream",
            size=len(data),
            platform="qqbot",
        )

    def _next_seq(self, key: str) -> int:
        value = self._seq.get(key, 0) + 1
        self._seq[key] = value
        return value

    def _message_path(self, chat_id: str) -> str:
        chat_type, target = _split_chat_id(chat_id)
        if chat_type == "group":
            return f"/v2/groups/{target}/messages"
        if chat_type == "guild":
            return f"/channels/{target}/messages"
        return f"/v2/users/{target}/messages"

    def _message_body(
        self,
        chat_id: str,
        text: str,
        *,
        keyboard: InlineKeyboard | None = None,
        force_text: bool = False,
    ) -> dict[str, Any]:
        msg_seq = self._next_seq(chat_id)
        chat_type, _ = _split_chat_id(chat_id)
        if self.markdown_support and not force_text and chat_type in {"c2c", "group"}:
            payload: dict[str, Any] = {
                "markdown": {"content": text},
                "msg_type": MSG_TYPE_MARKDOWN,
                "msg_seq": msg_seq,
            }
        else:
            payload = {
                "content": text,
                "msg_type": MSG_TYPE_TEXT,
                "msg_seq": msg_seq,
            }
        if keyboard is not None:
            payload["keyboard"] = keyboard.to_dict()
        return payload

    async def send_message(self, chat_id: str, text: str) -> int:
        path = self._message_path(chat_id)
        try:
            await self._api("POST", path, self._message_body(chat_id, text))
        except QQBotError:
            if not self.markdown_support:
                raise
            log.warning("qqbot markdown send failed chat=%s; falling back to text", chat_id)
            await self._api("POST", path, self._message_body(chat_id, text, force_text=True))
        return self._seq[chat_id]

    async def send_with_keyboard(self, chat_id: str, text: str, keyboard: InlineKeyboard) -> int:
        chat_type, _ = _split_chat_id(chat_id)
        if chat_type == "guild":
            await self.send_message(chat_id, text)
            return self._seq[chat_id]
        path = self._message_path(chat_id)
        try:
            await self._api("POST", path, self._message_body(chat_id, text, keyboard=keyboard))
        except QQBotError:
            if not self.markdown_support:
                raise
            log.warning("qqbot keyboard markdown send failed chat=%s; falling back to text", chat_id)
            await self._api("POST", path, self._message_body(
                chat_id, text, keyboard=keyboard, force_text=True,
            ))
        return self._seq[chat_id]

    async def ack_interaction(self, interaction_id: str, code: int = 0) -> None:
        if interaction_id:
            await self._api("PUT", f"/interactions/{interaction_id}", {"code": code})


def _chat_id(chat_type: str, raw_id: str) -> str:
    return f"{chat_type}:{raw_id}"


def _split_chat_id(chat_id: str) -> tuple[str, str]:
    if ":" not in chat_id:
        return "c2c", chat_id
    kind, raw = chat_id.split(":", 1)
    return kind or "c2c", raw


def _attachment_kind(content_type: str, filename: str = "") -> str:
    ct = (content_type or "").lower()
    suffix = Path(filename or "").suffix.lower()
    if ct == "voice" or ct.startswith("audio/") or suffix in {".silk", ".amr", ".mp3", ".wav", ".ogg", ".m4a"}:
        return "audio"
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("video/"):
        return "video"
    return "file"


def _decode_face_ext(ext: str) -> str:
    if not ext:
        return ""
    try:
        padded = ext + "=" * (-len(ext) % 4)
        raw = base64.b64decode(padded, validate=False)
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return ""
    if isinstance(data, dict):
        value = data.get("text") or data.get("desc") or data.get("name")
        return str(value).strip() if value else ""
    return ""


def _parse_face_tags(content: str) -> tuple[str, list[MessageAttachment]]:
    faces: list[MessageAttachment] = []

    def repl(match: re.Match[str]) -> str:
        body = match.group("body")
        fields = {
            m.group("key"): (m.group("quoted") if m.group("quoted") is not None else m.group("bare") or "")
            for m in FACE_FIELD_RE.finditer(body)
        }
        label = _decode_face_ext(fields.get("ext", ""))
        face_id = fields.get("faceId", "")
        name = label or (f"faceId={face_id}" if face_id else "表情包")
        faces.append(MessageAttachment(
            kind="emoji",
            path="",
            name=name,
            mime="qq/face",
            platform="qqbot",
            raw={
                "face_type": fields.get("faceType", ""),
                "face_id": face_id,
                "text": label,
            },
        ))
        return f"[表情: {name}]"

    return FACE_TAG_RE.sub(repl, content), faces


async def _parse_message_event(client: QQBotClient, event_type: str, data: dict[str, Any]) -> QQInbound | None:
    content, face_attachments = _parse_face_tags(str(data.get("content") or "").strip())
    author = data.get("author") or {}
    if event_type == "C2C_MESSAGE_CREATE":
        raw_chat_id = str(author.get("user_openid") or data.get("user_openid") or "")
        from_id = raw_chat_id
        chat_type = "c2c"
    elif event_type == "GROUP_AT_MESSAGE_CREATE":
        raw_chat_id = str(data.get("group_openid") or "")
        from_id = str(author.get("member_openid") or author.get("user_openid") or "")
        chat_type = "group"
    elif event_type in {"GUILD_MESSAGE_CREATE", "GUILD_AT_MESSAGE_CREATE"}:
        raw_chat_id = str(data.get("channel_id") or "")
        from_id = str(author.get("id") or "")
        chat_type = "guild"
    elif event_type == "DIRECT_MESSAGE_CREATE":
        raw_chat_id = str(data.get("guild_id") or data.get("channel_id") or "")
        from_id = str(author.get("id") or "")
        chat_type = "direct"
    else:
        return None
    if not raw_chat_id or not from_id:
        return None
    attachments = [*face_attachments, *await _process_attachments(client, data.get("attachments"))]
    notes: list[str] = []
    for att in attachments:
        if att.kind == "image":
            notes.append(f"[图片: {att.name or Path(att.path).name}]")
        elif att.kind == "audio":
            notes.append(f"[语音: {att.name or Path(att.path).name}]")
        elif att.kind == "video":
            notes.append(f"[视频: {att.name or Path(att.path).name}]")
        else:
            notes.append(f"[文件: {att.name or Path(att.path).name}]")
    if not content and not notes:
        return None
    text = "\n".join([p for p in [content, *notes] if p]).strip()
    return QQInbound(
        chat_id=_chat_id(chat_type, raw_chat_id),
        from_id=from_id,
        text=text,
        chat_type=chat_type,
        attachments=attachments,
        raw=data,
    )


async def _process_attachments(client: QQBotClient, attachments: Any) -> list[MessageAttachment]:
    if not isinstance(attachments, list):
        return []
    out: list[MessageAttachment] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        asr_text = item.get("asr_refer_text")
        if isinstance(asr_text, str) and asr_text.strip():
            out.append(MessageAttachment(
                kind="audio",
                path="",
                name=str(item.get("filename") or "voice"),
                mime=str(item.get("content_type") or "voice"),
                platform="qqbot",
                raw={"transcript": asr_text.strip()},
            ))
            continue
        att = await client.download_attachment(
            str(item.get("url") or item.get("voice_wav_url") or ""),
            str(item.get("content_type") or ""),
            str(item.get("filename") or ""),
        )
        if att is not None:
            out.append(att)
    return out


async def run_qqbot(client: QQBotClient, driver, *, allowed_user_ids: tuple[str, ...] = ()) -> None:
    """Run QQ Bot Gateway loop until cancelled."""
    allowed = {str(v) for v in allowed_user_ids}
    backoff = 2.0
    try:
        while True:
            try:
                url = await client.gateway_url()
                log.info("qqbot gateway connecting")
                await _run_once(client, driver, url, allowed)
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("qqbot gateway failed: %s; retry in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
    finally:
        await client.aclose()


async def _run_once(client: QQBotClient, driver, url: str, allowed: set[str]) -> None:
    import websockets

    async with websockets.connect(url) as ws:
        log.info("qqbot websocket connected")
        heartbeat_task: asyncio.Task | None = None
        last_seq: int | None = None
        try:
            async for raw in ws:
                msg = json.loads(raw)
                op = msg.get("op")
                if msg.get("s") is not None:
                    last_seq = int(msg["s"])
                if op == 10:
                    interval = float((msg.get("d") or {}).get("heartbeat_interval", 45000)) / 1000.0 * 0.8
                    await ws.send(json.dumps({
                        "op": 2,
                        "d": {
                            "token": f"QQBot {await client._ensure_token()}",
                            "intents": (
                                INTENT_INTERACTION
                                | INTENT_C2C_GROUP_AT
                                | INTENT_PUBLIC_GUILD
                                | INTENT_DIRECT_MESSAGE
                            ),
                            "shard": [0, 1],
                            "properties": {"os": "linux", "browser": "sophagent", "device": "sophagent"},
                        },
                    }))
                    log.info("qqbot identify sent")
                    heartbeat_task = asyncio.create_task(_heartbeat(ws, interval, lambda: last_seq))
                    continue
                if op != 0:
                    continue
                event_type = str(msg.get("t") or "")
                if event_type == "READY":
                    session_id = (msg.get("d") or {}).get("session_id")
                    log.info("qqbot ready session_id=%s", session_id)
                    continue
                if event_type == "INTERACTION_CREATE":
                    await _handle_interaction(client, driver, msg.get("d") or {}, allowed=allowed)
                    continue
                log.info("qqbot dispatch event=%s", event_type)
                inbound = await _parse_message_event(client, event_type, msg.get("d") or {})
                if inbound is None:
                    log.info("qqbot ignored event=%s (empty or unsupported)", event_type)
                    continue
                if allowed and inbound.from_id not in allowed:
                    log.info("qqbot ignored user=%s by allowlist", inbound.from_id)
                    continue
                log.info("qqbot inbound chat=%s from=%s text=%r", inbound.chat_id, inbound.from_id, inbound.text)
                ev = MessageEvent(
                    text=inbound.text,
                    chat_id=inbound.chat_id,
                    from_id=inbound.from_id,
                    platform="qqbot",
                    chat_type=inbound.chat_type,
                    attachments=inbound.attachments,
                    raw=inbound.raw,
                )
                await driver.handle_inbound(ev, client=client)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()


async def _heartbeat(ws, interval: float, seq_fn) -> None:
    while True:
        await asyncio.sleep(interval)
        await ws.send(json.dumps({"op": 1, "d": seq_fn()}))


async def _handle_interaction(
    client: QQBotClient,
    driver,
    data: dict[str, Any],
    *,
    allowed: set[str] | None = None,
) -> None:
    event = parse_interaction_event(data)
    try:
        await client.ack_interaction(event.id)
    except Exception as exc:
        log.warning("qqbot interaction ack failed id=%s error=%s", event.id, exc)
    if not event.chat_id:
        log.warning("qqbot interaction missing chat_id data=%s", data)
        return
    if allowed and event.operator_id not in allowed:
        log.info("qqbot ignored interaction user=%s by allowlist", event.operator_id)
        return
    text = interaction_text(event.button_data)
    chat_id = _chat_id(event.scene, event.chat_id)
    log.info(
        "qqbot interaction chat=%s operator=%s button=%r text=%r",
        chat_id, event.operator_id, event.button_data, text,
    )
    ev = MessageEvent(
        text=text,
        chat_id=chat_id,
        from_id=event.operator_id or event.chat_id,
        platform="qqbot",
        chat_type=event.scene,
        raw=event.raw,
    )
    await driver.handle_inbound(ev, client=client)
