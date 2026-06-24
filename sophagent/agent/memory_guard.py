"""Memory safety scan: reject memory entries that could subvert the system
prompt they get injected into.

Memory is rendered verbatim into the system prompt as a frozen snapshot, so a
poisoned entry (prompt injection, an exfiltration/backdoor instruction, or
hidden Unicode that changes how text is read) would silently steer every future
turn. Both the foreground ``memory`` tool and the background self-improvement
review write through here, so one gate covers both paths.

Unlike ``redact`` (which transforms credential *values* in transient text),
this *blocks* the whole write and tells the caller why.
"""

from __future__ import annotations

import re

# Invisible / direction-control codepoints that have no business in a memory
# note: zero-width chars and bidi overrides can hide or reorder text.
_INVISIBLE = (
    "​‌‍⁠﻿"          # zero-width space/joiners, BOM, word-joiner
    "‪‫‬‭‮"          # bidi embeddings/overrides
    "⁦⁧⁨⁩"                # bidi isolates
)
_INVISIBLE_RE = re.compile(f"[{_INVISIBLE}]")

# Prompt-injection phrasing aimed at the next turn's system prompt.
_INJECTION_RE = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+|the\s+)?(previous|above|prior|earlier|"
    r"your)\s+(instructions?|prompts?|rules?|directives?)"
    r"|you\s+are\s+now\s+\w"
    r"|new\s+(system\s+)?(instructions?|prompt)\s*:",
    re.IGNORECASE,
)

# Exfiltration / persistence backdoors that should never live in memory.
_BACKDOOR_RE = re.compile(
    r"authorized_keys"
    r"|-----BEGIN[A-Z ]*PRIVATE KEY-----"
    r"|curl\s+[^\n|]*\|\s*(sh|bash)"
    r"|wget\s+[^\n|]*\|\s*(sh|bash)",
    re.IGNORECASE,
)


def scan_memory(content: str) -> str | None:
    """Return a rejection reason if ``content`` is unsafe to store as memory,
    or ``None`` when it's clean."""
    if not content:
        return None
    if _INVISIBLE_RE.search(content):
        return "contains invisible or direction-control Unicode characters"
    if _INJECTION_RE.search(content):
        return "looks like a prompt-injection instruction"
    if _BACKDOOR_RE.search(content):
        return "looks like an exfiltration or backdoor instruction"
    return None
