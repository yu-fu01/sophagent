"""DingTalk group chat gating helpers."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


def parse_id_list(raw: str | None) -> tuple[str, ...]:
    text = (raw or "").strip()
    if not text:
        return ()
    return tuple(part.strip() for part in text.split(",") if part.strip())


def parse_mention_patterns(raw: str | None) -> tuple[str, ...]:
    text = (raw or "").strip()
    if not text:
        return ()
    try:
        loaded = json.loads(text)
        if isinstance(loaded, list):
            return tuple(str(p).strip() for p in loaded if str(p).strip())
    except json.JSONDecodeError:
        pass
    parts = [p.strip() for p in text.splitlines() if p.strip()]
    if not parts:
        parts = [p.strip() for p in text.split(",") if p.strip()]
    return tuple(parts)


def compile_mention_patterns(patterns: tuple[str, ...]) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        if not pattern:
            continue
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            log.warning("dingtalk: invalid mention pattern %r: %s", pattern, exc)
    return compiled


def text_matches_mention_patterns(text: str, patterns: list[re.Pattern[str]]) -> bool:
    if not text or not patterns:
        return False
    return any(p.search(text) for p in patterns)


def should_process_group(
    *,
    is_group: bool,
    chat_id: str,
    text: str,
    message: Any,
    require_mention: bool,
    allowed_chat_ids: set[str],
    free_response_chats: set[str],
    mention_patterns: list[re.Pattern[str]],
    is_slash_command: bool = False,
) -> bool:
    """Group trigger rules aligned with hermes DingTalk adapter."""
    if not is_group:
        return True
    if is_slash_command:
        return True
    if allowed_chat_ids and chat_id and chat_id not in allowed_chat_ids:
        return False
    if chat_id and chat_id in free_response_chats:
        return True
    if not require_mention:
        return True
    if getattr(message, "is_in_at_list", False):
        return True
    return text_matches_mention_patterns(text, mention_patterns)
