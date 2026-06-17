"""Provider protocol: one async streaming chat method.

The internal message/tool-schema format is OpenAI chat.completions shaped;
each adapter owns the full conversion to its native wire format so the rest
of the system never sees provider differences.
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from ..models import Message, StreamEvent


class Provider(Protocol):
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
        """Yield text_delta events followed by exactly one turn_done event.

        thinking: controls extended reasoning mode.
          "thinking" — enable native chain-of-thought (maps to reasoning_effort="high"
                        on OpenAI-compat, or {"type":"enabled"} on Anthropic).
          "fast"     — reserved for future fast-thinking mode; currently treated as None.
          None       — default behaviour, no extended reasoning.
        """
        ...
