"""Safe path resolution for DingTalk outbound media."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_outbound_path(raw: str, *, allowed_roots: list[Path]) -> Path | None:
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        return None
    if text.startswith("file://"):
        text = text[7:]
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = Path(os.path.abspath(text))
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return None
    for root in allowed_roots:
        try:
            root_resolved = root.resolve(strict=False)
            resolved.relative_to(root_resolved)
            if resolved.is_file():
                return resolved
        except ValueError:
            continue
    return None
