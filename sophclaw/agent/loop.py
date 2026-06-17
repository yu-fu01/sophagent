"""AgentRunner: the core model<->tools loop with two-layer context compression."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from ..config import get_config
from ..models import AgentDef, Message, StreamEvent, ToolCall
from ..providers import get_provider
from ..tools import registry
from ..usage import cache_hit_percent

log = logging.getLogger(__name__)

RETRYABLE_ATTEMPTS = 3
KEEP_RECENT_TOOL_MSGS = 8  # tool messages within this tail are never truncated
TOOL_TRUNCATE_NOTE = "[older tool output truncated: {n} chars]"


def _resolve_context_limit(provider_name: str) -> int:
    """Context limit from the provider registry; fall back to static config when
    the registry isn't initialized (e.g. unit tests constructing AgentRunner directly)."""
    from ..providers.registry import get_registry
    try:
        return get_registry().resolve(provider_name).context_limit
    except (RuntimeError, KeyError):
        return get_config().providers[provider_name].context_limit


def estimate_tokens(text: str) -> int:
    """Conservative heuristic for mixed CJK/latin text; avoids tiktoken."""
    return len(text) // 3 + 1


def history_tokens(system: str, messages: list[Message]) -> int:
    total = estimate_tokens(system)
    for m in messages:
        total += estimate_tokens(m.content) + 8
        for tc in m.tool_calls or []:
            total += estimate_tokens(json.dumps(tc.arguments, ensure_ascii=False)) + 8
    return total


def truncate_old_tool_messages(messages: list[Message]) -> list[Message]:
    """Layer 1 (no LLM): blank out tool outputs older than the recent tail."""
    tool_indexes = [i for i, m in enumerate(messages) if m.role == "tool"]
    old = set(tool_indexes[:-KEEP_RECENT_TOOL_MSGS]) if len(tool_indexes) > KEEP_RECENT_TOOL_MSGS else set()
    out = []
    for i, m in enumerate(messages):
        if i in old and len(m.content) > 200:
            m = Message(role="tool", content=TOOL_TRUNCATE_NOTE.format(n=len(m.content)),
                        tool_call_id=m.tool_call_id)
        out.append(m)
    return out


class AgentRunner:
    """Runs one conversation turn. Stateless between turns: history is loaded
    from the DB, mutated in memory, and persisted via the on_persist callback."""

    def __init__(
        self,
        agent: AgentDef,
        ctx: registry.ToolContext,
        history: list[Message],
        on_persist: Optional[Callable[[list[Message]], Awaitable[None]]] = None,
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
        self.compressed = False  # set when history was rewritten; caller may compact the DB
        self.thinking: str | None = None

    async def _persist(self, msg: Message) -> None:
        self._new_messages.append(msg)
        if self._on_persist:
            await self._on_persist([msg])

    def _build_system(self) -> str:
        from .prompt import build_system_prompt

        skill_index = None
        if self.ctx.skill_store is not None:
            skill_index = self.ctx.skill_store.index(self.agent.skills)
        memories = self.ctx.services.get("memories")
        return build_system_prompt(self.agent, skill_index, memories, self.ctx.workspace)

    async def _compress_if_needed(self, system: str) -> None:
        budget = int(self.context_limit * 0.8)
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
        """Layer 2: replace the oldest half of the history with an LLM summary."""
        if len(self.history) < 4:
            return
        cut = len(self.history) // 2
        # never split an assistant tool_call from its tool results
        while cut < len(self.history) and self.history[cut].role == "tool":
            cut += 1
        old, rest = self.history[:cut], self.history[cut:]
        transcript = "\n".join(f"[{m.role}] {m.content[:1000]}" for m in old if m.content)
        summary_parts: list[str] = []
        try:
            async for ev in self.provider.chat(
                model=self.agent.model,
                system="Summarize this conversation excerpt into a compact brief that preserves "
                       "goals, decisions, key facts, file names and unresolved items. Plain text.",
                messages=[Message(role="user", content=transcript[:60_000])],
                max_tokens=1500,
            ):
                if ev.type == "turn_done" and ev.turn:
                    summary_parts.append(ev.turn.content)
        except Exception:
            log.exception("summary compression failed; falling back to hard drop")
        summary = "".join(summary_parts).strip() or "(summary unavailable; older messages dropped)"
        self.history = [Message(role="user", content=f"[Earlier conversation summary]\n{summary}")] + rest
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
            assistant_msg = turn.as_message()
            self.history.append(assistant_msg)
            await self._persist(assistant_msg)

            if not turn.tool_calls:
                yield self._done_payload(system)
                return

            for tc in turn.tool_calls:
                yield {"type": "tool_call", "id": tc.id, "name": tc.name, "arguments": tc.arguments}
                result = await registry.dispatch(tc.name, tc.arguments, self.ctx)
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
