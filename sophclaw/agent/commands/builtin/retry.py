"""/retry — remove the last assistant response, then re-run the last user turn."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def handle(args: str, ctx: dict) -> dict:
    """Truncate from the last assistant response (after the last user message)
    and signal the caller to start a streaming turn with the last user message."""
    db = ctx["db"]
    session = ctx["session"]

    if not session:
        return {"content": "No active session."}

    session_id = session["id"]
    messages = await db.load_messages_with_ids(session_id)

    # Find the last user message and retry the response that follows it.
    last_user_idx = None
    for i, (_mid, m) in reversed(list(enumerate(messages))):
        if m.role == "user":
            last_user_idx = i
            break

    if last_user_idx is None:
        return {"content": "No user message to retry."}

    if last_user_idx + 1 >= len(messages):
        return {"content": "No assistant response to retry."}
    truncate_from = messages[last_user_idx + 1][0]

    deleted = await db.truncate_from(session_id, truncate_from)
    log.info("retry on session %s: truncated from message %d (%d deleted)",
             session_id, truncate_from, deleted)

    return {
        "content": messages[last_user_idx][1].content,
        "action": "retry_stream",
    }