"""Unified provider registry: built-in (yaml, read-only) + DB-managed, merged
with DB taking precedence. Module-level singleton, mirroring get_config()."""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from ..config import ProviderConfig
from ..crypto import decrypt
from .anthropic_provider import AnthropicProvider
from .base import Provider
from .openai_provider import OpenAIProvider

log = logging.getLogger(__name__)


@dataclass
class ResolvedProvider:
    name: str
    api_mode: str
    api_key: str
    base_url: str | None
    context_limit: int
    source: str
    default_model: str = ""

    def fingerprint(self) -> str:
        raw = f"{self.api_mode}|{self.base_url}|{self.api_key}|{self.context_limit}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ProviderRegistry:
    def __init__(self, db: Any, builtin_providers: dict[str, ProviderConfig], secret: str):
        self.db = db
        self.builtin = builtin_providers
        self.secret = secret
        self._resolved: dict[str, ResolvedProvider] = {}
        self._clients: dict[tuple[str, str], Provider] = {}
        self._models_cache: dict[str, tuple[float, list[str]]] = {}

    async def refresh(self) -> None:
        merged: dict[str, ResolvedProvider] = {}
        for name, c in self.builtin.items():
            merged[name] = ResolvedProvider(name, c.api_mode, c.api_key, c.base_url,
                                            c.context_limit, "builtin", c.default_model)
        for row in await self.db.list_providers():
            try:
                key = decrypt(row["api_key_enc"], self.secret) if row["api_key_enc"] else ""
            except Exception:
                log.warning("provider %r: api key decrypt failed (secret rotated?)", row["name"])
                key = ""
            merged[row["name"]] = ResolvedProvider(
                row["name"], row["api_mode"], key, row["base_url"],
                row["context_limit"], "db")
        self._resolved = merged
        active = {(n, r.fingerprint()) for n, r in self._resolved.items()}
        self._clients = {k: v for k, v in self._clients.items() if k in active}

    def names(self) -> list[str]:
        return sorted(self._resolved)

    def resolve(self, name: str) -> ResolvedProvider:
        if name not in self._resolved:
            raise KeyError(f"unknown provider: {name!r}")
        return self._resolved[name]

    def client(self, name: str) -> Provider:
        r = self.resolve(name)
        ckey = (name, r.fingerprint())
        if ckey not in self._clients:
            cfg = ProviderConfig(name=r.name, api_mode=r.api_mode, api_key=r.api_key,
                                 base_url=r.base_url, context_limit=r.context_limit)
            self._clients[ckey] = (AnthropicProvider(cfg) if r.api_mode == "anthropic"
                                   else OpenAIProvider(cfg))
        return self._clients[ckey]

    async def list_models(self, name: str, ttl: float = 300.0) -> list[str]:
        hit = self._models_cache.get(name)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        models = await _fetch_models(self.resolve(name))
        self._models_cache[name] = (time.monotonic(), models)
        return models


async def _fetch_models(r: ResolvedProvider) -> list[str]:
    if r.api_mode == "anthropic":
        from anthropic import AsyncAnthropic
        kw: dict = {"api_key": r.api_key or "missing"}
        if r.base_url:
            kw["base_url"] = r.base_url
        page = await AsyncAnthropic(**kw).models.list()
    else:
        from openai import AsyncOpenAI
        page = await AsyncOpenAI(api_key=r.api_key or "missing", base_url=r.base_url).models.list()
    return [m.id for m in getattr(page, "data", []) or []]


_registry: ProviderRegistry | None = None


def init_registry(db: Any, config) -> ProviderRegistry:
    global _registry
    _registry = ProviderRegistry(db, config.providers, config.secret)
    return _registry


def get_registry() -> ProviderRegistry:
    if _registry is None:
        raise RuntimeError("provider registry not initialized")
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None
