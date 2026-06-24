"""/help — list available slash commands."""

from __future__ import annotations

from ..registry import get_commands


async def handle(args: str, ctx: dict) -> dict:
    """Return a formatted list of available commands."""
    cmds = get_commands()
    lines = ["**可用命令：**\n"]
    # Group by category
    by_cat: dict[str, list[dict]] = {}
    for c in cmds:
        by_cat.setdefault(c["category"], []).append(c)

    for cat in sorted(by_cat.keys()):
        lines.append(f"**{cat}**")
        for c in by_cat[cat]:
            hint = f" {c['args_hint']}" if c["args_hint"] else ""
            aliases = ""
            if c["aliases"]:
                aliases = f"（别名：/{' /'.join(c['aliases'])}）"
            lines.append(f"  • `/{c['name']}{hint}` — {c['description']}{aliases}")
        lines.append("")

    return {"content": "\n".join(lines)}
