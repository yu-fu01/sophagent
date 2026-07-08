"""DingTalk adapter: Stream Mode inbound + markdown / optional AI Card outbound."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from pathlib import Path

import httpx

from ....models import MessageAttachment
from ...adapter import MessageEvent
from .card import DingTalkCardClient
from .emotion import DingTalkEmotionClient
from .gating import compile_mention_patterns, should_process_group
from .health import (
    clear_health,
    mark_credentials_invalid,
    mark_lock_blocked,
    public_health_view,
)
from .lock import acquire_lock, release_lock
from .media import download_inbound_attachments, media_placeholder, message_has_media
from .webhook import assert_session_webhook_url, send_markdown

log = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 20000
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
_SESSION_WEBHOOKS_MAX = 500
_RECENT_MEDIA_TTL_SECONDS = 45.0
_LOCK_SCOPE = "dingtalk-stream"
_AUTH_ERROR_MARKERS = (
    "invalid", "unauthorized", "access_token", "appkey", "app secret",
    "forbidden", "401", "403", "credential",
)
_WEBHOOK_RE = re.compile(r"^https://(?:api|oapi)\.dingtalk\.com/")


def _looks_like_auth_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_ERROR_MARKERS)


def _stream_available() -> bool:
    try:
        import dingtalk_stream  # noqa: F401
        return True
    except ImportError:
        return False


@dataclass
class DingTalkConfig:
    client_id: str
    client_secret: str
    card_template_id: str
    robot_code: str = ""
    dm_policy: str = "pairing"
    require_mention: bool = True
    allowed_chat_ids: tuple[str, ...] = ()
    free_response_chats: tuple[str, ...] = ()
    mention_patterns: tuple[str, ...] = ()
    reply_emotion: bool = True


class _MessageDedup:
    def __init__(self, max_size: int = 1000) -> None:
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._max_size = max_size

    def is_duplicate(self, message_id: str) -> bool:
        if not message_id:
            return False
        now = time.monotonic()
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        while len(self._seen) > self._max_size:
            self._seen.popitem(last=False)
        return False


def _extract_text(message: Any) -> str:
    text = getattr(message, "text", None) or ""
    if hasattr(text, "content"):
        content = (text.content or "").strip()
    elif isinstance(text, dict):
        content = text.get("content", "").strip()
    else:
        content = str(text).strip()
    if content:
        return content
    rich_text = getattr(message, "rich_text_content", None) or getattr(message, "rich_text", None)
    if not rich_text:
        return ""
    rich_list = getattr(rich_text, "rich_text_list", None) or rich_text
    if not isinstance(rich_list, list):
        return ""
    parts: list[str] = []
    for item in rich_list:
        if isinstance(item, dict):
            t = item.get("text") or item.get("content") or ""
            if t:
                parts.append(str(t))
        elif hasattr(item, "text") and item.text:
            parts.append(str(item.text))
    return "\n".join(parts).strip()


class DingTalkClient:
    """Outbound client used by IMDriver and BufferedSendTransport."""

    def __init__(self, parent: "DingTalkAdapter") -> None:
        self._parent = parent

    async def send_message(self, chat_id: str, text: str) -> str:
        return await self._parent._send_text(chat_id, text)

    async def fire_turn_complete(self, chat_id: str) -> None:
        await self._parent._fire_done_reaction(chat_id)


class DingTalkAdapter:
    def __init__(self, config: DingTalkConfig, data_dir: Path) -> None:
        self.config = config
        self.data_dir = data_dir
        self._robot_code = (config.robot_code or config.client_id).strip()
        self._card = DingTalkCardClient(
            card_template_id=config.card_template_id,
            robot_code=self._robot_code,
        )
        self._emotion = DingTalkEmotionClient(robot_code=self._robot_code)
        self._http: httpx.AsyncClient | None = None
        self._stream_client: Any = None
        self._stream_task: asyncio.Task | None = None
        self._running = False
        self._dedup = _MessageDedup()
        self._session_webhooks: dict[str, tuple[str, int]] = {}
        self._message_contexts: dict[str, Any] = {}
        self._turn_source_messages: dict[str, Any] = {}
        self._recent_media_by_chat: dict[str, tuple[float, list[MessageAttachment]]] = {}
        self._done_emoji_fired: set[str] = set()
        self._mention_patterns = compile_mention_patterns(config.mention_patterns)
        self._allowed_chat_ids = {c for c in config.allowed_chat_ids if c}
        self._free_response_chats = {c for c in config.free_response_chats if c}
        self._inbound_dir = data_dir / "dingtalk" / "inbound"
        self._lock_held = False
        self.client = DingTalkClient(self)

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def aclose(self) -> None:
        self._running = False
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except (asyncio.CancelledError, Exception):
                pass
        self._stream_task = None
        websocket = getattr(self._stream_client, "websocket", None) if self._stream_client else None
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                pass
        if self._http:
            await self._http.aclose()
            self._http = None
        if self._lock_held:
            release_lock(self.data_dir, _LOCK_SCOPE, self.config.client_id)
            self._lock_held = False
        self._stream_client = None

    async def _get_access_token(self) -> str | None:
        if not self._stream_client:
            return None
        try:
            return await asyncio.to_thread(self._stream_client.get_access_token)
        except Exception as exc:
            log.error("dingtalk: get_access_token failed: %s", exc)
            return None

    def _get_valid_webhook(self, chat_id: str) -> str | None:
        info = self._session_webhooks.get(chat_id)
        if not info:
            return None
        webhook, expired_time_ms = info
        if expired_time_ms and expired_time_ms > 0:
            now_ms = int(time.time() * 1000)
            if now_ms + 5 * 60 * 1000 >= expired_time_ms:
                self._session_webhooks.pop(chat_id, None)
                return None
        return webhook

    async def _send_text(self, chat_id: str, text: str) -> str:
        if not text or not text.strip():
            return ""
        message = self._message_contexts.get(chat_id)
        token = await self._get_access_token()
        if token and message and self._card.available:
            out_track_id = await self._card.create_and_stream(
                access_token=token,
                message=message,
                content=text[:MAX_MESSAGE_LENGTH],
                finalize=True,
            )
            if out_track_id:
                await self._fire_done_reaction(chat_id)
                return out_track_id

        webhook = self._get_valid_webhook(chat_id)
        if not webhook:
            raise RuntimeError("No valid DingTalk session webhook for chat")
        await send_markdown(self._ensure_http(), webhook, text[:MAX_MESSAGE_LENGTH])
        await self._fire_done_reaction(chat_id)
        return uuid.uuid4().hex[:12]

    def _spawn_thinking_reaction(self, message: Any) -> None:
        if not self.config.reply_emotion:
            return
        msg_id = getattr(message, "message_id", "") or ""
        conversation_id = getattr(message, "conversation_id", "") or ""
        if not msg_id or not conversation_id:
            return
        robot_code = (getattr(message, "robot_code", None) or self.config.client_id).strip()

        async def _run() -> None:
            token = await self._get_access_token()
            if not token:
                return
            emotion = DingTalkEmotionClient(robot_code=robot_code)
            await emotion.send_emotion(
                access_token=token,
                open_msg_id=msg_id,
                open_conversation_id=conversation_id,
                emoji_name="🤔Thinking",
                recall=False,
            )

        asyncio.create_task(_run())

    async def _fire_done_reaction(self, chat_id: str) -> None:
        if not self.config.reply_emotion or chat_id in self._done_emoji_fired:
            return
        message = self._turn_source_messages.pop(chat_id, None) or self._message_contexts.get(chat_id)
        if not message:
            return
        msg_id = getattr(message, "message_id", "") or ""
        conversation_id = getattr(message, "conversation_id", "") or ""
        if not msg_id or not conversation_id:
            return
        self._done_emoji_fired.add(chat_id)
        token = await self._get_access_token()
        if not token:
            return
        robot_code = (getattr(message, "robot_code", None) or self.config.client_id).strip()
        emotion = DingTalkEmotionClient(robot_code=robot_code)
        await emotion.send_emotion(
            access_token=token,
            open_msg_id=msg_id,
            open_conversation_id=conversation_id,
            emoji_name="🤔Thinking",
            recall=True,
        )
        await emotion.send_emotion(
            access_token=token,
            open_msg_id=msg_id,
            open_conversation_id=conversation_id,
            emoji_name="🥳Done",
            recall=False,
        )

    def _match_allowed(self, sender_id: str, sender_staff_id: str, allowed: set[str]) -> bool:
        if not allowed:
            return True
        candidates = {v.lower() for v in (sender_id, sender_staff_id) if v}
        allowed_lower = {v.lower() for v in allowed}
        return bool(candidates & allowed_lower)

    def _should_process_group_message(
        self,
        message: Any,
        text: str,
        chat_id: str,
        *,
        is_group: bool,
        is_slash_command: bool,
    ) -> bool:
        return should_process_group(
            is_group=is_group,
            chat_id=chat_id,
            text=text,
            message=message,
            require_mention=self.config.require_mention,
            allowed_chat_ids=self._allowed_chat_ids,
            free_response_chats=self._free_response_chats,
            mention_patterns=self._mention_patterns,
            is_slash_command=is_slash_command,
        )

    async def _on_message(self, message: Any, driver, *, allowed: set[str]) -> None:
        msg_id = getattr(message, "message_id", None) or uuid.uuid4().hex
        if self._dedup.is_duplicate(str(msg_id)):
            return

        conversation_id = getattr(message, "conversation_id", "") or ""
        conversation_type = getattr(message, "conversation_type", "1")
        is_group = str(conversation_type) == "2"
        sender_id = getattr(message, "sender_id", "") or ""
        sender_staff_id = getattr(message, "sender_staff_id", "") or ""
        chat_id = conversation_id or sender_id
        if not chat_id:
            return

        text = _extract_text(message)
        is_slash_command = bool(text and text.startswith("/"))

        if self.config.dm_policy == "disabled":
            return
        if self.config.dm_policy == "allowlist":
            if not self._match_allowed(sender_id, sender_staff_id, allowed) and not is_slash_command:
                return
        elif allowed and not self._match_allowed(sender_id, sender_staff_id, allowed) and not is_slash_command:
            return

        if is_group and not self._should_process_group_message(
            message, text, chat_id, is_group=is_group, is_slash_command=is_slash_command,
        ):
            return

        session_webhook = getattr(message, "session_webhook", None) or ""
        expired_time = int(getattr(message, "session_webhook_expired_time", 0) or 0)
        if session_webhook and _WEBHOOK_RE.match(session_webhook):
            if len(self._session_webhooks) >= _SESSION_WEBHOOKS_MAX:
                try:
                    self._session_webhooks.pop(next(iter(self._session_webhooks)))
                except StopIteration:
                    pass
            try:
                assert_session_webhook_url(session_webhook)
                self._session_webhooks[chat_id] = (session_webhook, expired_time)
            except ValueError:
                log.warning("dingtalk: rejected session webhook url")

        self._message_contexts[chat_id] = message
        self._done_emoji_fired.discard(chat_id)

        token = await self._get_access_token()
        attachments: list[MessageAttachment] = []
        if token:
            attachments = await download_inbound_attachments(
                self._ensure_http(),
                message,
                inbound_dir=self._inbound_dir,
                access_token=token,
                client_id=self.config.client_id,
            )

        if not text and not attachments:
            if message_has_media(message):
                text = media_placeholder(message)
            else:
                return

        if text and not attachments:
            recent = self._recent_media_by_chat.get(chat_id)
            if recent and (time.monotonic() - recent[0]) <= _RECENT_MEDIA_TTL_SECONDS:
                attachments = list(recent[1])

        if attachments:
            self._recent_media_by_chat[chat_id] = (time.monotonic(), list(attachments))

        self._turn_source_messages[chat_id] = message
        self._spawn_thinking_reaction(message)

        if not text and attachments:
            if any(a.kind == "image" for a in attachments):
                text = "[图片]"
            elif any(a.kind == "audio" for a in attachments):
                text = "[语音]"
            else:
                text = "[文件]"

        ev = MessageEvent(
            text=text,
            chat_id=chat_id,
            from_id=sender_staff_id or sender_id,
            platform="dingtalk",
            chat_type="group" if is_group else "dm",
            attachments=attachments or None,
            raw=message,
        )
        log.info(
            "dingtalk inbound from=%s chat=%s group=%s type=%s attachments=%d text=%s",
            (sender_staff_id or sender_id)[:8],
            chat_id[:8],
            is_group,
            getattr(message, "message_type", "?"),
            len(attachments),
            (text[:40] + "…") if len(text) > 40 else text,
        )
        await driver.handle_inbound(ev, client=self.client)

    async def _run_stream(self) -> None:
        import dingtalk_stream

        backoff_idx = 0
        while self._running:
            try:
                await self._stream_client.start()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if not self._running:
                    return
                if _looks_like_auth_error(exc):
                    mark_credentials_invalid(
                        self.data_dir,
                        self.config.client_id,
                        detail=f"钉钉 Stream 认证失败：{str(exc)[:200]}",
                    )
                    self._running = False
                    return
                log.warning("dingtalk: stream error: %s", exc)
            if not self._running:
                return
            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            log.info("dingtalk: reconnecting in %ds", delay)
            await asyncio.sleep(delay)
            backoff_idx += 1

    async def run(self, driver, *, allowed_user_ids: tuple[str, ...] = ()) -> None:
        if not _stream_available():
            log.error("dingtalk-stream not installed")
            return
        if not self.config.client_id or not self.config.client_secret:
            log.error("dingtalk: missing client_id/client_secret")
            return

        import dingtalk_stream
        from dingtalk_stream import AckMessage, CallbackMessage, ChatbotMessage

        allowed = {str(v).strip() for v in allowed_user_ids if str(v).strip()}
        self._running = True

        acquired, existing = acquire_lock(
            self.data_dir,
            _LOCK_SCOPE,
            self.config.client_id,
            metadata={"client_id": self.config.client_id[:8]},
        )
        if not acquired:
            holder_pid = None
            if existing:
                try:
                    holder_pid = int(existing.get("pid"))
                except (TypeError, ValueError):
                    holder_pid = None
            mark_lock_blocked(self.data_dir, self.config.client_id, holder_pid=holder_pid)
            log.error(
                "dingtalk: stream lock held by pid=%s; connection not started",
                holder_pid,
            )
            return
        self._lock_held = True
        health = public_health_view(self.data_dir, self.config.client_id)
        if health.get("status") == "lock_blocked":
            clear_health(self.data_dir, self.config.client_id)

        credential = dingtalk_stream.Credential(self.config.client_id, self.config.client_secret)
        self._stream_client = dingtalk_stream.DingTalkStreamClient(credential)
        try:
            token = await asyncio.to_thread(self._stream_client.get_access_token)
            if not token:
                mark_credentials_invalid(self.data_dir, self.config.client_id)
                return
        except Exception as exc:
            mark_credentials_invalid(
                self.data_dir,
                self.config.client_id,
                detail=f"获取 access_token 失败：{str(exc)[:200]}",
            )
            return

        adapter = self

        class _IncomingHandler(dingtalk_stream.ChatbotHandler):
            def pre_start(self) -> None:
                return

            async def process(self, message: CallbackMessage):
                try:
                    data = message.data
                    if isinstance(data, str):
                        data = json.loads(data)
                    chatbot_msg = ChatbotMessage.from_dict(data)
                    if not getattr(chatbot_msg, "session_webhook", None) and isinstance(data, dict):
                        webhook = data.get("sessionWebhook") or data.get("session_webhook") or ""
                        if webhook:
                            chatbot_msg.session_webhook = webhook
                    if not getattr(chatbot_msg, "is_in_at_list", False) and isinstance(data, dict):
                        if data.get("isInAtList"):
                            chatbot_msg.is_in_at_list = True
                    asyncio.create_task(adapter._safe_on_message(chatbot_msg, driver, allowed=allowed))
                except Exception:
                    log.exception("dingtalk: incoming handler error")
                    return AckMessage.STATUS_SYSTEM_EXCEPTION, "error"
                return AckMessage.STATUS_OK, "OK"

        handler = _IncomingHandler()
        self._stream_client.register_callback_handler(ChatbotMessage.TOPIC, handler)
        self._stream_task = asyncio.create_task(self._run_stream())
        log.info("dingtalk stream started client_id=%s", self.config.client_id[:8])
        try:
            await self._stream_task
        except asyncio.CancelledError:
            pass
        finally:
            log.info("dingtalk stream stopped")

    async def _safe_on_message(self, message: Any, driver, *, allowed: set[str]) -> None:
        try:
            await self._on_message(message, driver, allowed=allowed)
        except Exception:
            log.exception("dingtalk: handle_inbound failed")


async def run_dingtalk(
    config: DingTalkConfig,
    driver,
    data_dir: Path,
    *,
    allowed_user_ids: tuple[str, ...] = (),
) -> None:
    adapter = DingTalkAdapter(config, data_dir)
    try:
        await adapter.run(driver, allowed_user_ids=allowed_user_ids)
    finally:
        await adapter.aclose()
