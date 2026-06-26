"""/undo — remove the last user message and its assistant response(s)."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def handle(args: str, ctx: dict) -> dict:
    """Truncate from the last user message."""
    db = ctx["db"]
    session = ctx["session"]

    if not session:
        return {"content": "No active session."}

    session_id = session["id"]
    messages = await db.load_messages_with_ids(session_id)

    # find the last user message (not a summary)
    last_user_id = None
    for mid, m, _ in reversed(messages):
        if m.role == "user":
            last_user_id = mid
            break

    if last_user_id is None:
        return {"content": "No user message to undo."}

    deleted = await db.truncate_from(session_id, last_user_id)
    log.info("undo on session %s: truncated from message %d (%d messages deleted)",
             session_id, last_user_id, deleted)

    return {"content": f"↩️ Undone. Removed the last user message and its response.", "action": "reload"}