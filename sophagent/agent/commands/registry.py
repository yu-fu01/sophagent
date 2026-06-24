"""Slash command registry — inspired by hermes-agent's hermes_cli/commands.py.

Central registry for all slash commands. Every consumer — REST API, frontend
palette, help text — derives its data from this module.

To add a command: add a ``CommandDef`` entry via ``register()``.
To add an alias: set ``aliases=("short",)`` on the ``CommandDef``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CommandDef dataclass (mirrors hermes-agent's CommandDef)
# ---------------------------------------------------------------------------

@dataclass
class CommandDef:
    """Definition of a single slash command."""

    name: str                          # canonical name without slash: "help"
    description: str                   # human-readable description
    category: str = "General"          # "Session", "Configuration", etc.
    aliases: tuple[str, ...] = ()      # alternative names
    args_hint: str = ""                # argument placeholder: "<model>", "[text]"
    handler: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None  # async handler


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_commands: dict[str, CommandDef] = {}      # name/alias -> CommandDef
_command_list: list[CommandDef] = []        # ordered list (registration order)


def register(cmd: CommandDef) -> None:
    """Register a command and its aliases."""
    _commands[cmd.name] = cmd
    for alias in cmd.aliases:
        _commands[alias] = cmd
    # only add canonical name once
    if not any(c.name == cmd.name for c in _command_list):
        _command_list.append(cmd)


def resolve(name: str) -> CommandDef | None:
    """Resolve a command name or alias (with or without leading slash)."""
    return _commands.get(name.lower().lstrip("/"))


def get_commands() -> list[dict[str, Any]]:
    """Return serializable list of all registered commands (for API / frontend)."""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for cmd in _command_list:
        if cmd.name in seen:
            continue
        seen.add(cmd.name)
        result.append({
            "name": cmd.name,
            "description": cmd.description,
            "category": cmd.category,
            "args_hint": cmd.args_hint,
            "aliases": list(cmd.aliases),
        })
    return result


async def dispatch(
    raw: str,
    db: Any,
    session: dict[str, Any] | None,
    user: dict[str, Any],
    manager: Any,
    skill_store: Any = None,
) -> dict[str, Any]:
    """Parse and dispatch a slash command.

    Args:
        raw: Full user input (e.g. ``"/model claude-sonnet-4"``).
        db: Database handle.
        session: Current session dict, or None.
        user: Current user dict.
        manager: SessionManager (for stop, etc.).
        skill_store: Optional SkillStore.

    Returns:
        Dict with keys ``handled``, ``type`` (``"result"`` or ``"error"``),
        ``content``, and optionally ``action``.
    """
    raw = raw.strip()
    if not raw.startswith("/"):
        return {"handled": False}

    parts = raw.split(maxsplit=1)
    cmd_name = parts[0].lstrip("/").lower()
    args = parts[1].strip() if len(parts) > 1 else ""

    cmd_def = resolve(cmd_name)
    if cmd_def is None:
        return {
            "handled": True,
            "type": "error",
            "content": f"Unknown command: /{cmd_name}. Type /help to see available commands.",
        }

    if cmd_def.handler is None:
        return {
            "handled": True,
            "type": "error",
            "content": f"Command /{cmd_name} has no handler registered.",
        }

    ctx = {
        "db": db,
        "session": session,
        "user": user,
        "manager": manager,
        "skill_store": skill_store,
    }

    try:
        result = await cmd_def.handler(args, ctx)
        return {"handled": True, "type": "result", **result}
    except Exception as e:
        log.exception("command /%s failed", cmd_name)
        return {"handled": True, "type": "error", "content": f"Command /{cmd_name} failed: {e}"}
