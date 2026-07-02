"""AgentRunner: the core model<->tools loop with two-layer context compression."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from ..config import DEFAULT_COMPRESS_THRESHOLD, get_config
from ..models import AgentDef, Message, StreamEvent, ToolCall
from ..providers import get_provider
from ..tools import registry
from ..usage import cache_hit_percent
from .compaction import (
    KEEP_RECENT_TOOL_MSGS,
    TOOL_TRUNCATE_NOTE,
    estimate_tokens,
    find_previous_summary,
    history_tokens,
    is_summary_message,
    make_summary_message,
    split_for_summary,
    summarize,
    truncate_old_tool_messages,
)
from .redact import redact_known_secrets

# Re-export compaction primitives so existing import paths
# (e.g. `from sophagent.agent.loop import history_tokens`) keep working.
__all__ = [
    "AgentRunner",
    "expand_parallel_calls",
    "estimate_tokens",
    "history_tokens",
    "truncate_old_tool_messages",
    "KEEP_RECENT_TOOL_MSGS",
    "TOOL_TRUNCATE_NOTE",
]

log = logging.getLogger(__name__)

PARALLEL_TOOL = "multi_tool_use.parallel"


def expand_parallel_calls(tool_calls: list[ToolCall]) -> list[ToolCall]:
    """Flatten any multi_tool_use.parallel pseudo-call into real ToolCalls.

    GPT-family models sometimes bundle parallel tool calls into a single call
    named ``multi_tool_use.parallel`` whose arguments carry a ``tool_uses`` list.
    Each item names a tool (``recipient_name`` or ``name``) and its parameters
    (``parameters`` or ``arguments``). We expand those into individual ToolCalls
    so the normal dispatch path runs them.

    Malformed wrappers (missing/invalid ``tool_uses``, or no usable sub-call)
    are left as the original pseudo-call so dispatch returns an error under the
    *same* tool_call id the assistant message already carries — keeping history
    consistent (every tool_call has a matching tool reply).
    """
    out: list[ToolCall] = []
    for tc in tool_calls:
        if tc.name != PARALLEL_TOOL:
            out.append(tc)
            continue
        uses = tc.arguments.get("tool_uses")
        if not isinstance(uses, list):
            out.append(tc)  # malformed → dispatch reports the pseudo-tool error
            continue
        expanded: list[ToolCall] = []
        for use in uses:
            if not isinstance(use, dict):
                continue
            name = (use.get("recipient_name") or use.get("name") or "").removeprefix("functions.")
            if not name:
                continue
            args = use.get("parameters")
            if args is None:
                args = use.get("arguments")
            if not isinstance(args, dict):
                args = {}
            expanded.append(ToolCall(id=f"{tc.id}.{len(expanded)}", name=name, arguments=args))
        out.extend(expanded if expanded else [tc])
    return out


RETRYABLE_ATTEMPTS = 3
TAIL_BUDGET_RATIO = 0.25  # 每次 LLM 摘要保护的尾部上下文占 context_limit 的比例


def _resolve_context_limit(provider_name: str) -> int:
    """Context limit from the provider registry; fall back to static config when
    the registry isn't initialized (e.g. unit tests constructing AgentRunner directly)."""
    from ..providers.registry import get_registry
    try:
        return get_registry().resolve(provider_name).context_limit
    except (RuntimeError, KeyError):
        return get_config().providers[provider_name].context_limit


class AgentRunner:
    """Runs one conversation turn. Stateless between turns: history is loaded
    from the DB, mutated in memory, and persisted via the on_persist callback."""

    def __init__(
        self,
        agent: AgentDef,
        ctx: registry.ToolContext,
        history: list[Message],
        on_persist: Optional[Callable[[list[Message]], Awaitable[None]]] = None,
        compress_threshold: float = DEFAULT_COMPRESS_THRESHOLD,
    ):
        self.agent = agent
        self.ctx = ctx
        self.history = history
        self._on_persist = on_persist
        self._new_messages: list[Message] = []
        self.provider = get_provider(agent.provider)
        self.context_limit = _resolve_context_limit(agent.provider)
        self.usage = {"input_tokens": 0, "output_tokens": 0,
                      "cache_read_tokens": 0, "cache_write_tokens": 0}
        # runner-level flag: history was rewritten this turn → caller may compact the DB.
        # NOTE: distinct from Message.compressed (which marks a single summary message).
        self.compressed = False
        self.compress_threshold = compress_threshold
        self.thinking: str | None = None

    async def _persist(self, msg: Message) -> None:
        self._new_messages.append(msg)
        if self._on_persist:
            await self._on_persist([msg])

    def _build_system(self) -> str:
        from .prompt import build_system_prompt

        skill_index = None
        if self.ctx.skill_store is not None:
            from ..skills.store import filter_index_for_agent
            skill_index = self.ctx.skill_store.index(self.agent.skills)
            skill_index = filter_index_for_agent(skill_index, self.agent)
        memories = self.ctx.services.get("memories")
        return build_system_prompt(self.agent, skill_index, memories, self.ctx.workspace)

    async def _compress_if_needed(self, system: str) -> None:
        budget = int(self.context_limit * self.compress_threshold)
        if history_tokens(system, self.history) <= budget:
            return
        before = [m.content for m in self.history]
        self.history = truncate_old_tool_messages(self.history)
        if [m.content for m in self.history] != before:
            self.compressed = True
        for _ in range(3):
            if history_tokens(system, self.history) <= budget or len(self.history) < 4:
                return
            await self._summarize_oldest_half()

    async def _summarize_oldest_half(self) -> None:
        """Layer 2: replace older turns with a structured LLM summary, protecting
        a token-budgeted tail and iteratively merging any prior summary."""
        tail_budget = int(self.context_limit * TAIL_BUDGET_RATIO)
        old, rest = split_for_summary(self.history, tail_budget)
        if not old:
            return
        prev = find_previous_summary(self.history)
        turns = [m for m in old if not is_summary_message(m)]
        summary = await summarize(self.provider, self.agent.model, turns, prev_summary=prev)
        if summary:
            self.history = [make_summary_message(summary)] + rest
        else:
            log.warning("summary compression failed; falling back to hard drop")
            self.history = ([make_summary_message(prev)] if prev else []) + rest
        self.compressed = True

    async def _call_model(self, system: str, tool_schemas: list[dict]) -> AsyncIterator[StreamEvent]:
        delay = 2.0
        for attempt in range(RETRYABLE_ATTEMPTS):
            try:
                async for ev in self.provider.chat(
                    model=self.agent.model,
                    system=system,
                    messages=self.history,
                    tools=tool_schemas or None,
                    temperature=self.agent.temperature,
                    thinking=self.thinking,
                ):
                    yield ev
                return
            except Exception as e:
                status = getattr(e, "status_code", None)
                retryable = status in (429, 500, 502, 503, 529) or status is None
                if attempt == RETRYABLE_ATTEMPTS - 1 or not retryable:
                    raise
                log.warning("model call failed (attempt %d): %s; retrying in %.0fs", attempt + 1, e, delay)
                await asyncio.sleep(delay)
                delay *= 2

    def _done_payload(self, system: str) -> dict:
        u = self.usage
        prompt_total = u["input_tokens"] + u["cache_read_tokens"] + u["cache_write_tokens"]
        return {"type": "done", "usage": dict(u),
                "context_length": history_tokens(system, self.history),
                "context_limit": self.context_limit,
                "cache_hit": cache_hit_percent(u["cache_read_tokens"], prompt_total)}

    async def run(self, user_input: str | None) -> AsyncIterator[dict[str, Any]]:
        """Yield UI events: text_delta / tool_call / tool_result / done / error."""
        if user_input is not None:
            msg = Message(role="user", content=user_input)
            self.history.append(msg)
            await self._persist(msg)
        tool_schemas = registry.get_schemas(self.agent.tools)
        system = self._build_system()

        for _ in range(self.agent.max_iterations):
            await self._compress_if_needed(system)
            turn = None
            try:
                async for ev in self._call_model(system, tool_schemas):
                    if ev.type == "text_delta":
                        yield {"type": "text_delta", "text": ev.text}
                    elif ev.type == "reasoning_delta":
                        yield {"type": "reasoning_delta", "text": ev.text}
                    elif ev.type == "turn_done":
                        turn = ev.turn
            except Exception as e:
                log.exception("model call failed permanently")
                yield {"type": "error", "message": f"model call failed: {e}"}
                return
            assert turn is not None
            self.usage["input_tokens"] += turn.input_tokens
            self.usage["output_tokens"] += turn.output_tokens
            self.usage["cache_read_tokens"] += turn.cache_read_tokens
            self.usage["cache_write_tokens"] += turn.cache_write_tokens
            turn_total = turn.input_tokens + turn.cache_read_tokens + turn.cache_write_tokens
            yield {"type": "turn_usage", "input_tokens": turn.input_tokens,
                   "output_tokens": turn.output_tokens,
                   "cache_read_tokens": turn.cache_read_tokens,
                   "cache_write_tokens": turn.cache_write_tokens,
                   "cache_hit": cache_hit_percent(turn.cache_read_tokens, turn_total)}
            turn.tool_calls = expand_parallel_calls(turn.tool_calls)
            assistant_msg = turn.as_message()
            self.history.append(assistant_msg)
            await self._persist(assistant_msg)

            if not turn.tool_calls:
                yield self._done_payload(system)
                return

            # 先按原序广播全部调用事件，让 UI 立刻看到
            for tc in turn.tool_calls:
                yield {"type": "tool_call", "id": tc.id, "name": tc.name, "arguments": tc.arguments}
            # 并发执行（dispatch 自身吞异常返回文本，gather 不会抛）。并行主要利好只读类
            # 工具；写类工具（files/memory/skills）若同 turn 并发改同一资源需自行负责冲突，
            # 本期不强制串行（见设计文档"并发边界"）。结果回写严格有序，history 不会错乱。
            results = await asyncio.gather(
                *(registry.dispatch(tc.name, tc.arguments, self.ctx) for tc in turn.tool_calls)
            )
            # 严格按原序回写 history 并广播结果，保证可复现且每个 tool_call_id 都有回复
            for tc, result in zip(turn.tool_calls, results):
                # 兜底脱敏：抹掉工具输出里出现的已知密钥明文（Landlock 缺失时的第二层）
                result = redact_known_secrets(result)
                tool_msg = Message(role="tool", content=result, tool_call_id=tc.id)
                self.history.append(tool_msg)
                await self._persist(tool_msg)
                yield {"type": "tool_result", "id": tc.id, "name": tc.name,
                       "preview": result[:500] + ("..." if len(result) > 500 else "")}
            # system prompt may change after skill mutations
            system = self._build_system()

        payload = self._done_payload(system)
        payload["note"] = "max_iterations reached"
        yield payload

    def final_text(self) -> str:
        """Last assistant text produced in this run (for delegate / compat layer)."""
        for m in reversed(self._new_messages):
            if m.role == "assistant" and m.content:
                return m.content
        return ""
