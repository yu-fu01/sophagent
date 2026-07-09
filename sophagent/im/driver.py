"""IM inbound driver: 命令分发 + run_turns(TelegramTransport)。

入站 MessageEvent -> 解析 /pair /new /stop /help，否则查绑定 -> 跑共享
turn 核心 run_turns，事件经 TelegramTransport edit-in-place 推回 chat。
单 chat 串行（manager.lock_for(session_id)）。排队复用 manager.try_put。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from ..agent.turn import run_turns
from ..config import get_config
from ..models import MessageAttachment
from . import pairing
from .adapter import MessageEvent
from .commands import parse
from .platforms.feishu_card import build_help_card
from .platforms.qqbot_keyboard import build_help_keyboard
from .transcribe import transcribe_audio_attachment
from .transport import TelegramTransport

log = logging.getLogger(__name__)

HELP_TEXT = "命令：/pair <code> 绑定 · /new 新会话 · /stop 停止 · /help"
_MEDIA_PLACEHOLDER_RE = re.compile(r"^\[(audio|voice|语音|video|media|file|图片|image)\]\s*$", re.I)


class IMDriver:
    def __init__(
        self,
        db,
        manager,
        skill_store,
        *,
        transport_factory: Callable[[MessageEvent, Any], Any] | None = None,
    ) -> None:
        self.db = db
        self.manager = manager
        self.skill_store = skill_store
        self.transport_factory = transport_factory or self._default_transport_factory

    def _default_transport_factory(self, ev: MessageEvent, client):
        return TelegramTransport(ev.chat_id, client)

    async def handle_inbound(self, ev: MessageEvent, *, client) -> None:
        name, args = parse(ev.text)
        if name:
            await self._dispatch_command(name, args, ev, client)
            return
        await self._handle_text(ev, client)

    async def _dispatch_command(self, name: str, args: str, ev: MessageEvent, client) -> None:
        if name == "help":
            send_with_keyboard = getattr(client, "send_with_keyboard", None)
            send_with_card = getattr(client, "send_with_card", None)
            if ev.platform == "qqbot" and callable(send_with_keyboard):
                await send_with_keyboard(ev.chat_id, HELP_TEXT, build_help_keyboard())
            elif ev.platform == "feishu" and callable(send_with_card):
                await send_with_card(ev.chat_id, HELP_TEXT, build_help_card())
            else:
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
        await pairing.bind(self.db, ev.chat_id, user_id, agent_id, platform=ev.platform)
        await client.send_message(ev.chat_id,
            f"✅ 已绑定，agent={agent['name'] if agent else agent_id}。发消息开始对话。")

    async def _cmd_new(self, ev: MessageEvent, client) -> None:
        sid = await pairing.renew_session(self.db, ev.chat_id, platform=ev.platform)
        if sid is None:
            await client.send_message(ev.chat_id, "未绑定，请先 /pair <配对码>。")
            return
        await client.send_message(ev.chat_id, "✅ 已开启新会话。")

    async def _cmd_stop(self, ev: MessageEvent, client) -> None:
        row = await pairing.lookup(self.db, ev.chat_id, platform=ev.platform)
        if row is None:
            await client.send_message(ev.chat_id, "未绑定。")
            return
        stopped = self.manager.stop(row["session_id"])
        self.manager.clear_queue(row["session_id"])
        await client.send_message(ev.chat_id, "⏹ 已停止。" if stopped else "（无在跑的回复）")

    async def _handle_text(self, ev: MessageEvent, client) -> None:
        row = await pairing.lookup(self.db, ev.chat_id, platform=ev.platform)
        if row is None:
            await client.send_message(ev.chat_id, "未绑定，请先 /pair <配对码>。")
            return
        session_id = row["session_id"]
        user_id = row["user_id"]
        text, attachments = await self._prepare_input(ev, user_id, session_id=session_id)
        if self.manager.is_busy(session_id):
            if not self.manager.try_put(session_id, text):
                await client.send_message(ev.chat_id, "队列已满，请稍后再试。")
            return
        transport = self.transport_factory(ev, client)
        task = asyncio.create_task(self._run(session_id, text, user_id, transport, attachments))
        self.manager.register_task(session_id, task)

    async def _prepare_input(
        self,
        ev: MessageEvent,
        user_id: int,
        *,
        session_id: str | None = None,
    ) -> tuple[str, list[MessageAttachment]]:
        attachments = await self._persist_attachments(ev.attachments or [], user_id, platform=ev.platform)
        attachments = await self._maybe_transcribe_audio(attachments, user_id, session_id)
        refs = [f"[附加文件: {a.path}]" for a in attachments if a.path and a.kind != "emoji"]
        image_refs = [a for a in attachments if a.kind == "image" and a.path]
        if image_refs:
            refs.append(
                "[提示: 用户发送了图片，当前模型不支持原生识图，请用 read_file 读取上方图片路径查看内容]"
            )
        transcript_notes = []
        emoji_notes = []
        audio_notes = []
        for a in attachments:
            if a.kind == "audio":
                transcript = (a.raw or {}).get("transcript") if a.raw else None
                if transcript:
                    transcript_notes.append(f'[语音转写] "{transcript}"')
                elif a.path:
                    audio_notes.append(
                        f"[用户发送了一条语音消息，文件已保存至 {a.path}；"
                        "当前未能自动转写，请根据上下文理解用户意图。]"
                    )
                else:
                    audio_notes.append("[用户发送了一条语音消息，但未能下载音频文件。]")
                continue
            if a.kind == "emoji":
                note = f"[表情: {a.raw.get('text') or a.name or '表情包'}]"
                if note not in ev.text:
                    emoji_notes.append(note)
        text = ev.text.strip()
        if attachments and _MEDIA_PLACEHOLDER_RE.match(text):
            text = ""
        parts = [*refs, *transcript_notes, *audio_notes, *emoji_notes, text]
        return "\n".join(p for p in parts if p).strip(), attachments

    async def _maybe_transcribe_audio(
        self,
        attachments: list[MessageAttachment],
        user_id: int,
        session_id: str | None,
    ) -> list[MessageAttachment]:
        if not session_id or not attachments:
            return attachments
        if not any(a.kind == "audio" and not (a.raw or {}).get("transcript") for a in attachments):
            return attachments
        session = await self.db.get_session(session_id, user_id)
        if session is None:
            return attachments
        agent = await self.db.get_agent(session["agent_id"])
        if agent is None:
            return attachments
        root = get_config().workspace_for(user_id)
        out: list[MessageAttachment] = []
        for att in attachments:
            if att.kind != "audio" or (att.raw or {}).get("transcript"):
                out.append(att)
                continue
            out.append(await transcribe_audio_attachment(
                att,
                workspace_root=root,
                provider_name=str(agent["provider"] or ""),
            ))
        return out

    async def _persist_attachments(
        self,
        attachments: list[MessageAttachment],
        user_id: int,
        *,
        platform: str = "telegram",
    ) -> list[MessageAttachment]:
        if not attachments:
            return []
        root = get_config().workspace_for(user_id)
        platform_name = (platform or "telegram").strip() or "telegram"
        target_dir = root / "im" / platform_name
        target_dir.mkdir(parents=True, exist_ok=True)
        persisted: list[MessageAttachment] = []
        for idx, att in enumerate(attachments, start=1):
            if not att.path:
                persisted.append(att)
                continue
            src = Path(att.path)
            if not src.is_file():
                continue
            name = Path(att.name or src.name).name or f"attachment-{idx}"
            dest = _dedupe_path(target_dir / name)
            try:
                shutil.move(str(src), dest)
            except Exception:
                log.warning("im attachment persist failed path=%s", src)
                continue
            persisted.append(MessageAttachment(
                kind=att.kind,
                path=str(dest.relative_to(root)),
                name=att.name or dest.name,
                mime=att.mime,
                size=att.size,
                platform=att.platform,
                raw=att.raw,
            ))
        return persisted

    async def _run(self, session_id, user_input, user_id, transport, attachments=None):
        # 注意：不要再 `async with self.manager.lock_for(session_id)`——
        # run_turns 内部已加锁（见 agent/turn.py），asyncio.Lock 不可重入会自死锁。
        try:
            async for ev in run_turns(session_id=session_id, user_input=user_input,
                                      db=self.db, manager=self.manager,
                                      skill_store=self.skill_store, user_id=user_id,
                                      user_attachments=attachments):
                await transport.on_event(ev)
        except asyncio.CancelledError:
            await transport.on_event({"type": "error", "message": "stopped by user"})
            raise
        except Exception as e:
            log.exception("im turn failed session=%s", session_id)
            await transport.on_event({"type": "error", "message": f"出错：{e}"})
        finally:
            await transport.close()


def _dedupe_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}-{id(path)}{suffix}")
