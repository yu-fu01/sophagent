"""/model — switch model for the current session."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def handle(args: str, ctx: dict) -> dict:
    """Set override_model on the session."""
    db = ctx["db"]
    session = ctx["session"]
    user = ctx["user"]

    if not session:
        return {"content": "No active session. Create one first."}

    if not args:
        return {"content": f"Current model override: {session.get('override_model') or '(none — using agent default)'}\n\nUsage: `/model <model-name>`"}

    model_name = args.strip()
    session_id = session["id"]

    await db.set_session_overrides(session_id, override_model=model_name)
    log.info("session %s model overridden to %s by user %d", session_id, model_name, user["id"])

    return {"content": f"✅ Model switched to **{model_name}** for this session."}