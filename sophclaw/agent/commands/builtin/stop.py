"""/stop — stop the currently running turn."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def handle(args: str, ctx: dict) -> dict:
    """Stop the current turn via SessionManager."""
    session = ctx["session"]
    manager = ctx["manager"]

    if not session:
        return {"content": "No active session."}

    stopped = manager.stop(session["id"])
    manager.clear_queue(session["id"])
    if stopped:
        return {"content": "⏹️ Turn stopped.", "action": "stop"}
    else:
        return {"content": "No running turn to stop."}