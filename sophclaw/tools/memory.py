"""Long-term per-user memory. A frozen snapshot is injected into the system
prompt at the start of each turn; edits become visible next turn."""

from __future__ import annotations

from ..config import get_config
from .registry import ToolContext, tool


@tool(
    "memory",
    "Manage long-term memory about this user (persists across sessions). "
    "Keep entries short and factual. Actions: read, add, replace, remove.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "add", "replace", "remove"]},
            "content": {"type": "string", "description": "Entry text (add/replace)"},
            "memory_id": {"type": "integer", "description": "Entry id (replace/remove)"},
        },
        "required": ["action"],
    },
)
async def memory(ctx: ToolContext, action: str, content: str = "", memory_id: int = 0) -> str:
    db = ctx.db
    if db is None:
        return "Error: memory unavailable (no database)"
    cfg = get_config()
    if action == "read":
        rows = await db.memory_list(ctx.user_id)
        if not rows:
            return "No memories stored."
        return "\n".join(f"[{r['id']}] {r['content']}" for r in rows)
    if action == "add":
        if not content.strip():
            return "Error: content required"
        if len(content) > cfg.memory_max_chars:
            return f"Error: entry too long (max {cfg.memory_max_chars} chars)"
        rows = await db.memory_list(ctx.user_id)
        if len(rows) >= cfg.memory_max_items:
            return f"Error: memory full ({cfg.memory_max_items} entries); remove or replace one first"
        mid = await db.memory_add(ctx.user_id, content.strip())
        return f"Saved memory [{mid}]"
    if action == "replace":
        if not content.strip():
            return "Error: content required"
        if len(content) > cfg.memory_max_chars:
            return f"Error: entry too long (max {cfg.memory_max_chars} chars)"
        ok = await db.memory_replace(memory_id, ctx.user_id, content.strip())
        return f"Updated memory [{memory_id}]" if ok else f"Error: memory [{memory_id}] not found"
    if action == "remove":
        ok = await db.memory_remove(memory_id, ctx.user_id)
        return f"Removed memory [{memory_id}]" if ok else f"Error: memory [{memory_id}] not found"
    return f"Error: unknown action {action!r}"
