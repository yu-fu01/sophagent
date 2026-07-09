"""TelegramTransport: 内部 agent 事件 -> Telegram edit-in-place 流式。

消费 sophagent 内部事件（text_delta/done/error/...），不使用 WS 的 wire 帧。
首轮首帧 sendMessage 记 message_id；后续累积文本 editMessageText（限频）；
done 落最终全文并清 message_id（下一轮首帧重发新消息）。
reasoning/tool/turn_usage/session.info/queued_next 忽略。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol

log = logging.getLogger(__name__)


class TelegramLikeClient(Protocol):
    async def send_message(self, chat_id: str, text: str) -> int: ...
    async def edit_message(self, chat_id: str, message_id: int, text: str) -> None: ...


class TelegramTransport:
    def __init__(self, chat_id: str, client: TelegramLikeClient,
                 *, min_edit_interval: float = 0.6) -> None:
        self.chat_id = chat_id
        self.client = client
        self.min_edit_interval = min_edit_interval
        self.message_id: int | None = None
        self._text = ""
        self._last_edit_at = 0.0

    async def on_event(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "text_delta":
            self._text += ev.get("text", "")
            await self._flush(force=False)
        elif t == "done":
            await self._flush(force=True)            # 落最终全文
            self.message_id = None
            self._text = ""
        elif t == "error":
            msg = ev.get("message", "error")
            if self.message_id is None:
                try:
                    await self.client.send_message(self.chat_id, msg)
                except Exception:
                    log.warning("telegram send_message(error) failed chat=%s", self.chat_id)
            else:
                try:
                    await self.client.edit_message(self.chat_id, self.message_id, msg)
                except Exception:
                    log.warning("telegram edit_message(error) failed chat=%s", self.chat_id)
            self.message_id = None
            self._text = ""
        # reasoning_delta / tool_call / tool_result / turn_usage / session_info / queued_next: 忽略

    async def _flush(self, *, force: bool) -> None:
        if not self._text:
            return
        now = time.monotonic()
        if self.message_id is None:
            try:
                self.message_id = await self.client.send_message(self.chat_id, self._text)
                self._last_edit_at = now
            except Exception:
                # 发送失败（网络瞬断）：保留 _text，下次事件/done 重试。绝不杀 turn。
                log.warning("telegram send_message failed chat=%s; will retry next flush", self.chat_id)
            return
        # 限频：非正 min_edit_interval 禁用中间 edit（仅 done/close 强制落）；
        # 否则距上次 edit 不足间隔则等下次或 done。
        if not force and (
            self.min_edit_interval <= 0
            or now - self._last_edit_at < self.min_edit_interval
        ):
            return
        try:
            await self.client.edit_message(self.chat_id, self.message_id, self._text)
            self._last_edit_at = now
        except Exception:
            # edit 失败：保留 message_id + _text，下次/done 重试。绝不杀 turn。
            log.warning("telegram edit_message failed chat=%s msg=%s; will retry next flush",
                        self.chat_id, self.message_id)

    async def close(self) -> None:
        await self._flush(force=True)  # 兜底落全文（_flush 自身容错）


class BufferedSendTransport:
    """Buffer streamed deltas and send one final message.

    Used by platforms such as QQ Bot where message editing is unavailable or too
    costly. The client only needs ``send_message(chat_id, text)``.
    """

    def __init__(self, chat_id: str, client: TelegramLikeClient) -> None:
        self.chat_id = chat_id
        self.client = client
        self._text = ""
        self._sent = False

    async def on_event(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "text_delta":
            self._text += ev.get("text", "")
        elif t == "done":
            await self._send_final()
            # Chained turns (queued_next) reuse this transport; allow the next reply.
            self._sent = False
        elif t == "error":
            msg = ev.get("message", "error")
            try:
                await self.client.send_message(self.chat_id, msg)
            except Exception:
                log.warning("buffered send error message failed chat=%s", self.chat_id)
            self._sent = True
            self._text = ""

    async def _send_final(self) -> None:
        if self._sent or not self._text:
            if not self._sent and not self._text:
                log.debug("buffered send skipped: empty text chat=%s", self.chat_id)
            return
        try:
            await self.client.send_message(self.chat_id, self._text)
            self._sent = True
            self._text = ""
        except Exception:
            log.warning("buffered send failed chat=%s; will retry on close", self.chat_id)

    async def close(self) -> None:
        await self._send_final()
