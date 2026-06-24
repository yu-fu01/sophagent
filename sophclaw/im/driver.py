"""IM inbound driver: 命令分发 + run_turns(TelegramTransport)。

入站 MessageEvent -> 解析 /pair /new /stop /help，否则查绑定 -> 跑共享
turn 核心 run_turns，事件经 TelegramTransport edit-in-place 推回 chat。
单 chat 串行（manager.lock_for(session_id)）。排队复用 manager.try_put。
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from ..agent.turn import run_turns
from . import pairing
from .adapter import MessageEvent
from .commands import parse
from .transport import TelegramTransport

log = logging.getLogger(__name__)

HELP_TEXT = "命令：/pair <code> 绑定 · /new 新会话 · /stop 停止 · /help"


class IMDriver:
    def __init__(self, db, manager, skill_store) -> None:
        self.db = db
        self.manager = manager
        self.skill_store = skill_store

    async def handle_inbound(self, ev: MessageEvent, *, client) -> None:
        name, args = parse(ev.text)
        if name:
            await self._dispatch_command(name, args, ev, client)
            return
        await self._handle_text(ev, client)

    async def _dispatch_command(self, name: str, args: str, ev: MessageEvent, client) -> None:
        if name == "help":
            await client.send_message(ev.chat_id, HELP_TEXT)
        elif name == "pair":
            await self._cmd_pair(args, ev, client)
        elif name == "new":
            await self._cmd_new(ev, client)
        elif name == "stop":
            await self._cmd_stop(ev, client)
        else:
            await client.send_message(ev.chat_id, f"未知命令 /{name}。{HELP_TEXT}")

    async def _cmd_pair(self, args: str, ev: MessageEvent, client) -> None:
        if not args:
            await client.send_message(ev.chat_id, "用法：/pair <配对码>")
            return
        pair = await pairing.consume_code(self.db, args.strip())
        if pair is None:
            await client.send_message(ev.chat_id, "配对码无效或已过期。")
            return
        user_id, agent_id = pair
        agent = await self.db.get_agent(agent_id)
        await pairing.bind(self.db, ev.chat_id, user_id, agent_id)
        await client.send_message(ev.chat_id,
            f"✅ 已绑定，agent={agent['name'] if agent else agent_id}。发消息开始对话。")

    async def _cmd_new(self, ev: MessageEvent, client) -> None:
        sid = await pairing.renew_session(self.db, ev.chat_id)
        if sid is None:
            await client.send_message(ev.chat_id, "未绑定，请先 /pair <配对码>。")
            return
        await client.send_message(ev.chat_id, "✅ 已开启新会话。")

    async def _cmd_stop(self, ev: MessageEvent, client) -> None:
        row = await pairing.lookup(self.db, ev.chat_id)
        if row is None:
            await client.send_message(ev.chat_id, "未绑定。")
            return
        stopped = self.manager.stop(row["session_id"])
        self.manager.clear_queue(row["session_id"])
        await client.send_message(ev.chat_id, "⏹ 已停止。" if stopped else "（无在跑的回复）")

    async def _handle_text(self, ev: MessageEvent, client) -> None:
        row = await pairing.lookup(self.db, ev.chat_id)
        if row is None:
            await client.send_message(ev.chat_id, "未绑定，请先 /pair <配对码>。")
            return
        session_id = row["session_id"]
        user_id = row["user_id"]
        if self.manager.is_busy(session_id):
            if not self.manager.try_put(session_id, ev.text):
                await client.send_message(ev.chat_id, "队列已满，请稍后再试。")
            return
        transport = TelegramTransport(ev.chat_id, client)
        task = asyncio.create_task(self._run(session_id, ev.text, user_id, transport))
        self.manager.register_task(session_id, task)

    async def _run(self, session_id, user_input, user_id, transport):
        # 注意：不要再 `async with self.manager.lock_for(session_id)`——
        # run_turns 内部已加锁（见 agent/turn.py），asyncio.Lock 不可重入会自死锁。
        try:
            async for ev in run_turns(session_id=session_id, user_input=user_input,
                                      db=self.db, manager=self.manager,
                                      skill_store=self.skill_store, user_id=user_id):
                await transport.on_event(ev)
        except asyncio.CancelledError:
            await transport.on_event({"type": "error", "message": "stopped by user"})
            raise
        except Exception as e:
            log.exception("im turn failed session=%s", session_id)
            await transport.on_event({"type": "error", "message": f"出错：{e}"})
        finally:
            await transport.close()
