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

DEFAULT_COMPRESS_THRESHOLD = 0.5   # 对齐 hermes threshold_percent；超过 context*该值触发压缩
COMPRESS_THRESHOLD_MIN = 0.3
COMPRESS_THRESHOLD_MAX = 0.9


def exec_credentials():
    """Thin indirection over ``tools.sandbox.exec_credentials``.

    Imported lazily inside the body to avoid a ``config`` ↔ ``tools`` import
    cycle, yet kept at module scope so callers (and tests) resolve it as
    ``config.exec_credentials`` — giving a stable patch seam."""
    from .tools.sandbox import exec_credentials as _ec
    return _ec()


@dataclass
class ProviderConfig:
    name: str
    api_mode: str  # "openai" | "anthropic"
    api_key: str = ""
    base_url: str | None = None
    context_limit: int = 100_000  # tokens (estimated)
    source: str = "builtin"  # builtin (yaml) | db
    default_model: str = ""  # preferred model; pre-selected in the new-agent form


@dataclass
class Config:
    data_dir: Path
    secret: str
    admin_username: str
    admin_password: str | None
    # Local-dev only: when true, token-less requests auto-resolve to the
    # bootstrap admin (the Vue frontend has no login page). Never enable in
    # production — it disables authentication for anonymous callers.
    no_login: bool = False
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
    # 双存储字符预算（对齐 hermes）：每个 target 的总字符上限 + 单条上限。
    memory_total_chars: int = 2200  # MEMORY 段（agent 笔记，~800 token）
    user_total_chars: int = 1375    # USER 段（用户画像，~500 token）
    memory_max_chars: int = 500     # 单条上限
    # 后台自改进 review（③）：turn 后重放对话自动写记忆/改 skill
    self_improve_enabled: bool = True
    review_max_iterations: int = 6
    # WebSocket DNS-rebinding defence: when non-empty, the gateway only accepts
    # handshakes whose Host header matches one of these (host or host:port).
    # Empty => accept any host (development / tests).
    allowed_hosts: tuple[str, ...] = ()
    # Detach→reap grace window (seconds) for a WebSocket that disconnects
    # mid-turn: the turn keeps running; if no client reconnects in this window
    # the session state is reaped (DB history already persisted).
    ws_grace_seconds: float = 60.0
    # IM 网关（Telegram）：配了 bot token 才启用 in-process 轮询。
    telegram_bot_token: str = ""
    # 额外白名单（逗号分隔的 Telegram user id）；空=仅靠配对码控制访问。
    telegram_allowed_user_ids: tuple[int, ...] = ()
    # 定时任务（cron）：默认启用，60s 一轮 tick。
    cron_enabled: bool = True
    cron_tick_interval_seconds: float = 60.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sophagent.db"

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    def workspace_for(self, user_id: int) -> Path:
        ws = self.workspaces_dir / str(user_id)
        existed = ws.exists()
        ws.mkdir(parents=True, exist_ok=True)
        if not existed:
            creds = exec_credentials()
            if creds is not None:
                try:
                    os.chown(ws, creds[0], creds[1])
                except OSError:
                    pass  # 非 root 时无权改属主，本就无需降权，忽略
        return ws


def _load_providers(data_dir: Path) -> dict[str, ProviderConfig]:
    """Providers come from $SOPHAGENT_PROVIDERS (inline YAML/JSON) or
    providers.yaml in the data dir. Format:

        providers:
          my-openai:
            api_mode: openai
            base_url: https://api.example.com/v1
            api_key: sk-...
            context_limit: 128000
    """
    raw = os.environ.get("SOPHAGENT_PROVIDERS", "")
    if not raw:
        path = Path(os.environ.get("SOPHAGENT_PROVIDERS_FILE", data_dir / "providers.yaml"))
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
            default_model=str(spec.get("model") or ""),
        )
    return providers


def _load_secret(data_dir: Path) -> str:
    secret = os.environ.get("SOPHAGENT_SECRET", "")
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
    data_dir = Path(os.environ.get("SOPHAGENT_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        data_dir=data_dir,
        secret=_load_secret(data_dir),
        admin_username=os.environ.get("ADMIN_USERNAME", "admin"),
        admin_password=os.environ.get("ADMIN_PASSWORD") or None,
        no_login=os.environ.get("SOPHAGENT_NO_LOGIN", "").lower()
        in {"1", "true", "yes", "on"},
        providers=_load_providers(data_dir),
        token_ttl_hours=int(os.environ.get("SOPHAGENT_TOKEN_TTL_HOURS", "24")),
        max_concurrent_turns=int(os.environ.get("SOPHAGENT_MAX_CONCURRENT_TURNS", "32")),
        max_upload_bytes=int(os.environ.get("SOPHAGENT_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))),
        delegate_concurrency=int(os.environ.get("SOPHAGENT_DELEGATE_CONCURRENCY", "4")),
        allowed_hosts=tuple(
            h for h in
            (os.environ.get("SOPHAGENT_ALLOWED_HOSTS") or "").split(",") if h
        ),
        ws_grace_seconds=float(os.environ.get("SOPHAGENT_WS_GRACE_SECONDS", "60")),
        telegram_bot_token=os.environ.get("SOPHAGENT_TELEGRAM_BOT_TOKEN", ""),
        telegram_allowed_user_ids=tuple(
            int(u) for u in
            (os.environ.get("SOPHAGENT_TELEGRAM_ALLOWED_USER_IDS") or "").split(",") if u
        ),
        self_improve_enabled=os.environ.get("SOPHAGENT_SELF_IMPROVE", "true").lower()
        not in {"0", "false", "no", "off"},
        cron_enabled=os.environ.get("SOPHAGENT_CRON_ENABLED", "true").lower()
        not in {"0", "false", "no", "off"},
        cron_tick_interval_seconds=float(os.environ.get("SOPHAGENT_CRON_TICK_SECONDS", "60")),
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


async def effective_max_upload_bytes(db) -> int:
    """Runtime-effective single-file size limit: an admin-set DB override
    (key ``max_upload_bytes``) wins; otherwise fall back to the env/default
    value baked into Config. A malformed DB value is ignored."""
    raw = await db.get_setting("max_upload_bytes")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    return get_config().max_upload_bytes


async def effective_write_approval(db) -> bool:
    """Runtime-effective memory write-approval gate: an admin-set DB override
    (key ``write_approval``) wins; default off (write freely, like hermes)."""
    raw = await db.get_setting("write_approval")
    return str(raw).lower() in {"1", "true", "yes", "on"} if raw is not None else False


async def effective_compress_threshold(db) -> float:
    """Runtime-effective auto-compaction trigger ratio: an admin-set DB override
    (key ``compress_threshold``) wins, clamped into [COMPRESS_THRESHOLD_MIN,
    COMPRESS_THRESHOLD_MAX]. A malformed value falls back to the default."""
    raw = await db.get_setting("compress_threshold")
    if raw is not None:
        try:
            val = float(raw)
            return max(COMPRESS_THRESHOLD_MIN, min(val, COMPRESS_THRESHOLD_MAX))
        except (ValueError, TypeError):
            pass
    return DEFAULT_COMPRESS_THRESHOLD


async def effective_telegram_token(db) -> str:
    """Runtime-effective Telegram bot token: a DB setting (key
    ``telegram_bot_token``) wins; otherwise fall back to the env/default
    value baked into Config. Empty string => IM gateway off."""
    raw = await db.get_setting("telegram_bot_token")
    if raw:
        return raw
    return get_config().telegram_bot_token
