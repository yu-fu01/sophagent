"""Shared construction of ToolContext + AgentRunner (used by the session API,
the OpenAI-compat layer and delegate_task)."""

from __future__ import annotations

import logging
from dataclasses import replace as dc_replace
from typing import Any, Awaitable, Callable, Optional

from ..config import DEFAULT_COMPRESS_THRESHOLD, effective_compress_threshold, get_config
from ..models import AgentDef, Message
from ..tools.registry import ToolContext
from .loop import AgentRunner

log = logging.getLogger(__name__)


async def build_runner(
    *,
    db: Any,
    skill_store: Any,
    agent: AgentDef,
    user_id: int,
    history: list[Message],
    on_persist: Optional[Callable[[list[Message]], Awaitable[None]]] = None,
    depth: int = 0,
    override_provider: Optional[str] = None,
    override_model: Optional[str] = None,
    thinking_mode: Optional[str] = None,
) -> AgentRunner:
    cfg = get_config()
    # dual-store: group entries by target ('memory' = agent notes, 'user' = profile)
    memories: dict[str, list[str]] = {"memory": [], "user": []}
    if db:
        for r in await db.memory_list(user_id):
            memories.setdefault(r["target"], []).append(r["content"])
    ctx = ToolContext(
        user_id=user_id,
        workspace=cfg.workspace_for(user_id),
        agent=agent,
        depth=depth,
        db=db,
        skill_store=skill_store,
        services={"memories": memories},
    )
    eff_provider = agent.provider
    if override_provider:
        from ..providers.registry import get_registry
        try:
            get_registry().resolve(override_provider)
            eff_provider = override_provider
        except (RuntimeError, KeyError):
            log.warning("session override_provider %r not in registry; falling back to agent provider %r",
                        override_provider, agent.provider)
    eff = dc_replace(agent, provider=eff_provider, model=override_model or agent.model)
    ctx.agent = eff  # 让工具上下文也用 effective agent
    threshold = await effective_compress_threshold(db) if db else DEFAULT_COMPRESS_THRESHOLD
    runner = AgentRunner(eff, ctx, history, on_persist=on_persist, compress_threshold=threshold)
    runner.thinking = thinking_mode if thinking_mode and thinking_mode != "default" else None
    return runner
