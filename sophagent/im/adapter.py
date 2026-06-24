"""Telegram Bot API client (httpx, no SDK) + long-poll loop.

只实现 IM 网关需要的子集：getUpdates / sendMessage / editMessageText。
adapter 与 transport 分离：adapter 管 HTTP，transport 管事件->投递语义。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class MessageEvent:
    text: str
    chat_id: str
    from_id: str


class TelegramClient:
    """httpx 直连 Bot API。长生命周期 client（复用 TLS 连接，减少握手抖动）；
    trust_env=True 自动尊重 HTTPS_PROXY/HTTP_PROXY（国内访问 Telegram 可走代理）。
    _call 对网络瞬断（ConnectError/Timeout）重试，避免单次失败杀掉整轮回复。"""

    def __init__(self, token: str, *, timeout: float = 35.0) -> None:
        self.token = token
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send_message(self, chat_id: str, text: str) -> int:
        r = await self._call("sendMessage", {"chat_id": chat_id, "text": text})
        return r["result"]["message_id"]

    async def edit_message(self, chat_id: str, message_id: int, text: str) -> None:
        try:
            await self._call("editMessageText",
                             {"chat_id": chat_id, "message_id": message_id, "text": text})
        except TelegramError as e:
            # "message is not modified" 等可忽略
            if "not modified" not in str(e).lower():
                raise

    async def get_updates(self, offset: int) -> list[dict[str, Any]]:
        r = await self._call("getUpdates", {"offset": offset, "timeout": 30},
                             timeout=self.timeout)
        return r["result"]

    async def _call(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> dict:
        url = BASE.format(token=self.token, method=method)
        delay = 1.0
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.post(url, json=params, timeout=timeout or 30)
                data = resp.json()
                if not data.get("ok"):
                    raise TelegramError(data.get("description", "telegram error"))
                return data
            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
                last = e
                log.warning("telegram %s network error (attempt %d/3): %s", method, attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(delay)
                    delay *= 2
        assert last is not None
        raise last   # 3 次都失败；transport 会兜底，不杀 turn


class TelegramError(Exception):
    pass


async def run_polling(client: TelegramClient, driver, *, allowed_user_ids: tuple[int, ...] = ()) -> None:
    """长轮询 getUpdates，把文本消息交给 driver.handle_inbound。"""
    offset = 0
    log.info("telegram polling started")
    try:
        while True:
            try:
                updates = await client.get_updates(offset)
            except Exception as e:
                log.warning("getUpdates failed: %s; retry in 2s", e)
                await asyncio.sleep(2)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message")
                if not msg:
                    continue
                text = (msg.get("text") or "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                from_id = str(msg.get("from", {}).get("id", ""))
                if not text or not chat_id:
                    continue
                if allowed_user_ids and from_id not in {str(i) for i in allowed_user_ids}:
                    continue
                try:
                    await driver.handle_inbound(MessageEvent(text=text, chat_id=chat_id, from_id=from_id),
                                                client=client)
                except Exception:
                    log.exception("handle_inbound failed chat=%s", chat_id)
    finally:
        log.info("telegram polling stopped")
        await client.aclose()
