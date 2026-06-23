"""Slash command: /compact — manually trigger conversation compression."""

from __future__ import annotations

import logging
from typing import Any

from ....models import Message

log = logging.getLogger(__name__)


async def handle(args: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Manually compact the current session's history in-database.

    Loads the full history, runs ``AgentRunner._summarize_oldest_half``-style
    compression directly, and writes the compacted result back to the DB.
    """
    db = ctx["db"]
    session = ctx["session"]
    if session is None:
        return {"content": "No active session.", "action": None}

    session_id = session["id"]

    # load live (non-archived) messages
    messages = await db.load_messages(session_id)
    if not messages:
        return {"content": "No messages to compact.", "action": None}

    if len(messages) < 6:
        return {"content": f"Only {len(messages)} messages — too few to compact (need at least 6).", "action": None}

    # reproduce the compression logic from loop.py
    cut = len(messages) // 2
    old, rest = messages[:cut], messages[cut:]

    transcript = "\n".join(
        f"[{m.role}] {m.content[:1000]}" for m in old if m.content
    )

    # use the session's configured provider + model for summarization
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
    summary_parts: list[str] = []

    try:
        async for ev in provider.chat(
            model=model_name,
            system="Summarize this conversation excerpt into a compact brief that preserves "
                   "goals, decisions, key facts, file names and unresolved items. Plain text.",
            messages=[Message(role="user", content=transcript[:60_000])],
            max_tokens=1500,
        ):
            if ev.type == "turn_done" and ev.turn:
                summary_parts.append(ev.turn.content)
    except Exception as e:
        log.exception("manual compression failed")
        return {"content": f"Compression failed: {e}", "action": None}

    summary = "".join(summary_parts).strip()
    if not summary:
        return {"content": "Compression produced empty summary — aborting.", "action": None}

    new_history = [Message(role="user", content=f"[Earlier conversation summary]\n{summary}")] + rest

    # archive old rows + insert compacted history
    await db.compact_session(session_id, new_history)

    orig_count = len(messages)
    new_count = len(new_history)
    dropped = sum(len(m.content) for m in old)
    kept = sum(len(m.content) for m in new_history)

    return {
        "content": (
            f"✅ **Conversation compacted.**\n"
            f"- Original: {orig_count} messages ({dropped:,} chars)\n"
            f"- After: {new_count} messages ({kept:,} chars)\n"
            f"- Oldest {cut} messages summarized into a single brief."
        ),
        "action": "reload",
    }
