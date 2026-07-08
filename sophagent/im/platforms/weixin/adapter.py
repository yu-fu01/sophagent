"""Weixin (personal WeChat) adapter via Tencent iLink Bot API."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from ....models import MessageAttachment
from ...adapter import MessageEvent
from .cdn import WEIXIN_CDN_BASE_URL
from .formatting import split_text_for_delivery, truncate_message, wrap_copy_friendly_lines
from .media import (
    EP_GET_CONFIG,
    EP_SEND_TYPING,
    TYPING_START,
    TYPING_STOP,
    TypingTicketCache,
    download_inbound_attachments,
    extract_outbound_file_paths,
    send_file,
)
from .health import clear_health, mark_lock_blocked, mark_session_expired, public_health_view
from .lock import acquire_lock, release_lock
from .paths import resolve_outbound_path
from .store import ContextTokenStore, load_account, load_sync_buf, save_sync_buf

log = logging.getLogger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2
BACKOFF_DELAY_SECONDS = 30
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2
MESSAGE_DEDUP_TTL_SECONDS = 300
MAX_MESSAGE_LENGTH = 2000
TEXT_BATCH_DELAY_SECONDS = 3.0
TEXT_BATCH_SPLIT_DELAY_SECONDS = 5.0
SEND_CHUNK_DELAY_SECONDS = 1.5
SPLIT_THRESHOLD = 1800

ITEM_TEXT = 1
ITEM_VOICE = 3
ITEM_IMAGE = 2
ITEM_FILE = 4
ITEM_VIDEO = 5
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2


class WeixinError(Exception):
    pass


class MessageDeduplicator:
    def __init__(self, max_size: int = 2000, ttl_seconds: float = MESSAGE_DEDUP_TTL_SECONDS) -> None:
        self._seen: dict[str, float] = {}
        self._max_size = max_size
        self._ttl = ttl_seconds

    def is_duplicate(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        now = time.time()
        if msg_id in self._seen:
            if now - self._seen[msg_id] < self._ttl:
                return True
            del self._seen[msg_id]
        if len(self._seen) >= self._max_size:
            cutoff = now - self._ttl
            self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        self._seen[msg_id] = now
        return False


@dataclass
class _PendingTextBatch:
    text: str
    message: dict[str, Any]
    attachments: list[MessageAttachment] = field(default_factory=list)


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _base_info() -> dict[str, Any]:
    return {"channel_version": CHANNEL_VERSION}


def _headers(token: str | None, body: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _is_stale_session_ret(ret: int | None, errcode: int | None, errmsg: str | None) -> bool:
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False
    return (errmsg or "").lower() == "unknown error"


def extract_text(item_list: list[dict[str, Any]]) -> str:
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            ref_type = ref_item.get("type")
            if ref_type in {ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE}:
                title = ref.get("title") or ""
                prefix = f"[引用媒体: {title}]\n" if title else "[引用媒体]\n"
                return f"{prefix}{text}".strip()
            if ref_item:
                parts: list[str] = []
                if ref.get("title"):
                    parts.append(str(ref["title"]))
                ref_text = extract_text([ref_item])
                if ref_text:
                    parts.append(ref_text)
                if parts:
                    return f"[引用: {' | '.join(parts)}]\n{text}".strip()
            return text
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            voice_text = str((item.get("voice_item") or {}).get("text") or "")
            if voice_text:
                return voice_text
    return ""


def _guess_chat_type(message: dict[str, Any], account_id: str) -> tuple[str, str]:
    room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
    to_user_id = str(message.get("to_user_id") or "").strip()
    is_group = bool(room_id) or (
        to_user_id and account_id and to_user_id != account_id and message.get("msg_type") == 1
    )
    if is_group:
        return "group", room_id or to_user_id or str(message.get("from_user_id") or "")
    return "dm", str(message.get("from_user_id") or "")


async def _api_post(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    endpoint: str,
    payload: dict[str, Any],
    token: str | None,
    timeout_ms: int,
) -> dict[str, Any]:
    body = _json_dumps({**payload, "base_info": _base_info()})
    url = f"{base_url.rstrip('/')}/{endpoint}"

    async def _do() -> dict[str, Any]:
        async with session.post(url, data=body, headers=_headers(token, body)) as response:
            raw = await response.text()
            if not response.ok:
                raise WeixinError(f"iLink POST {endpoint} HTTP {response.status}: {raw[:200]}")
            return json.loads(raw)

    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


async def _get_updates(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    sync_buf: str,
    timeout_ms: int,
) -> dict[str, Any]:
    try:
        return await _api_post(
            session,
            base_url=base_url,
            endpoint=EP_GET_UPDATES,
            payload={"get_updates_buf": sync_buf},
            token=token,
            timeout_ms=timeout_ms,
        )
    except asyncio.TimeoutError:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


async def _send_message_api(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    to: str,
    text: str,
    context_token: str | None,
    client_id: str,
) -> dict[str, Any]:
    if not text or not text.strip():
        raise ValueError("send_message: text must not be empty")
    message: dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        message["context_token"] = context_token
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_MESSAGE,
        payload={"msg": message},
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )


@dataclass
class WeixinConfig:
    account_id: str
    token: str
    base_url: str = ILINK_BASE_URL
    cdn_base_url: str = WEIXIN_CDN_BASE_URL
    split_multiline: bool = False
    send_chunk_delay_seconds: float = SEND_CHUNK_DELAY_SECONDS


class WeixinClient:
    """iLink Bot API client for inbound polling and outbound text/media."""

    def __init__(
        self,
        config: WeixinConfig,
        data_dir: Path,
        *,
        group_policy: str = "disabled",
        dm_policy: str = "pairing",
    ) -> None:
        self.config = config
        self.data_dir = data_dir
        self.group_policy = group_policy
        self.dm_policy = (dm_policy or "pairing").strip().lower()
        self._token_store = ContextTokenStore(data_dir)
        self._typing_cache = TypingTicketCache()
        self._dedup = MessageDeduplicator()
        self._session: aiohttp.ClientSession | None = None
        self._send_gate = asyncio.Lock()
        self._running = False
        self._inbound_dir = data_dir / "weixin" / "inbound"
        self._pending_text: dict[str, _PendingTextBatch] = {}
        self._pending_tasks: dict[str, asyncio.Task] = {}
        self._lock_scope = "weixin-bot-token"
        self._lock_held = False

    async def aclose(self) -> None:
        self._running = False
        for task in self._pending_tasks.values():
            if not task.done():
                task.cancel()
        self._pending_text.clear()
        self._pending_tasks.clear()
        if self._lock_held:
            release_lock(self.data_dir, self._lock_scope, self.config.token)
            self._lock_held = False
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _outbound_allowed_roots(self) -> list[Path]:
        return [
            self.data_dir / "workspaces",
            self.data_dir / "weixin",
            self.data_dir,
        ]

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=None, sock_read=None)
            self._session = aiohttp.ClientSession(trust_env=True, timeout=timeout)
        return self._session

    async def send_message(self, chat_id: str, text: str) -> int:
        if not text or not text.strip():
            return 0
        session = self._ensure_session()
        cleaned, file_paths = extract_outbound_file_paths(text)
        chunks = split_text_for_delivery(
            wrap_copy_friendly_lines(cleaned),
            MAX_MESSAGE_LENGTH,
            split_multiline=self.config.split_multiline,
        )
        for idx, chunk in enumerate(chunks):
            await self._send_chunk(session, chat_id, chunk)
            if idx < len(chunks) - 1 and self.config.send_chunk_delay_seconds > 0:
                await asyncio.sleep(self.config.send_chunk_delay_seconds)
        account_id = self.config.account_id
        context_token = self._token_store.get(account_id, chat_id)
        for rel in file_paths:
            path = resolve_outbound_path(str(rel), allowed_roots=self._outbound_allowed_roots())
            if path is None:
                log.warning("weixin: rejected outbound path outside allowed roots path=%s", rel)
                continue
            try:
                await send_file(
                    session,
                    base_url=self.config.base_url,
                    token=self.config.token,
                    cdn_base_url=self.config.cdn_base_url,
                    chat_id=chat_id,
                    path=path,
                    context_token=context_token,
                    api_post=_api_post,
                )
            except Exception as exc:
                log.warning("weixin: outbound file send failed path=%s: %s", rel, exc)
        return 0

    async def send_typing(self, chat_id: str) -> None:
        ticket = await self._ensure_typing_ticket(chat_id)
        if not ticket:
            return
        session = self._ensure_session()
        try:
            await _api_post(
                session,
                base_url=self.config.base_url,
                endpoint=EP_SEND_TYPING,
                payload={"ilink_user_id": chat_id, "typing_ticket": ticket, "status": TYPING_START},
                token=self.config.token,
                timeout_ms=CONFIG_TIMEOUT_MS,
            )
        except Exception as exc:
            log.debug("weixin: typing start failed chat=%s: %s", chat_id[:8], exc)

    async def stop_typing(self, chat_id: str) -> None:
        ticket = await self._ensure_typing_ticket(chat_id)
        if not ticket:
            return
        session = self._ensure_session()
        try:
            await _api_post(
                session,
                base_url=self.config.base_url,
                endpoint=EP_SEND_TYPING,
                payload={"ilink_user_id": chat_id, "typing_ticket": ticket, "status": TYPING_STOP},
                token=self.config.token,
                timeout_ms=CONFIG_TIMEOUT_MS,
            )
        except Exception as exc:
            log.debug("weixin: typing stop failed chat=%s: %s", chat_id[:8], exc)

    async def _ensure_typing_ticket(self, chat_id: str) -> str | None:
        ticket = self._typing_cache.get(chat_id)
        if ticket:
            return ticket
        session = self._ensure_session()
        context_token = self._token_store.get(self.config.account_id, chat_id)
        try:
            resp = await _api_post(
                session,
                base_url=self.config.base_url,
                endpoint=EP_GET_CONFIG,
                payload={"ilink_user_id": chat_id, **({"context_token": context_token} if context_token else {})},
                token=self.config.token,
                timeout_ms=CONFIG_TIMEOUT_MS,
            )
            ticket = str(resp.get("typing_ticket") or "")
            if ticket:
                self._typing_cache.set(chat_id, ticket)
                return ticket
        except Exception as exc:
            log.debug("weixin: getConfig failed chat=%s: %s", chat_id[:8], exc)
        return None

    async def _send_chunk(self, session: aiohttp.ClientSession, chat_id: str, chunk: str) -> None:
        account_id = self.config.account_id
        context_token = self._token_store.get(account_id, chat_id)
        client_id = str(uuid.uuid4())
        retried_without_token = False
        last_error: Exception | None = None

        async with self._send_gate:
            for attempt in range(4):
                try:
                    resp = await _send_message_api(
                        session,
                        base_url=self.config.base_url,
                        token=self.config.token,
                        to=chat_id,
                        text=chunk,
                        context_token=context_token,
                        client_id=client_id,
                    )
                    ret = resp.get("ret", 0)
                    errcode = resp.get("errcode", 0)
                    if ret in {0, None} and errcode in {0, None}:
                        return
                    is_session_expired = (
                        ret == SESSION_EXPIRED_ERRCODE
                        or errcode == SESSION_EXPIRED_ERRCODE
                        or _is_stale_session_ret(ret, errcode, resp.get("errmsg"))
                    )
                    if is_session_expired and not retried_without_token and context_token:
                        retried_without_token = True
                        context_token = None
                        self._token_store.clear(account_id, chat_id)
                        log.warning("weixin: session expired for %s; retrying without context_token", chat_id[:8])
                        continue
                    last_error = WeixinError(
                        f"sendmessage failed ret={ret} errcode={errcode} errmsg={resp.get('errmsg', '')}"
                    )
                    if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE:
                        await asyncio.sleep(1.0 * (attempt + 1))
                        continue
                    raise last_error
                except asyncio.TimeoutError as exc:
                    last_error = exc
                    await asyncio.sleep(1.0 * (attempt + 1))
                except WeixinError:
                    raise
                except Exception as exc:
                    last_error = exc
                    await asyncio.sleep(1.0 * (attempt + 1))
            if last_error:
                raise last_error

    def _batch_key(self, sender_id: str) -> str:
        return sender_id

    def _enqueue_text(
        self,
        driver,
        *,
        sender_id: str,
        text: str,
        message: dict[str, Any],
        attachments: list[MessageAttachment],
        allowed: set[str],
    ) -> None:
        key = self._batch_key(sender_id)
        existing = self._pending_text.get(key)
        if existing:
            existing.text = f"{existing.text}\n{text}".strip()
            existing.attachments.extend(attachments)
        else:
            self._pending_text[key] = _PendingTextBatch(text=text, message=message, attachments=attachments)
        task = self._pending_tasks.get(key)
        if task and not task.done():
            task.cancel()
        self._pending_tasks[key] = asyncio.create_task(
            self._flush_text_batch(driver, key, allowed=allowed),
        )

    async def _flush_text_batch(self, driver, key: str, *, allowed: set[str]) -> None:
        current = asyncio.current_task()
        batch = self._pending_text.get(key)
        delay = TEXT_BATCH_SPLIT_DELAY_SECONDS if batch and len(batch.text) >= SPLIT_THRESHOLD else TEXT_BATCH_DELAY_SECONDS
        try:
            await asyncio.sleep(delay)
            if self._pending_tasks.get(key) is not current:
                return
            batch = self._pending_text.pop(key, None)
            if not batch:
                return
            await self._dispatch_inbound(
                driver,
                batch.message,
                text=batch.text,
                attachments=batch.attachments,
                allowed=allowed,
            )
        finally:
            if self._pending_tasks.get(key) is current:
                self._pending_tasks.pop(key, None)

    async def _process_message(self, driver, message: dict[str, Any], *, allowed: set[str]) -> None:
        sender_id = str(message.get("from_user_id") or "").strip()
        if not sender_id or sender_id == self.config.account_id:
            return

        message_id = str(message.get("message_id") or "").strip()
        if message_id and self._dedup.is_duplicate(message_id):
            return

        item_list = message.get("item_list") or []
        text = extract_text(item_list)
        if text:
            content_key = f"content:{sender_id}:{hashlib.md5(text.encode()).hexdigest()}"
            if self._dedup.is_duplicate(content_key):
                return

        chat_type, _ = _guess_chat_type(message, self.config.account_id)
        if chat_type == "group":
            if self.group_policy == "disabled":
                return
            return

        if self.dm_policy == "disabled":
            return
        is_slash_command = bool(text and text.startswith("/"))
        if self.dm_policy == "allowlist":
            if sender_id not in allowed and not is_slash_command:
                return
        elif allowed and sender_id not in allowed and not is_slash_command:
            return

        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._token_store.set(self.config.account_id, sender_id, context_token)
        asyncio.create_task(self._maybe_fetch_typing_ticket(sender_id, context_token or None))

        session = self._ensure_session()
        attachments = await download_inbound_attachments(
            session,
            item_list,
            cdn_base_url=self.config.cdn_base_url,
            inbound_dir=self._inbound_dir,
        )

        if text and not attachments and not text.startswith("/"):
            self._enqueue_text(driver, sender_id=sender_id, text=text, message=message,
                               attachments=attachments, allowed=allowed)
            return
        if not text and not attachments:
            return
        await self._dispatch_inbound(driver, message, text=text, attachments=attachments, allowed=allowed)

    async def _dispatch_inbound(
        self,
        driver,
        message: dict[str, Any],
        *,
        text: str,
        attachments: list[MessageAttachment],
        allowed: set[str],
    ) -> None:
        sender_id = str(message.get("from_user_id") or "").strip()
        chat_type, effective_chat_id = _guess_chat_type(message, self.config.account_id)
        if not text and attachments:
            text = "[图片]" if any(a.kind == "image" for a in attachments) else "[媒体]"
        ev = MessageEvent(
            text=text,
            chat_id=effective_chat_id,
            from_id=sender_id,
            platform="weixin",
            chat_type=chat_type,
            attachments=attachments or None,
            raw=message,
        )
        log.info("weixin inbound from=%s chat=%s attachments=%d", sender_id[:8], effective_chat_id[:8], len(attachments))
        await driver.handle_inbound(ev, client=self)

    async def _maybe_fetch_typing_ticket(self, user_id: str, context_token: str | None) -> None:
        if self._typing_cache.get(user_id):
            return
        await self._ensure_typing_ticket(user_id)

    async def _poll_loop(self, driver, *, allowed: set[str]) -> None:
        session = self._ensure_session()
        account_id = self.config.account_id
        self._token_store.restore(account_id)
        sync_buf = load_sync_buf(self.data_dir, account_id)
        timeout_ms = LONG_POLL_TIMEOUT_MS
        consecutive_failures = 0
        self._running = True

        acquired, existing = acquire_lock(
            self.data_dir,
            self._lock_scope,
            self.config.token,
            metadata={"account_id": account_id},
        )
        if not acquired:
            holder_pid = None
            if existing:
                try:
                    holder_pid = int(existing.get("pid"))
                except (TypeError, ValueError):
                    holder_pid = None
            mark_lock_blocked(self.data_dir, account_id, holder_pid=holder_pid)
            log.error(
                "weixin: bot token lock held by pid=%s; polling not started (only one instance per token)",
                holder_pid,
            )
            return
        self._lock_held = True
        health = public_health_view(self.data_dir, account_id)
        if health.get("status") == "lock_blocked":
            clear_health(self.data_dir, account_id)

        log.info("weixin polling started account=%s", account_id[:8])
        while self._running:
            try:
                response = await _get_updates(
                    session,
                    base_url=self.config.base_url,
                    token=self.config.token,
                    sync_buf=sync_buf,
                    timeout_ms=timeout_ms,
                )
                suggested_timeout = response.get("longpolling_timeout_ms")
                if isinstance(suggested_timeout, int) and suggested_timeout > 0:
                    timeout_ms = suggested_timeout

                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
                if ret not in {0, None} or errcode not in {0, None}:
                    if (
                        ret == SESSION_EXPIRED_ERRCODE
                        or errcode == SESSION_EXPIRED_ERRCODE
                        or _is_stale_session_ret(ret, errcode, response.get("errmsg"))
                    ):
                        mark_session_expired(
                            self.data_dir,
                            account_id,
                            detail="微信 iLink 会话已过期，轮询已停止。请在 Web UI → IM → Weixin 重新扫码登录。",
                        )
                        log.error(
                            "weixin: session expired for account=%s; polling stopped — re-login required",
                            account_id[:8],
                        )
                        self._running = False
                        break
                    consecutive_failures += 1
                    log.warning(
                        "weixin: getUpdates failed ret=%s errcode=%s (%d/%d)",
                        ret, errcode, consecutive_failures, MAX_CONSECUTIVE_FAILURES,
                    )
                    delay = BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_SECONDS
                    await asyncio.sleep(delay)
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        consecutive_failures = 0
                    continue

                consecutive_failures = 0
                new_sync_buf = str(response.get("get_updates_buf") or "")
                if new_sync_buf:
                    sync_buf = new_sync_buf
                    save_sync_buf(self.data_dir, account_id, sync_buf)

                for message in response.get("msgs") or []:
                    try:
                        await self._process_message(driver, message, allowed=allowed)
                    except Exception:
                        log.exception("weixin: handle_inbound failed from=%s", message.get("from_user_id"))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                log.error("weixin: poll error (%d/%d): %s", consecutive_failures, MAX_CONSECUTIVE_FAILURES, exc)
                delay = BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_SECONDS
                await asyncio.sleep(delay)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0


def resolve_config(
    *,
    account_id: str,
    token: str,
    base_url: str,
    data_dir: Path,
    cdn_base_url: str = WEIXIN_CDN_BASE_URL,
    split_multiline: bool = False,
) -> WeixinConfig | None:
    aid = (account_id or "").strip()
    tok = (token or "").strip()
    url = (base_url or ILINK_BASE_URL).strip().rstrip("/")
    if aid and not tok:
        persisted = load_account(data_dir, aid)
        if persisted:
            tok = str(persisted.get("token") or "").strip()
            url = str(persisted.get("base_url") or url).strip().rstrip("/")
    if not aid or not tok:
        return None
    return WeixinConfig(
        account_id=aid,
        token=tok,
        base_url=url,
        cdn_base_url=(cdn_base_url or WEIXIN_CDN_BASE_URL).strip().rstrip("/"),
        split_multiline=split_multiline,
    )


async def run_weixin(
    config: WeixinConfig,
    driver,
    data_dir: Path,
    *,
    allowed_user_ids: tuple[str, ...] = (),
    group_policy: str = "disabled",
    dm_policy: str = "pairing",
) -> None:
    allowed = {str(v) for v in allowed_user_ids}
    client = WeixinClient(config, data_dir, group_policy=group_policy, dm_policy=dm_policy)
    try:
        await client._poll_loop(driver, allowed=allowed)
    finally:
        log.info("weixin polling stopped")
        await client.aclose()
