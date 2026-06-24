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
    """httpx 直连 Bot API。供 TelegramTransport 与 run_polling 共用。"""

    def __init__(self, token: str, *, timeout: float = 35.0) -> None:
        self.token = token
        self.timeout = timeout

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
        async with httpx.AsyncClient(timeout=timeout or 30) as c:
            resp = await c.post(url, json=params)
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(data.get("description", "telegram error"))
        return data


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
