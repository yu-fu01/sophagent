"""/memory — review and act on write-approval staged memory writes.

Subcommands:
  /memory pending            list your staged memory writes
  /memory approve <id|all>   apply staged write(s) through the normal write path
  /memory reject  <id|all>   drop staged write(s) without applying
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_USAGE = (
    "Usage:\n"
    "  `/memory pending` — list staged writes\n"
    "  `/memory approve <id|all>` — apply\n"
    "  `/memory reject <id|all>` — drop"
)


def _line(row) -> str:
    tag = " [auto]" if row["origin"] == "review" else ""
    body = row["content"] if row["op"] != "remove" else f"#{row['memory_id']}"
    preview = body if len(body) <= 80 else body[:80] + "…"
    return f"[{row['id']}] {row['op']} {row['target']}: {preview}{tag}"


def _failed(result: str) -> bool:
    return result.startswith("Error") or "Consolidate now" in result


async def handle(args: str, ctx: dict) -> dict:
    db = ctx["db"]
    user_id = ctx["user"]["id"]
    from ....config import effective_write_approval, get_config
    from ....tools.memory import apply_pending_write

    parts = args.split()
    sub = parts[0].lower() if parts else "pending"
    arg = parts[1] if len(parts) > 1 else ""

    if sub == "pending":
        rows = await db.pending_list(user_id)
        gate = "on" if await effective_write_approval(db) else "off"
        if not rows:
            return {"content": f"No pending memory writes. (write_approval: {gate})"}
        listing = "\n".join(_line(r) for r in rows)
        return {"content": f"Pending memory writes (write_approval: {gate}):\n{listing}"}

    if sub == "approve":
        cfg = get_config()
        if arg == "all":
            rows = await db.pending_list(user_id)
            if not rows:
                return {"content": "No pending memory writes."}
            results = []
            for r in rows:
                out = await apply_pending_write(db, user_id, r, cfg)
                if not _failed(out):
                    await db.pending_remove(r["id"], user_id)
                results.append(f"[{r['id']}] {out}")
            return {"content": "\n".join(results)}
        if not arg.isdigit():
            return {"content": _USAGE}
        row = await db.pending_get(int(arg), user_id)
        if row is None:
            return {"content": f"Pending [{arg}] not found."}
        out = await apply_pending_write(db, user_id, row, cfg)
        if not _failed(out):
            await db.pending_remove(row["id"], user_id)
        return {"content": out}

    if sub == "reject":
        if arg == "all":
            n = await db.pending_clear(user_id)
            return {"content": f"Rejected {n} pending memory write(s)."}
        if not arg.isdigit():
            return {"content": _USAGE}
        ok = await db.pending_remove(int(arg), user_id)
        return {"content": f"Rejected pending [{arg}]." if ok else f"Pending [{arg}] not found."}

    return {"content": _USAGE}
