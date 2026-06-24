"""Slash command: /compact [focus] — 手动触发对话压缩（可带引导主题）。"""

from __future__ import annotations

import logging
from typing import Any

from ....agent import compaction as C

log = logging.getLogger(__name__)

PROTECT_TAIL_TOKENS = 2000  # 手动 /compact 保护的尾部 token 预算（比自动压缩更激进，仅保留近期若干轮）


async def handle(args: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """手动压缩当前会话历史。``args`` 非空时作为引导式压缩的 focus 主题。"""
    db = ctx["db"]
    session = ctx["session"]
    if session is None:
        return {"content": "No active session.", "action": None}

    focus = args.strip() or None
    session_id = session["id"]

    messages = await db.load_messages(session_id)
    if not messages:
        return {"content": "No messages to compact.", "action": None}
    if len(messages) < 6:
        return {"content": f"Only {len(messages)} messages — too few to compact "
                           f"(need at least 6).", "action": None}

    old, rest = C.split_for_summary(messages, PROTECT_TAIL_TOKENS)
    if not old:
        return {"content": "Nothing old enough to compact.", "action": None}

    # 解析 provider/model（沿用既有逻辑）
    from ....providers import get_provider

    provider_name = session["override_provider"] if "override_provider" in session.keys() and session["override_provider"] else None
    model_name = session["override_model"] if "override_model" in session.keys() and session["override_model"] else None
    if not provider_name:
        agent = await db.get_agent(session["agent_id"])
        if agent:
            provider_name = agent["provider"]
            model_name = model_name or agent["model"]
    if not provider_name or not model_name:
        return {"content": "Cannot determine provider/model for compression.", "action": None}

    provider = get_provider(provider_name)
    prev = C.find_previous_summary(messages)
    turns = [m for m in old if not C.is_summary_message(m)]

    summary = await C.summarize(provider, model_name, turns, prev_summary=prev, focus=focus)
    if summary is None:
        return {"content": "Compression produced empty summary — aborting.", "action": None}

    new_history = [C.make_summary_message(summary)] + rest
    await db.compact_session(session_id, new_history)

    dropped = sum(len(m.content) for m in old)
    kept = sum(len(m.content) for m in new_history)
    focus_note = f"（聚焦：{focus}）" if focus else ""
    return {
        "content": (
            f"✅ **对话已压缩。**{focus_note}\n"
            f"- 原始：{len(messages)} 条消息（{dropped:,} 字符）\n"
            f"- 压缩后：{len(new_history)} 条消息（{kept:,} 字符）\n"
            f"- 最旧 {len(old)} 条消息已摘要为单条结构化简报。"
        ),
        "action": "reload",
    }
