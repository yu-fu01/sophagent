"""Long-term per-user memory. A frozen snapshot is injected into the system
prompt at the start of each turn; edits become visible next turn."""

from __future__ import annotations

from dataclasses import dataclass

from ..agent.memory_guard import scan_memory
from ..config import effective_write_approval, get_config
from .registry import ToolContext, tool


@dataclass
class WriteCheck:
    """Outcome of validating a prospective memory write against the per-target
    char budget. ``status`` is one of ok | too_long | duplicate | over_capacity;
    ``used`` / ``limit`` are populated for over_capacity to drive the guided
    consolidation prompt."""

    status: str
    used: int = 0
    limit: int = 0


def check_write(
    *,
    new_content: str,
    existing_contents: list[str],
    used_chars: int,
    max_chars: int,
    total_limit: int,
    check_dup: bool = True,
) -> WriteCheck:
    """Classify a memory add/replace before it touches the DB. ``used_chars`` is
    the current char total of the target store (excluding the row being replaced,
    for replace)."""
    if len(new_content) > max_chars:
        return WriteCheck("too_long")
    if check_dup and new_content in existing_contents:
        return WriteCheck("duplicate")
    if used_chars + len(new_content) > total_limit:
        return WriteCheck("over_capacity", used=used_chars, limit=total_limit)
    return WriteCheck("ok")


TARGETS = ("memory", "user")


def _limit_for(cfg, target: str) -> int:
    return cfg.user_total_chars if target == "user" else cfg.memory_total_chars


def _over_capacity_msg(target: str, n: int, used: int, limit: int, entries: list) -> str:
    """hermes-style guided-consolidation prompt: list current entries so the
    agent can merge/remove them and retry the add in the same turn."""
    listing = "\n".join(f"[{r['id']}] {r['content']}" for r in entries) or "(none)"
    return (
        f"{target} memory at {used}/{limit} chars. Adding this entry ({n} chars) "
        f"would exceed the limit. Consolidate now: use 'replace' to merge "
        f"overlapping entries into shorter ones, or 'remove' stale entries "
        f"(see current_entries below), then retry this add — all in this turn.\n"
        f"current_entries:\n{listing}"
    )


@tool(
    "memory",
    "Manage long-term memory about this user (persists across sessions). "
    "Two stores via `target`: 'memory' = your own notes (environment facts, "
    "conventions, lessons learned); 'user' = the user's profile (identity, "
    "preferences, communication style). Keep entries short and factual. "
    "Actions: read, add, replace, remove.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "add", "replace", "remove"]},
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which store to operate on (default: memory)",
            },
            "content": {"type": "string", "description": "Entry text (add/replace)"},
            "memory_id": {"type": "integer", "description": "Entry id (replace/remove)"},
        },
        "required": ["action"],
    },
)
async def memory(
    ctx: ToolContext,
    action: str,
    content: str = "",
    memory_id: int = 0,
    target: str = "memory",
) -> str:
    db = ctx.db
    if db is None:
        return "Error: memory unavailable (no database)"
    if target not in TARGETS:
        return f"Error: target must be one of {TARGETS}"
    cfg = get_config()
    limit = _limit_for(cfg, target)

    if action == "read":
        rows = await db.memory_list(ctx.user_id, target=target)
        if not rows:
            return f"No {target} memories stored."
        return "\n".join(f"[{r['id']}] {r['content']}" for r in rows)

    gated = await effective_write_approval(db)
    origin = ctx.services.get("write_origin", "foreground")

    async def _stage(op: str, *, mid: int | None = None) -> str:
        pid = await db.pending_add(
            ctx.user_id, op=op, target=target, content=content,
            memory_id=mid, origin=origin,
        )
        return f"Staged {op} for approval [pending {pid}] — review with /memory pending"

    if action == "add":
        content = content.strip()
        if not content:
            return "Error: content required"
        reason = scan_memory(content)
        if reason:
            return f"Error: rejected by safety scan ({reason})"
        if gated:
            return await _stage("add")
        return await do_add(db, ctx.user_id, target, content, cfg)

    if action == "replace":
        content = content.strip()
        if not content:
            return "Error: content required"
        reason = scan_memory(content)
        if reason:
            return f"Error: rejected by safety scan ({reason})"
        rows = await db.memory_list(ctx.user_id, target=target)
        if not any(r["id"] == memory_id for r in rows):
            return f"Error: {target} memory [{memory_id}] not found"
        if gated:
            return await _stage("replace", mid=memory_id)
        return await do_replace(db, ctx.user_id, target, content, memory_id, cfg)

    if action == "remove":
        if gated:
            rows = await db.memory_list(ctx.user_id, target=target)
            if not any(r["id"] == memory_id for r in rows):
                return f"Error: {target} memory [{memory_id}] not found"
            return await _stage("remove", mid=memory_id)
        return await do_remove(db, ctx.user_id, memory_id)

    return f"Error: unknown action {action!r}"


# -- shared write executors (used by the tool's non-gated path and by approval) --


async def do_add(db, user_id: int, target: str, content: str, cfg) -> str:
    limit = _limit_for(cfg, target)
    rows = await db.memory_list(user_id, target=target)
    used = sum(len(r["content"]) for r in rows)
    chk = check_write(
        new_content=content, existing_contents=[r["content"] for r in rows],
        used_chars=used, max_chars=cfg.memory_max_chars, total_limit=limit,
    )
    if chk.status == "too_long":
        return f"Error: entry too long (max {cfg.memory_max_chars} chars)"
    if chk.status == "duplicate":
        return "OK (no duplicate added — that entry already exists)"
    if chk.status == "over_capacity":
        return _over_capacity_msg(target, len(content), chk.used, chk.limit, rows)
    mid = await db.memory_add(user_id, content, target=target)
    return f"Saved {target} memory [{mid}]"


async def do_replace(db, user_id: int, target: str, content: str, memory_id: int, cfg) -> str:
    limit = _limit_for(cfg, target)
    rows = await db.memory_list(user_id, target=target)
    if not any(r["id"] == memory_id for r in rows):
        return f"Error: {target} memory [{memory_id}] not found"
    # capacity excludes the row being replaced (it's making way for the new one)
    others = [r for r in rows if r["id"] != memory_id]
    used = sum(len(r["content"]) for r in others)
    chk = check_write(
        new_content=content, existing_contents=[r["content"] for r in others],
        used_chars=used, max_chars=cfg.memory_max_chars, total_limit=limit, check_dup=False,
    )
    if chk.status == "too_long":
        return f"Error: entry too long (max {cfg.memory_max_chars} chars)"
    if chk.status == "over_capacity":
        return _over_capacity_msg(target, len(content), chk.used, chk.limit, others)
    await db.memory_replace(memory_id, user_id, content)
    return f"Updated {target} memory [{memory_id}]"


async def do_remove(db, user_id: int, memory_id: int) -> str:
    ok = await db.memory_remove(memory_id, user_id)
    return f"Removed memory [{memory_id}]" if ok else f"Error: memory [{memory_id}] not found"


async def apply_pending_write(db, user_id: int, row, cfg) -> str:
    """Execute a previously-staged memory op, bypassing the approval gate
    (approving IS the release). Re-runs the normal write checks (scan / capacity
    / dedup), so an approved add that no longer fits still surfaces the
    consolidation prompt instead of silently landing."""
    op, target = row["op"], row["target"]
    content, mid = row["content"], row["memory_id"]
    if op in ("add", "replace"):
        reason = scan_memory(content)
        if reason:
            return f"Error: rejected by safety scan ({reason})"
    if op == "add":
        return await do_add(db, user_id, target, content, cfg)
    if op == "replace":
        return await do_replace(db, user_id, target, content, mid, cfg)
    if op == "remove":
        return await do_remove(db, user_id, mid)
    return f"Error: unknown pending op {op!r}"
