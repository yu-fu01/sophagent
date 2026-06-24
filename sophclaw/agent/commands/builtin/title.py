"""/title — set the title of the current session."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def handle(args: str, ctx: dict) -> dict:
    """Update session title."""
    db = ctx["db"]
    session = ctx["session"]

    if not session:
        return {"content": "No active session."}

    if not args:
        return {"content": f"Current title: **{session.get('title') or '(no title)'}**\n\nUsage: `/title <your-title>`"}

    title = args.strip()
    session_id = session["id"]
    await db.touch_session(session_id, title=title)
    log.info("session %s title set to %r", session_id, title)

    return {"content": f"✅ Title set to: **{title}**", "action": "reload_sessions"}