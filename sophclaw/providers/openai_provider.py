"""Adapter for OpenAI and any OpenAI-compatible third-party API."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

from openai import AsyncOpenAI

from ..config import ProviderConfig
from ..models import AssistantTurn, Message, StreamEvent, ToolCall


def messages_to_openai(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        if m.role == "assistant":
            d: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.reasoning:
                d["reasoning_content"] = m.reasoning
            if m.tool_calls:
                d["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                    }
                    for tc in m.tool_calls
                ]
            out.append(d)
        elif m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def _parse_arguments(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"_raw": raw}


class OpenAIProvider:
    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.client = AsyncOpenAI(api_key=cfg.api_key or "missing", base_url=cfg.base_url)

    async def chat(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages_to_openai(system, messages),
            "stream": True,
        }
        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if thinking == "thinking":
            kwargs["reasoning_effort"] = "high"
        try:
            stream = await self.client.chat.completions.create(
                **kwargs, stream_options={"include_usage": True}
            )
        except TypeError:
            # some third-party endpoints reject stream_options / reasoning_effort;
            # retry without them — note token usage will then be unavailable
            log.warning("endpoint rejected stream_options/reasoning_effort; "
                        "retrying without them (token counts unavailable)")
            kwargs.pop("reasoning_effort", None)
            stream = await self.client.chat.completions.create(**kwargs)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        # index -> {"id":, "name":, "arguments": str}
        pending_calls: dict[int, dict[str, str]] = {}
        stop_reason = ""
        input_tokens = output_tokens = 0
        cache_read_tokens = 0

        async for chunk in stream:
            if getattr(chunk, "usage", None):
                input_tokens = chunk.usage.prompt_tokens or 0
                output_tokens = chunk.usage.completion_tokens or 0
                details = getattr(chunk.usage, "prompt_tokens_details", None)
                if details is not None:
                    cache_read_tokens = getattr(details, "cached_tokens", 0) or 0
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                stop_reason = choice.finish_reason
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                content_parts.append(delta.content)
                yield StreamEvent("text_delta", text=delta.content)
            # thinking models (DeepSeek et al.) stream reasoning separately;
            # keep it out of the visible text but surface it as its own event
            # (for live display) and echo it back next call
            reasoning_delta = getattr(delta, "reasoning_content", None)
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)
                yield StreamEvent("reasoning_delta", text=reasoning_delta)
            for tc in delta.tool_calls or []:
                slot = pending_calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function.arguments:
                        slot["arguments"] += tc.function.arguments

        tool_calls = [
            ToolCall(id=slot["id"] or f"call_{i}", name=slot["name"], arguments=_parse_arguments(slot["arguments"]))
            for i, slot in sorted(pending_calls.items())
            if slot["name"]
        ]
        yield StreamEvent(
            "turn_done",
            turn=AssistantTurn(
                content="".join(content_parts),
                tool_calls=tool_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                stop_reason=stop_reason,
                reasoning="".join(reasoning_parts),
            ),
        )
