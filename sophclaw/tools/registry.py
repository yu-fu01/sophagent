"""Tool registry: @tool decorator, ToolContext, dispatch with timeout/truncation."""

from __future__ import annotations

import asyncio
import inspect
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..config import get_config
from ..models import AgentDef

Handler = Callable[..., Awaitable[str]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler

    def schema(self) -> dict[str, Any]:
        """OpenAI function schema (the internal standard)."""
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass
class ToolContext:
    """Per-call context handed to every tool handler."""

    user_id: int
    workspace: Path
    agent: AgentDef
    depth: int = 0  # delegate nesting depth
    db: Any = None  # Database (set when running inside the server)
    skill_store: Any = None  # SkillStore
    services: dict[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None  # 当前会话 id（定时任务结果可绑定到此处）


_TOOLS: dict[str, ToolSpec] = {}


def tool(name: str, description: str, parameters: dict[str, Any]):
    """Register an async handler(ctx, **arguments) -> str."""

    def deco(fn: Handler) -> Handler:
        _TOOLS[name] = ToolSpec(name=name, description=description, parameters=parameters, handler=fn)
        return fn

    return deco


def all_tool_names() -> list[str]:
    return sorted(_TOOLS)


def get_schemas(names: list[str]) -> list[dict[str, Any]]:
    return [_TOOLS[n].schema() for n in names if n in _TOOLS]


def truncate(text: str, limit: int | None = None) -> str:
    limit = limit or get_config().tool_output_limit
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} chars omitted]"


async def dispatch(name: str, arguments: dict[str, Any], ctx: ToolContext) -> str:
    """Run a tool; errors come back as text so the model can self-correct."""
    spec = _TOOLS.get(name)
    if spec is None:
        return f"Error: unknown tool {name!r}"
    if name not in ctx.agent.tools:
        return f"Error: tool {name!r} is not enabled for this agent"
    # drop hallucinated kwargs instead of crashing
    sig = inspect.signature(spec.handler)
    accepts_kwargs = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
    if not accepts_kwargs:
        arguments = {k: v for k, v in arguments.items() if k in sig.parameters}
    try:
        result = await asyncio.wait_for(
            spec.handler(ctx, **arguments), timeout=get_config().tool_timeout * 10
        )
        return truncate(result if isinstance(result, str) else str(result))
    except asyncio.TimeoutError:
        return f"Error: tool {name!r} timed out"
    except TypeError as e:
        return f"Error: bad arguments for {name!r}: {e}"
    except Exception:
        tb = traceback.format_exc(limit=3)
        return truncate(f"Error: tool {name!r} raised an exception:\n{tb}", 2000)
