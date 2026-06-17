"""Adapter for the Anthropic Messages API.

All format differences are contained here:
- system is a top-level parameter
- assistant tool calls become tool_use content blocks
- role=tool messages become tool_result blocks inside a user message
  (consecutive tool messages merge into one user message)
- tool schemas use input_schema instead of parameters
- max_tokens is required (default 8192)
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from ..config import ProviderConfig
from ..models import AssistantTurn, Message, StreamEvent, ToolCall

DEFAULT_MAX_TOKENS = 8192


def messages_to_anthropic(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls or []:
                blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        elif m.role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        else:  # user (system handled at top level)
            out.append({"role": "user", "content": m.content})
    return out


def tools_to_anthropic(tools: list[dict]) -> list[dict[str, Any]]:
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("parameters") or {"type": "object", "properties": {}},
        }
        for t in tools
    ]


class AnthropicProvider:
    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        kwargs: dict[str, Any] = {"api_key": cfg.api_key or "missing"}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self.client = AsyncAnthropic(**kwargs)

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
            "messages": messages_to_anthropic(messages),
            "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools_to_anthropic(tools)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if thinking == "thinking":
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": 4096}
            kwargs.pop("temperature", None)  # extended thinking 要求不自定义 temperature

        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        # block index -> {"id":, "name":, "json": str}
        pending: dict[int, dict[str, str]] = {}
        stop_reason = ""
        input_tokens = output_tokens = 0

        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                etype = event.type
                if etype == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        pending[event.index] = {"id": block.id, "name": block.name, "json": ""}
                elif etype == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        content_parts.append(delta.text)
                        yield StreamEvent("text_delta", text=delta.text)
                    elif delta.type == "input_json_delta":
                        slot = pending.get(event.index)
                        if slot is not None:
                            slot["json"] += delta.partial_json
                elif etype == "content_block_stop":
                    slot = pending.pop(event.index, None)
                    if slot is not None:
                        try:
                            args = json.loads(slot["json"]) if slot["json"] else {}
                        except json.JSONDecodeError:
                            args = {"_raw": slot["json"]}
                        tool_calls.append(ToolCall(id=slot["id"], name=slot["name"], arguments=args))
                elif etype == "message_start":
                    input_tokens = event.message.usage.input_tokens or 0
                elif etype == "message_delta":
                    if event.delta.stop_reason:
                        stop_reason = event.delta.stop_reason
                    if getattr(event, "usage", None):
                        output_tokens = event.usage.output_tokens or 0

        yield StreamEvent(
            "turn_done",
            turn=AssistantTurn(
                content="".join(content_parts),
                tool_calls=tool_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                stop_reason=stop_reason,
            ),
        )
