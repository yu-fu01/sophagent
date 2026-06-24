"""REST API routes for slash command discovery."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..agent.commands import get_commands_list
from ..auth import require_user

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def list_commands(user=Depends(require_user)):
    """Return list of available slash commands (for frontend autocomplete)."""
    return get_commands_list()