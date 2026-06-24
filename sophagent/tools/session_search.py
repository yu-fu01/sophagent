"""session_search: FTS5 full-text recall over the user's own past conversations.

Three calling shapes, inferred from arguments (no `mode` parameter):

* **discovery** — pass ``query``: FTS5 search, dedupe by session, top N sessions
  each with a highlighted snippet + a window of messages around the match.
* **scroll** — pass ``session_id`` + ``around_message_id``: a ±``window`` slice of
  that session, for pulling more context after a discovery hit.
* **browse** — no args: most recent sessions with a short preview.

Every shape is scoped to ``ctx.user_id`` — an agent can only search the calling
user's sessions, never another user's.
"""

from __future__ import annotations

from .registry import ToolContext, tool


def _window(texts: list[tuple[int, str, str]], anchor_id: int, window: int):
    """Return (slice, before_count, after_count) of ±window messages centred on
    the message whose id == anchor_id. If the anchor isn't found, returns empty."""
    idx = next((i for i, (mid, _, _) in enumerate(texts) if mid == anchor_id), None)
    if idx is None:
        return [], 0, 0
    start = max(0, idx - window)
    end = min(len(texts), idx + window + 1)
    return texts[start:end], idx - start, end - idx - 1


def _fmt_window(texts, anchor_id: int) -> list[str]:
    lines = []
    for mid, role, text in texts:
        marker = ">" if mid == anchor_id else " "
        lines.append(f"  {marker} [{mid}|{role}] {text}")
    return lines


async def _discovery(ctx: ToolContext, query: str, limit: int, window: int) -> str:
    db = ctx.db
    try:
        hits = await db.search_messages(ctx.user_id, query, limit_rows=max(limit * 10, 50))
    except Exception as e:  # malformed FTS5 query syntax, etc.
        return f"Error: invalid search query ({e})"
    if not hits:
        return f"No past messages match {query!r}."

    blocks: list[str] = []
    seen: set[str] = set()
    for h in hits:
        sid = h["session_id"]
        if sid in seen:
            continue  # best (highest-ranked) hit per session only
        seen.add(sid)
        session = await db.get_session(sid, ctx.user_id)
        if session is None:
            continue
        texts = await db.session_texts(sid)
        win, before, after = _window(texts, h["message_id"], window)
        title = session["title"] or "(untitled)"
        head = f'Session {sid} — "{title}" ({session["updated_at"]})'
        snippet = (h["snippet"] or "").strip()
        body = "\n".join(_fmt_window(win, h["message_id"]))
        blocks.append(
            f"{head}\n  match: {snippet}\n{body}\n  "
            f"(match id {h['message_id']}; {before} before, {after} after in window)"
        )
        if len(blocks) >= limit:
            break
    if not blocks:
        return f"No past messages match {query!r}."
    return "\n\n".join(blocks)


async def _scroll(ctx: ToolContext, session_id: str, around: int, window: int) -> str:
    db = ctx.db
    session = await db.get_session(session_id, ctx.user_id)
    if session is None:
        return "Error: session not found"  # also guards cross-user access
    texts = await db.session_texts(session_id)
    win, before, after = _window(texts, around, window)
    if not win:
        return f"Error: message {around} not found in session {session_id}"
    body = "\n".join(_fmt_window(win, around))
    return (
        f"Session {session_id} window around message {around}:\n{body}\n"
        f"({before} before, {after} after — fewer than window means you hit a boundary)"
    )


async def _browse(ctx: ToolContext, limit: int) -> str:
    db = ctx.db
    rows = await db.recent_sessions(ctx.user_id, limit)
    if not rows:
        return "No sessions yet."
    lines = ["Recent sessions:"]
    for r in rows:
        texts = await db.session_texts(r["id"])
        preview = next((t for _, role, t in texts if role == "user"), "")
        if len(preview) > 80:
            preview = preview[:80] + "…"
        title = r["title"] or "(untitled)"
        lines.append(f'- {r["id"]} "{title}" ({r["updated_at"]}): {preview}')
    return "\n".join(lines)


@tool(
    "session_search",
    "Search and recall your past conversations with this user (FTS5, no LLM "
    "calls). Three shapes inferred from args: pass `query` to find sessions by "
    "keyword (FTS5 syntax: AND default, OR, NOT, \"phrases\", prefix*); pass "
    "`session_id`+`around_message_id` to scroll a window of one session; pass "
    "nothing to list recent sessions. Only your own sessions are searched.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords (discovery shape)"},
            "session_id": {"type": "string", "description": "Session to scroll"},
            "around_message_id": {"type": "integer", "description": "Anchor for scroll"},
            "limit": {"type": "integer", "description": "Max sessions (default 5)"},
            "window": {"type": "integer", "description": "±messages of context (default 5)"},
        },
    },
)
async def session_search(
    ctx: ToolContext,
    query: str = "",
    session_id: str = "",
    around_message_id: int = 0,
    limit: int = 5,
    window: int = 5,
) -> str:
    if ctx.db is None:
        return "Error: session search unavailable (no database)"
    limit = max(1, min(limit, 20))
    window = max(1, min(window, 50))
    if query.strip():
        return await _discovery(ctx, query.strip(), limit, window)
    if session_id and around_message_id:
        return await _scroll(ctx, session_id, around_message_id, window)
    return await _browse(ctx, limit)
