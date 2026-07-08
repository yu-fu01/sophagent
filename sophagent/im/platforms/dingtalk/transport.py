"""DingTalk transports: buffered markdown + optional AI Card streaming."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ...transport import BufferedSendTransport

log = logging.getLogger(__name__)


class DingTalkLikeClient(Protocol):
    async def send_message(self, chat_id: str, text: str, *, finalize: bool = True) -> str: ...
    async def edit_message(
        self, chat_id: str, message_id: str, text: str, *, finalize: bool = False,
    ) -> None: ...
    async def fire_turn_complete(self, chat_id: str) -> None: ...


class DingTalkBufferedTransport(BufferedSendTransport):
    """Buffered markdown send with optional turn-complete emoji callback."""

    def __init__(
        self,
        chat_id: str,
        client: Any,
        *,
        on_turn_complete: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(chat_id, client)
        self._on_turn_complete = on_turn_complete

    async def close(self) -> None:
        await super().close()
        if self._on_turn_complete is not None:
            try:
                await self._on_turn_complete(self.chat_id)
            except Exception:
                log.debug("dingtalk turn-complete callback failed", exc_info=True)


class DingTalkCardTransport:
    def __init__(
        self,
        chat_id: str,
        client: DingTalkLikeClient,
        *,
        min_edit_interval: float = 0.8,
    ) -> None:
        self.chat_id = chat_id
        self.client = client
        self.min_edit_interval = min_edit_interval
        self._text = ""
        self._message_id: str | None = None
        self._last_edit_at = 0.0

    async def on_event(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "text_delta":
            self._text += ev.get("text", "")
            await self._flush(force=False)
        elif t == "done":
            await self._flush(force=True)
            self._message_id = None
            self._text = ""
        elif t == "error":
            msg = ev.get("message", "error")
            if self._message_id is None:
                try:
                    await self.client.send_message(self.chat_id, msg, finalize=True)
                except Exception:
                    log.warning("dingtalk send_message(error) failed chat=%s", self.chat_id)
            else:
                try:
                    await self.client.edit_message(
                        self.chat_id, self._message_id, msg, finalize=True,
                    )
                except Exception:
                    log.warning("dingtalk edit_message(error) failed chat=%s", self.chat_id)
            self._message_id = None
            self._text = ""

    async def _flush(self, *, force: bool) -> None:
        if not self._text:
            return
        now = time.monotonic()
        if self._message_id is None:
            try:
                self._message_id = await self.client.send_message(
                    self.chat_id, self._text, finalize=force,
                )
                self._last_edit_at = now
            except Exception:
                log.warning("dingtalk send_message failed chat=%s; will retry", self.chat_id)
            return
        if not force and (
            self.min_edit_interval <= 0
            or now - self._last_edit_at < self.min_edit_interval
        ):
            return
        try:
            await self.client.edit_message(
                self.chat_id, self._message_id, self._text, finalize=force,
            )
            self._last_edit_at = now
        except Exception:
            log.warning(
                "dingtalk edit_message failed chat=%s card=%s; will retry",
                self.chat_id, self._message_id,
            )

    async def close(self) -> None:
        await self._flush(force=True)
