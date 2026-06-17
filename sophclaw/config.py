"""Runtime configuration.

Everything comes from environment variables plus an optional providers YAML
file. API keys never touch the database: agents reference providers by name.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ProviderConfig:
    name: str
    api_mode: str  # "openai" | "anthropic"
    api_key: str = ""
    base_url: str | None = None
    context_limit: int = 100_000  # tokens (estimated)
    source: str = "builtin"  # builtin (yaml) | db


@dataclass
class Config:
    data_dir: Path
    secret: str
    admin_username: str
    admin_password: str | None
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    token_ttl_hours: int = 24
    max_concurrent_turns: int = 32
    delegate_concurrency: int = 4
    delegate_timeout: float = 300.0
    tool_timeout: float = 60.0
    tool_output_limit: int = 30_000  # chars per tool result
    max_upload_bytes: int = 10 * 1024 * 1024  # 上传/下载单文件上限 (10MB)
    max_skill_md_bytes: int = 64 * 1024
    max_skill_file_bytes: int = 256 * 1024
    memory_max_items: int = 50
    memory_max_chars: int = 500

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sophclaw.db"

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    def workspace_for(self, user_id: int) -> Path:
        ws = self.workspaces_dir / str(user_id)
        ws.mkdir(parents=True, exist_ok=True)
        return ws


def _load_providers(data_dir: Path) -> dict[str, ProviderConfig]:
    """Providers come from $SOPHCLAW_PROVIDERS (inline YAML/JSON) or
    providers.yaml in the data dir. Format:

        providers:
          my-openai:
            api_mode: openai
            base_url: https://api.example.com/v1
            api_key: sk-...
            context_limit: 128000
    """
    raw = os.environ.get("SOPHCLAW_PROVIDERS", "")
    if not raw:
        path = Path(os.environ.get("SOPHCLAW_PROVIDERS_FILE", data_dir / "providers.yaml"))
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
    if not raw:
        return {}
    doc = yaml.safe_load(raw) or {}
    entries = doc.get("providers", doc)
    providers: dict[str, ProviderConfig] = {}
    for name, spec in (entries or {}).items():
        if not isinstance(spec, dict):
            continue
        mode = str(spec.get("api_mode", "openai")).lower()
        if mode not in ("openai", "anthropic"):
            raise ValueError(f"provider {name!r}: api_mode must be 'openai' or 'anthropic'")
        api_key = str(spec.get("api_key") or "")
        if api_key.startswith("$"):  # indirection: $ENV_VAR_NAME
            api_key = os.environ.get(api_key[1:], "")
        providers[name] = ProviderConfig(
            name=name,
            api_mode=mode,
            api_key=api_key,
            base_url=spec.get("base_url") or None,
            context_limit=int(spec.get("context_limit", 100_000)),
        )
    return providers


def _load_secret(data_dir: Path) -> str:
    secret = os.environ.get("SOPHCLAW_SECRET", "")
    if secret:
        return secret
    secret_file = data_dir / ".secret"
    if secret_file.is_file():
        return secret_file.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(secret, encoding="utf-8")
    secret_file.chmod(0o600)
    return secret


def load_config() -> Config:
    data_dir = Path(os.environ.get("SOPHCLAW_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        data_dir=data_dir,
        secret=_load_secret(data_dir),
        admin_username=os.environ.get("ADMIN_USERNAME", "admin"),
        admin_password=os.environ.get("ADMIN_PASSWORD") or None,
        providers=_load_providers(data_dir),
        token_ttl_hours=int(os.environ.get("SOPHCLAW_TOKEN_TTL_HOURS", "24")),
        max_concurrent_turns=int(os.environ.get("SOPHCLAW_MAX_CONCURRENT_TURNS", "32")),
        max_upload_bytes=int(os.environ.get("SOPHCLAW_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))),
        delegate_concurrency=int(os.environ.get("SOPHCLAW_DELEGATE_CONCURRENCY", "4")),
    )
    cfg.skills_dir.mkdir(parents=True, exist_ok=True)
    cfg.workspaces_dir.mkdir(parents=True, exist_ok=True)
    return cfg


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """For tests."""
    global _config
    _config = None
