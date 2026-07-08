"""Weixin transport: buffered text + optional typing indicator."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class WeixinBufferedTransport:
    """Buffer streamed deltas, send final text/media, show typing while generating."""

    def __init__(self, chat_id: str, client) -> None:
        self.chat_id = chat_id
        self.client = client
        self._text = ""
        self._sent = False
        self._typing = False

    async def on_event(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "text_delta":
            if not self._typing:
                await self._start_typing()
            self._text += ev.get("text", "")
        elif t == "done":
            await self._send_final()
            await self._stop_typing()
        elif t == "error":
            await self._stop_typing()
            msg = ev.get("message", "error")
            try:
                await self.client.send_message(self.chat_id, msg)
            except Exception:
                log.warning("weixin send error message failed chat=%s", self.chat_id)
            self._sent = True
            self._text = ""

    async def _start_typing(self) -> None:
        self._typing = True
        try:
            await self.client.send_typing(self.chat_id)
        except Exception:
            self._typing = False

    async def _stop_typing(self) -> None:
        if not self._typing:
            return
        self._typing = False
        try:
            await self.client.stop_typing(self.chat_id)
        except Exception:
            pass

    async def _send_final(self) -> None:
        if self._sent or not self._text:
            return
        try:
            await self.client.send_message(self.chat_id, self._text)
            self._sent = True
            self._text = ""
        except Exception:
            log.warning("weixin buffered send failed chat=%s; will retry on close", self.chat_id)

    async def close(self) -> None:
        await self._send_final()
        await self._stop_typing()
