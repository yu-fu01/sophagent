"""Provider factory with per-name client caching (connection pool reuse)."""

from __future__ import annotations

from ..config import get_config
from .anthropic_provider import AnthropicProvider
from .base import Provider
from .openai_provider import OpenAIProvider

_cache: dict[str, Provider] = {}


def get_provider(name: str) -> Provider:
    if name in _cache:
        return _cache[name]
    cfg = get_config().providers.get(name)
    if cfg is None:
        raise KeyError(f"unknown provider: {name!r} (configure it in providers.yaml)")
    provider: Provider
    if cfg.api_mode == "anthropic":
        provider = AnthropicProvider(cfg)
    else:
        provider = OpenAIProvider(cfg)
    _cache[name] = provider
    return provider


def reset_providers() -> None:
    """For tests / config reload."""
    _cache.clear()
