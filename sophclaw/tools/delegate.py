"""delegate_task: spawn a sub-agent for a focused goal.

Guards (mirroring hermes delegate_tool): max nesting depth 1, a global
concurrency semaphore, and a per-task timeout. Sub-agents share the parent
user's workspace but run an in-memory history (not persisted as a session).
"""

from __future__ import annotations

import asyncio

from ..config import get_config
from ..models import AgentDef
from .registry import ToolContext, tool

MAX_DEPTH = 1

_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(get_config().delegate_concurrency)
    return _semaphore


@tool(
    "delegate_task",
    "Delegate a focused sub-task to another agent (or a fresh copy of yourself). "
    "The sub-agent works in your workspace and returns its final answer. "
    "Use for parallelizable or self-contained chunks of work.",
    {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "Complete, self-contained task description"},
            "agent_name": {"type": "string", "description": "Agent definition to use (default: current agent)"},
            "context": {"type": "string", "description": "Extra background the sub-agent needs"},
        },
        "required": ["goal"],
    },
)
async def delegate_task(ctx: ToolContext, goal: str, agent_name: str = "", context: str = "") -> str:
    if ctx.depth >= MAX_DEPTH:
        return "Error: maximum delegation depth reached; do this task yourself"
    if ctx.db is None:
        return "Error: delegation unavailable (no database)"

    from ..agent.runtime import build_runner  # late import to avoid a cycle

    agent = ctx.agent
    if agent_name and agent_name != ctx.agent.name:
        row = await ctx.db.get_agent_by_name(agent_name)
        if row is None:
            return f"Error: unknown agent {agent_name!r}"
        agent = AgentDef.from_row(row)
        if "delegate_task" in agent.tools:
            # depth guard also applies, but don't even offer the tool downstream
            agent.tools = [t for t in agent.tools if t != "delegate_task"]

    prompt = goal if not context else f"{goal}\n\nBackground:\n{context}"
    cfg = get_config()

    async def run_sub() -> str:
        runner = await build_runner(
            db=ctx.db, skill_store=ctx.skill_store, agent=agent,
            user_id=ctx.user_id, history=[], depth=ctx.depth + 1,
        )
        tool_count = 0
        async for ev in runner.run(prompt):
            if ev["type"] == "tool_call":
                tool_count += 1
            elif ev["type"] == "error":
                return f"Error: sub-agent failed: {ev['message']}"
        answer = runner.final_text() or "(sub-agent produced no text)"
        return f"{answer}\n\n[sub-agent: {agent.name}, {tool_count} tool calls]"

    async with _get_semaphore():
        try:
            return await asyncio.wait_for(run_sub(), timeout=cfg.delegate_timeout)
        except asyncio.TimeoutError:
            return f"Error: sub-task timed out after {cfg.delegate_timeout:.0f}s"
