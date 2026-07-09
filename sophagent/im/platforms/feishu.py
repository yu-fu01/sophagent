"""Feishu/Lark IM adapter for sophagent.

Phase-1: websocket long connection, text inbound/outbound, pairing commands.
Phase-2: attachments, post/markdown outbound, interactive cards, webhook mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..adapter import MessageEvent
from ...models import MessageAttachment
from .feishu_card import build_card_content, build_help_card
from .feishu_parse import FeishuInbound, parse_card_action_data, parse_message_event_data

log = logging.getLogger(__name__)

FEISHU_BASE = "https://open.feishu.cn"
LARK_BASE = "https://open.larksuite.com"

FEISHU_SETUP_CHECKLIST = (
    "应用能力 → 机器人：已启用",
    "权限管理 → 开通「读取用户发给机器人的单聊消息」—— 私聊必开",
    "权限管理 → 开通「以应用的身份发消息」",
    "权限管理 → 群聊可选：「接收群聊中@机器人消息事件」",
    "权限管理 → 下载语音/图片/文件：通常已包含在「获取与发送单聊、群组消息」中",
    "事件配置 → 订阅方式：使用长连接（sophagent 日志出现 connected 后再保存）",
    "事件配置 → 添加「接收消息」，且事件旁不能出现橙色「请开通权限」提示",
    "权限变更后：创建版本并发布，再在 sophagent 点「刷新连接」",
    "私聊发送 /pair <配对码> 完成绑定",
)


class FeishuError(Exception):
    pass


@dataclass
class FeishuRuntime:
    driver: Any
    client: "FeishuClient"
    allowed_user_ids: set[str]
    require_mention_in_group: bool


_runtime_lock = threading.Lock()
_runtime: FeishuRuntime | None = None


def register_feishu_runtime(
    driver,
    client: "FeishuClient",
    *,
    allowed_user_ids: tuple[str, ...] = (),
    require_mention_in_group: bool = True,
) -> None:
    global _runtime
    with _runtime_lock:
        _runtime = FeishuRuntime(
            driver=driver,
            client=client,
            allowed_user_ids={str(v) for v in allowed_user_ids},
            require_mention_in_group=require_mention_in_group,
        )


def clear_feishu_runtime() -> None:
    global _runtime
    with _runtime_lock:
        _runtime = None


def get_feishu_runtime() -> FeishuRuntime | None:
    with _runtime_lock:
        return _runtime


class FeishuClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        domain: str = "feishu",
        timeout: float = 30.0,
        use_post_outbound: bool = True,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.domain = domain if domain in {"feishu", "lark"} else "feishu"
        self.use_post_outbound = use_post_outbound
        self._base = FEISHU_BASE if self.domain == "feishu" else LARK_BASE
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=True)
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self.bot_open_id: str = ""

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        resp = await self._client.post(
            f"{self._base}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        data = resp.json()
        token = data.get("tenant_access_token")
        if not token:
            raise FeishuError(f"Feishu token response missing tenant_access_token: {data}")
        self._access_token = str(token)
        self._token_expires_at = time.time() + int(data.get("expire", 7200))
        return self._access_token

    async def probe(self) -> dict[str, Any]:
        token = await self._ensure_token()
        resp = await self._client.get(
            f"{self._base}/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
        if resp.status_code >= 400 or data.get("code") not in (0, None):
            raise FeishuError(f"Feishu bot info failed: {resp.status_code} {data}")
        bot = data.get("bot") or {}
        self.bot_open_id = str(bot.get("open_id") or "")
        return data

    def _split_chat_id(self, chat_id: str) -> tuple[str, str]:
        if chat_id.startswith("p2p:"):
            return "p2p", chat_id[4:]
        if chat_id.startswith("chat:"):
            return "group", chat_id[5:]
        return "p2p", chat_id

    def _receive_target(self, chat_id: str) -> tuple[str, str]:
        chat_type, target = self._split_chat_id(chat_id)
        if chat_type == "p2p":
            return "open_id", target
        return "chat_id", target

    def _text_payload(self, text: str) -> tuple[str, str]:
        if self.use_post_outbound and ("\n" in text or "**" in text or "`" in text):
            lines = text.split("\n")
            content = [[{"tag": "text", "text": line or " "}] for line in lines]
            post = {"zh_cn": {"title": "", "content": content}}
            return "post", json.dumps(post, ensure_ascii=False)
        return "text", json.dumps({"text": text}, ensure_ascii=False)

    async def _create_message(
        self,
        chat_id: str,
        msg_type: str,
        content: str,
    ) -> str:
        receive_id_type, receive_id = self._receive_target(chat_id)
        token = await self._ensure_token()
        resp = await self._client.post(
            f"{self._base}/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": content,
            },
        )
        data = resp.json()
        if resp.status_code >= 400 or data.get("code") not in (0, None):
            raise FeishuError(f"Feishu send failed: {resp.status_code} {data}")
        message_id = ((data.get("data") or {}).get("message_id") or "")
        return str(message_id)

    async def send_message(self, chat_id: str, text: str) -> str:
        msg_type, content = self._text_payload(text)
        try:
            return await self._create_message(chat_id, msg_type, content)
        except FeishuError:
            if msg_type == "text":
                raise
            log.warning("feishu post send failed chat=%s; falling back to text", chat_id)
            return await self._create_message(
                chat_id,
                "text",
                json.dumps({"text": text}, ensure_ascii=False),
            )

    async def send_with_card(self, chat_id: str, text: str, card: dict[str, Any]) -> str:
        content = build_card_content(card)
        try:
            return await self._create_message(chat_id, "interactive", content)
        except FeishuError as exc:
            log.warning("feishu card send failed chat=%s error=%s; fallback text", chat_id, exc)
            return await self.send_message(chat_id, text)

    async def download_resource(
        self,
        message_id: str,
        file_key: str,
        *,
        resource_type: str,
        filename: str = "",
        media_kind: str = "",
    ) -> MessageAttachment | None:
        if not message_id or not file_key:
            return None
        # Feishu API only accepts type=image|file; audio/video/file all use file.
        api_type = "image" if resource_type == "image" else "file"
        local_kind = media_kind or ("image" if api_type == "image" else "file")
        token = await self._ensure_token()
        url = f"{self._base}/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
        try:
            resp = await self._client.get(
                url,
                params={"type": api_type},
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=True,
            )
            resp.raise_for_status()
        except Exception as exc:
            detail = ""
            if hasattr(exc, "response") and exc.response is not None:
                try:
                    detail = exc.response.text[:200]
                except Exception:
                    pass
            log.warning(
                "feishu resource download failed key=%s type=%s error=%s %s",
                file_key[:16],
                resource_type,
                exc,
                detail,
            )
            return None
        suffix = Path(filename or file_key).suffix or {
            "image": ".jpg",
            "file": ".bin",
            "audio": ".opus",
        }.get(local_kind, ".bin")
        tmp = tempfile.NamedTemporaryFile(prefix="feishu_", suffix=suffix, delete=False)
        try:
            tmp.write(resp.content)
            tmp.close()
        except Exception:
            tmp.close()
            Path(tmp.name).unlink(missing_ok=True)
            raise
        kind = "image" if local_kind == "image" else ("audio" if local_kind == "audio" else "file")
        mime = resp.headers.get("content-type", "").split(";", 1)[0].strip()
        return MessageAttachment(
            kind=kind,
            path=tmp.name,
            name=filename or (f"{file_key}.opus" if local_kind == "audio" else file_key),
            mime=mime or "application/octet-stream",
            size=len(resp.content),
            platform="feishu",
            raw={"file_key": file_key},
        )


async def _materialize_attachments(client: FeishuClient, inbound: FeishuInbound) -> list[MessageAttachment]:
    out: list[MessageAttachment] = []
    for att in inbound.attachments or []:
        if att.path:
            out.append(att)
            continue
        raw = att.raw or {}
        file_key = str(raw.get("file_key") or "")
        if not file_key or not inbound.message_id:
            out.append(att)
            continue
        downloaded = await client.download_resource(
            inbound.message_id,
            file_key,
            resource_type="image" if att.kind == "image" else "file",
            filename=att.name,
            media_kind=att.kind,
        )
        out.append(downloaded or att)
    return out


async def _dispatch_inbound(
    runtime: FeishuRuntime,
    inbound: FeishuInbound,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    if runtime.allowed_user_ids and inbound.from_id not in runtime.allowed_user_ids:
        log.info("feishu ignored user=%s by allowlist", inbound.from_id)
        return
    attachments = await _materialize_attachments(runtime.client, inbound)
    ev = MessageEvent(
        text=inbound.text,
        chat_id=inbound.chat_id,
        from_id=inbound.from_id,
        platform="feishu",
        chat_type=inbound.chat_type,
        attachments=attachments,
        raw=inbound.raw,
    )
    log.info("feishu inbound chat=%s from=%s text=%r", inbound.chat_id, inbound.from_id, inbound.text[:80])
    await runtime.driver.handle_inbound(ev, client=runtime.client)


def _event_to_dict(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    event = getattr(data, "event", None)
    if event is None:
        return {}
    try:
        if hasattr(event, "model_dump"):
            return {"event": event.model_dump()}
        if hasattr(event, "to_dict"):
            return {"event": event.to_dict()}
    except Exception:
        pass
    return {"event": event}


async def handle_feishu_event_dict(data: dict[str, Any]) -> None:
    runtime = get_feishu_runtime()
    if runtime is None:
        log.warning("feishu event received but runtime is not registered")
        return
    header = data.get("header") if isinstance(data.get("header"), dict) else {}
    event_type = str(header.get("event_type") or data.get("type") or "")
    if event_type == "card.action.trigger":
        inbound = parse_card_action_data(data)
    else:
        inbound = parse_message_event_data(
            data,
            bot_open_id=runtime.client.bot_open_id,
            require_mention_in_group=runtime.require_mention_in_group,
        )
    if inbound is None:
        return
    await _dispatch_inbound(runtime, inbound)


async def run_feishu(
    client: FeishuClient,
    driver,
    *,
    allowed_user_ids: tuple[str, ...] = (),
    require_mention_in_group: bool = True,
    connection_mode: str = "websocket",
    verification_token: str = "",
    encrypt_key: str = "",
) -> None:
    """Run Feishu gateway until cancelled."""
    if connection_mode == "webhook":
        await client.probe()
        register_feishu_runtime(
            driver,
            client,
            allowed_user_ids=allowed_user_ids,
            require_mention_in_group=require_mention_in_group,
        )
        log.info("feishu webhook mode armed; waiting for HTTP callbacks")
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            clear_feishu_runtime()
            await client.aclose()
        return

    try:
        import lark_oapi as lark
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
        from lark_oapi.ws import Client as FeishuWSClient
    except ImportError as exc:
        raise FeishuError("lark-oapi is required for Feishu websocket mode") from exc

    await client.probe()
    register_feishu_runtime(
        driver,
        client,
        allowed_user_ids=allowed_user_ids,
        require_mention_in_group=require_mention_in_group,
    )
    loop = asyncio.get_running_loop()
    stop_event = threading.Event()

    async def _handle_message_data(data: Any) -> None:
        runtime = get_feishu_runtime()
        if runtime is None:
            log.warning("feishu message event received but runtime is not registered")
            return
        inbound = parse_message_event_data(
            data,
            bot_open_id=runtime.client.bot_open_id,
            require_mention_in_group=runtime.require_mention_in_group,
        )
        if inbound is None:
            log.debug("feishu inbound dropped during parse/filter")
            return
        await _dispatch_inbound(runtime, inbound)

    async def _handle_card_action(data: Any) -> None:
        runtime = get_feishu_runtime()
        if runtime is None:
            log.warning("feishu card action received but runtime is not registered")
            return
        inbound = parse_card_action_data(data)
        if inbound is None:
            log.debug("feishu card action dropped during parse")
            return
        await _dispatch_inbound(runtime, inbound)

    async def _handle_p2p_entered(data: Any) -> None:
        runtime = get_feishu_runtime()
        if runtime is None:
            return
        event = getattr(data, "event", None)
        operator_id = getattr(event, "operator_id", None) if event else None
        open_id = str(getattr(operator_id, "open_id", "") or "").strip()
        if not open_id:
            log.debug("feishu p2p entered without operator open_id")
            return
        chat_id = f"p2p:{open_id}"
        text = (
            "你好，我是 sophagent 飞书机器人。\n"
            "在 sophagent 网页生成配对码后，发送：/pair <配对码>\n"
            "若发消息无回复：请到飞书开放平台 → 权限管理，"
            "开通「读取用户发给机器人的单聊消息」，并发布应用版本。"
        )
        try:
            await runtime.client.send_message(chat_id, text)
            log.info("feishu p2p welcome sent chat=%s", chat_id)
        except Exception as exc:
            log.warning("feishu p2p welcome failed chat=%s error=%s", chat_id, exc)

    def _submit(coro) -> None:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        fut.add_done_callback(lambda f: f.exception() and log.exception("feishu inbound failed", exc_info=f.exception()))

    def _on_message(data: Any) -> None:
        log.info("feishu websocket message event received")
        _submit(_handle_message_data(data))

    def _on_card(data: Any) -> None:
        log.info("feishu websocket card action received")
        _submit(_handle_card_action(data))

    def _on_p2p_entered(data: Any) -> None:
        log.info("feishu websocket p2p chat entered")
        _submit(_handle_p2p_entered(data))

    domain = lark.FEISHU_DOMAIN if client.domain == "feishu" else lark.LARK_DOMAIN
    handler = (
        EventDispatcherHandler.builder(encrypt_key, verification_token)
        .register_p2_im_message_receive_v1(_on_message)
        .register_p2_card_action_trigger(_on_card)
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(_on_p2p_entered)
        .build()
    )
    ws_client = FeishuWSClient(
        app_id=client.app_id,
        app_secret=client.app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
        domain=domain,
    )

    def _run_ws() -> None:
        import lark_oapi.ws.client as ws_client_module

        thread_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(thread_loop)
        ws_client_module.loop = thread_loop
        try:
            ws_client.start()
        except Exception as exc:
            if not stop_event.is_set():
                log.warning("feishu websocket stopped: %s", exc)
        finally:
            pending = [t for t in asyncio.all_tasks(thread_loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                thread_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            thread_loop.close()

    ws_thread = threading.Thread(target=_run_ws, name="feishu-ws", daemon=True)
    ws_thread.start()
    log.info(
        "feishu websocket started domain=%s; ensure 「接收消息」 event is subscribed in Feishu console",
        client.domain,
    )
    try:
        while ws_thread.is_alive():
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        stop_event.set()
        raise
    finally:
        stop_event.set()
        clear_feishu_runtime()
        await client.aclose()
