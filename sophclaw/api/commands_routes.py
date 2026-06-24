"""REST API routes for slash command discovery."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from ..agent.commands import get_commands_list
from ..agent.commands.registry import resolve
from ..auth import require_user

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def list_commands(user=Depends(require_user)):
    """Return list of available slash commands (for frontend autocomplete)."""
    return get_commands_list()


@router.get("/suggestions")
async def get_command_suggestions(
    request: Request,
    cmd: str,
    session_id: str | None = None,
    user=Depends(require_user),
):
    """Return suggestions for a given slash command, resolved dynamically.

    For commands with static subcommands (e.g. /cron), returns them directly.
    For commands with a suggestion_resolver (e.g. /model), calls the resolver
    with the current session context.
    """
    cmd_def = resolve(cmd.lstrip("/"))
    if cmd_def is None:
        return {"suggestions": []}

    # Static subcommands — already available
    if cmd_def.subcommands:
        return {"suggestions": [
            {"label": s.label, "description": s.description, "args_hint": s.args_hint}
            for s in cmd_def.subcommands
        ]}

    # Dynamic resolver — needs session context
    if cmd_def.suggestion_resolver:
        db = request.app.state.db
        session = None
        if session_id:
            session = await db.get_session(session_id, user["id"])
        ctx = {"db": db, "user": user}
        suggestions = await cmd_def.suggestion_resolver(session, ctx)
        return {"suggestions": [
            {"label": s.label, "description": s.description, "args_hint": s.args_hint}
            for s in suggestions
        ]}

    return {"suggestions": []}