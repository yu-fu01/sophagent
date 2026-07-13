"""IM binding summaries per agent."""

from __future__ import annotations

from typing import Any

IM_PLATFORMS: tuple[str, ...] = ("dingtalk", "feishu", "weixin", "qqbot")


async def binding_summary_for_agent(db, agent_id: int) -> dict[str, Any]:
    """Return per-platform binding status for one agent."""
    rows = await db.list_bindings_for_agent(agent_id)
    grouped: dict[str, list[dict[str, str]]] = {p: [] for p in IM_PLATFORMS}
    for row in rows:
        platform = str(row["platform"] or "").strip()
        if platform not in grouped:
            continue
        grouped[platform].append(
            {
                "chat_id": str(row["chat_id"] or ""),
                "session_id": str(row["session_id"] or ""),
                "created_at": str(row["created_at"] or ""),
            }
        )
    platforms: dict[str, dict[str, Any]] = {}
    for platform in IM_PLATFORMS:
        sessions = grouped[platform]
        platforms[platform] = {
            "bound": bool(sessions),
            "count": len(sessions),
            "sessions": sessions,
        }
    return {"agent_id": agent_id, "platforms": platforms}
