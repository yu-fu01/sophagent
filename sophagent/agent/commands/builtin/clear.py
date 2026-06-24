"""/clear — clear all messages in the current session (start fresh)."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def handle(args: str, ctx: dict) -> dict:
    """Truncate the entire session history."""
    db = ctx["db"]
    session = ctx["session"]
    if not session:
        return {"content": "No active session to clear."}

    session_id = session["id"]
    messages = await db.load_messages_with_ids(session_id)
    if not messages:
        return {"content": "Session is already empty."}

    first_id = messages[0][0]
    deleted = await db.truncate_from(session_id, first_id)
    log.info("cleared session %s: removed %d messages", session_id, deleted)

    return {
        "content": f"✅ Session cleared. Removed {deleted} message(s).",
        "action": "reload",  # signals frontend to refresh
    }