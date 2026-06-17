"""Provider factory with per-name client caching (connection pool reuse)."""

from __future__ import annotations

from .base import Provider

_cache: dict[str, Provider] = {}


def get_provider(name: str) -> Provider:
    if name in _cache:
        return _cache[name]
    from .registry import get_registry
    return get_registry().client(name)


def reset_providers() -> None:
    """For tests / config reload."""
    _cache.clear()
