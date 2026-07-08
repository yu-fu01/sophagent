"""Weixin outbound text splitting helpers."""

from __future__ import annotations

import re
import textwrap

WEIXIN_COPY_LINE_WIDTH = 120
_FENCE_RE = re.compile(r"^```([^\n`]*)\s*$")
_TABLE_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")


def truncate_message(text: str, max_length: int) -> list[str]:
    if len(text) <= max_length:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + max_length])
        start += max_length
    return chunks


def _split_markdown_blocks(content: str) -> list[str]:
    if not content:
        return []
    blocks: list[str] = []
    lines = content.splitlines()
    current: list[str] = []
    in_code = False
    for raw_line in lines:
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            if not in_code and current:
                blocks.append("\n".join(current).strip())
                current = []
            current.append(line)
            in_code = not in_code
            if not in_code:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        if in_code:
            current.append(line)
            continue
        if not line.strip():
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


def _pack_blocks(content: str, max_length: int) -> list[str]:
    if len(content) <= max_length:
        return [content]
    packed: list[str] = []
    current = ""
    for block in _split_markdown_blocks(content):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            packed.append(current)
            current = ""
        if len(block) <= max_length:
            current = block
        else:
            packed.extend(truncate_message(block, max_length))
    if current:
        packed.append(current)
    return packed


def _looks_chatty_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 48:
        return False
    if line.startswith((" ", "\t")):
        return False
    if stripped.startswith((">", "-", "*", "【", "#", "|")):
        return False
    if _TABLE_RULE_RE.match(stripped):
        return False
    return True


def _should_split_short_chat_block(content: str) -> bool:
    lines = [line for line in content.splitlines() if line.strip()]
    if not 2 <= len(lines) <= 6:
        return False
    return all(_looks_chatty_line(line) for line in lines)


def _split_delivery_units(content: str) -> list[str]:
    units: list[str] = []
    for block in _split_markdown_blocks(content):
        if _FENCE_RE.match(block.splitlines()[0].strip()):
            units.append(block)
            continue
        current: list[str] = []
        for raw_line in block.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                if current:
                    units.append("\n".join(current).strip())
                    current = []
                continue
            if current and raw_line.startswith((" ", "\t")):
                current.append(line)
                continue
            if current:
                units.append("\n".join(current).strip())
            current = [line]
        if current:
            units.append("\n".join(current).strip())
    return [u for u in units if u]


def wrap_copy_friendly_lines(content: str) -> str:
    if not content:
        return content
    wrapped: list[str] = []
    in_code = False
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if _FENCE_RE.match(stripped):
            in_code = not in_code
            wrapped.append(line)
            continue
        if in_code or len(line) <= WEIXIN_COPY_LINE_WIDTH or not stripped or stripped.startswith("|"):
            wrapped.append(line)
            continue
        wrapped.extend(textwrap.wrap(
            line, width=WEIXIN_COPY_LINE_WIDTH, break_long_words=False,
            break_on_hyphens=False, replace_whitespace=False, drop_whitespace=True,
        ) or [line])
    return "\n".join(wrapped).strip()


def split_text_for_delivery(
    content: str,
    max_length: int,
    *,
    split_multiline: bool = False,
) -> list[str]:
    content = wrap_copy_friendly_lines(content)
    if not content:
        return []
    if split_multiline:
        chunks: list[str] = []
        for unit in _split_delivery_units(content):
            if len(unit) <= max_length:
                chunks.append(unit)
            else:
                chunks.extend(_pack_blocks(unit, max_length))
        return chunks or [content]
    if len(content) <= max_length:
        if _should_split_short_chat_block(content):
            return [u for u in _split_delivery_units(content) if u]
        return [content]
    return _pack_blocks(content, max_length) or [content]
