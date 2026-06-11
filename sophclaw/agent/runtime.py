"""Shared construction of ToolContext + AgentRunner (used by the session API,
the OpenAI-compat layer and delegate_task)."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from ..config import get_config
from ..models import AgentDef, Message
from ..tools.registry import ToolContext
from .loop import AgentRunner


async def build_runner(
    *,
    db: Any,
    skill_store: Any,
    agent: AgentDef,
    user_id: int,
    history: list[Message],
    on_persist: Optional[Callable[[list[Message]], Awaitable[None]]] = None,
    depth: int = 0,
) -> AgentRunner:
    cfg = get_config()
    memories = [r["content"] for r in await db.memory_list(user_id)] if db else []
    ctx = ToolContext(
        user_id=user_id,
        workspace=cfg.workspace_for(user_id),
        agent=agent,
        depth=depth,
        db=db,
        skill_store=skill_store,
        services={"memories": memories},
    )
    return AgentRunner(agent, ctx, history, on_persist=on_persist)
