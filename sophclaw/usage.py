"""Prompt-cache hit percent: cached reads over the full prompt total
(input + cache reads + cache writes). Kept server-side so all displays agree."""
from __future__ import annotations


def cache_hit_percent(cache_read: int, prompt_total: int):
    cache_read = int(cache_read or 0)
    prompt_total = int(prompt_total or 0)
    if cache_read <= 0 or prompt_total <= 0:
        return None
    return min(100, round(cache_read / prompt_total * 100))
