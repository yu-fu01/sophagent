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
    # IM 网关（QQ Bot）：配了 app_id + client_secret 才启用。
    qq_app_id: str = ""
    qq_client_secret: str = ""
    # 额外白名单（逗号分隔的 QQ openid）；空=仅靠配对码控制访问。
    qq_allowed_user_ids: tuple[str, ...] = ()
    # IM 网关（Feishu/Lark）：配了 app_id + app_secret 才启用。
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_domain: str = "feishu"
    feishu_connection_mode: str = "websocket"
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_require_mention: bool = True
    feishu_allowed_user_ids: tuple[str, ...] = ()
    # IM 网关（Weixin 个人微信）：配了 account_id + token 才启用。
    weixin_account_id: str = ""
    weixin_token: str = ""
    weixin_base_url: str = "https://ilinkai.weixin.qq.com"
    weixin_allowed_user_ids: tuple[str, ...] = ()
    weixin_dm_policy: str = "pairing"
    weixin_cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"
    weixin_split_multiline: bool = False
    # IM 网关（钉钉 DingTalk）：Stream 入站 + AI Card 流式出站。
    dingtalk_client_id: str = ""
    dingtalk_client_secret: str = ""
    dingtalk_card_template_id: str = ""
    dingtalk_robot_code: str = ""
    dingtalk_allowed_user_ids: tuple[str, ...] = ()
    dingtalk_dm_policy: str = "pairing"
    dingtalk_require_mention: bool = True
    dingtalk_allowed_chat_ids: tuple[str, ...] = ()
    dingtalk_free_response_chats: tuple[str, ...] = ()
    dingtalk_mention_patterns: str = ""
    dingtalk_webhook_url: str = ""
    dingtalk_home_channel: str = ""
    dingtalk_reply_emotion: bool = True
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
        ws.mkdir(parents=True, exist_ok=True)
        # 幂等：每次都尝试 chown 给 sandbox，确保存量(升级前 root 持有)workspace
        # 也归属正确，降权后的 exec 子进程才能读写自己的 workspace。
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
        qq_app_id=os.environ.get("SOPHAGENT_QQ_APP_ID", ""),
        qq_client_secret=os.environ.get("SOPHAGENT_QQ_CLIENT_SECRET", ""),
        qq_allowed_user_ids=tuple(
            u.strip() for u in
            (os.environ.get("SOPHAGENT_QQ_ALLOWED_USER_IDS") or "").split(",") if u.strip()
        ),
        feishu_app_id=os.environ.get("SOPHAGENT_FEISHU_APP_ID", ""),
        feishu_app_secret=os.environ.get("SOPHAGENT_FEISHU_APP_SECRET", ""),
        feishu_domain=os.environ.get("SOPHAGENT_FEISHU_DOMAIN", "feishu"),
        feishu_connection_mode=os.environ.get("SOPHAGENT_FEISHU_CONNECTION_MODE", "websocket"),
        feishu_verification_token=os.environ.get("SOPHAGENT_FEISHU_VERIFICATION_TOKEN", ""),
        feishu_encrypt_key=os.environ.get("SOPHAGENT_FEISHU_ENCRYPT_KEY", ""),
        feishu_require_mention=os.environ.get("SOPHAGENT_FEISHU_REQUIRE_MENTION", "true").lower()
        not in {"0", "false", "no", "off"},
        feishu_allowed_user_ids=tuple(
            u.strip() for u in
            (os.environ.get("SOPHAGENT_FEISHU_ALLOWED_USER_IDS") or "").split(",") if u.strip()
        ),
        weixin_account_id=os.environ.get("SOPHAGENT_WEIXIN_ACCOUNT_ID", ""),
        weixin_token=os.environ.get("SOPHAGENT_WEIXIN_TOKEN", ""),
        weixin_base_url=os.environ.get("SOPHAGENT_WEIXIN_BASE_URL", "https://ilinkai.weixin.qq.com"),
        weixin_allowed_user_ids=tuple(
            u.strip() for u in
            (os.environ.get("SOPHAGENT_WEIXIN_ALLOWED_USER_IDS") or "").split(",") if u.strip()
        ),
        weixin_dm_policy=os.environ.get("SOPHAGENT_WEIXIN_DM_POLICY", "pairing").strip().lower() or "pairing",
        weixin_cdn_base_url=os.environ.get("SOPHAGENT_WEIXIN_CDN_BASE_URL", "https://novac2c.cdn.weixin.qq.com/c2c"),
        weixin_split_multiline=os.environ.get("SOPHAGENT_WEIXIN_SPLIT_MULTILINE", "").lower() in {"1", "true", "yes", "on"},
        dingtalk_client_id=os.environ.get("SOPHAGENT_DINGTALK_CLIENT_ID", ""),
        dingtalk_client_secret=os.environ.get("SOPHAGENT_DINGTALK_CLIENT_SECRET", ""),
        dingtalk_card_template_id=os.environ.get("SOPHAGENT_DINGTALK_CARD_TEMPLATE_ID", ""),
        dingtalk_robot_code=os.environ.get("SOPHAGENT_DINGTALK_ROBOT_CODE", ""),
        dingtalk_allowed_user_ids=tuple(
            u.strip() for u in
            (os.environ.get("SOPHAGENT_DINGTALK_ALLOWED_USER_IDS") or "").split(",") if u.strip()
        ),
        dingtalk_dm_policy=os.environ.get("SOPHAGENT_DINGTALK_DM_POLICY", "pairing").strip().lower() or "pairing",
        dingtalk_require_mention=os.environ.get("SOPHAGENT_DINGTALK_REQUIRE_MENTION", "true").lower()
        not in {"0", "false", "no", "off"},
        dingtalk_allowed_chat_ids=tuple(
            c.strip() for c in
            (os.environ.get("SOPHAGENT_DINGTALK_ALLOWED_CHAT_IDS") or "").split(",") if c.strip()
        ),
        dingtalk_free_response_chats=tuple(
            c.strip() for c in
            (os.environ.get("SOPHAGENT_DINGTALK_FREE_RESPONSE_CHATS") or "").split(",") if c.strip()
        ),
        dingtalk_mention_patterns=os.environ.get("SOPHAGENT_DINGTALK_MENTION_PATTERNS", ""),
        dingtalk_webhook_url=os.environ.get("SOPHAGENT_DINGTALK_WEBHOOK_URL", ""),
        dingtalk_home_channel=os.environ.get("SOPHAGENT_DINGTALK_HOME_CHANNEL", ""),
        dingtalk_reply_emotion=os.environ.get("SOPHAGENT_DINGTALK_REPLY_EMOTION", "true").lower()
        not in {"0", "false", "no", "off"},
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


async def effective_qq_config(db) -> tuple[str, str]:
    """Runtime-effective QQ Bot credentials.

    DB settings win over env defaults. Empty app_id or client_secret disables
    the QQ IM gateway.
    """
    app_id = await db.get_setting("qq_app_id")
    client_secret = await db.get_setting("qq_client_secret")
    cfg = get_config()
    return (app_id or cfg.qq_app_id, client_secret or cfg.qq_client_secret)


@dataclass
class FeishuConfig:
    app_id: str
    app_secret: str
    domain: str = "feishu"
    connection_mode: str = "websocket"
    verification_token: str = ""
    encrypt_key: str = ""


async def effective_feishu_config(db) -> FeishuConfig:
    """Runtime-effective Feishu credentials and connection settings."""
    cfg = get_config()
    app_id = await db.get_setting("feishu_app_id")
    app_secret = await db.get_setting("feishu_app_secret")
    domain = await db.get_setting("feishu_domain")
    connection_mode = await db.get_setting("feishu_connection_mode")
    verification_token = await db.get_setting("feishu_verification_token")
    encrypt_key = await db.get_setting("feishu_encrypt_key")
    return FeishuConfig(
        app_id=(app_id or cfg.feishu_app_id or "").strip(),
        app_secret=(app_secret or cfg.feishu_app_secret or "").strip(),
        domain=(domain or cfg.feishu_domain or "feishu").strip() or "feishu",
        connection_mode=(connection_mode or cfg.feishu_connection_mode or "websocket").strip() or "websocket",
        verification_token=(verification_token or cfg.feishu_verification_token or "").strip(),
        encrypt_key=(encrypt_key or cfg.feishu_encrypt_key or "").strip(),
    )


async def effective_weixin_config(db):
    """Runtime-effective Weixin credentials. DB settings win over env defaults."""
    from .im.platforms.weixin import WeixinConfig, resolve_config

    cfg = get_config()
    account_id = await db.get_setting("weixin_account_id")
    token = await db.get_setting("weixin_token")
    base_url = await db.get_setting("weixin_base_url")
    resolved_account_id = (account_id or cfg.weixin_account_id or "").strip()
    resolved_token = (token or cfg.weixin_token or "").strip()
    resolved_base_url = (
        (base_url or cfg.weixin_base_url or "https://ilinkai.weixin.qq.com").strip().rstrip("/")
        or "https://ilinkai.weixin.qq.com"
    )
    cdn_base_url = (
        (await db.get_setting("weixin_cdn_base_url") or cfg.weixin_cdn_base_url or "https://novac2c.cdn.weixin.qq.com/c2c")
        .strip().rstrip("/")
    )
    split_raw = await db.get_setting("weixin_split_multiline")
    split_multiline = (
        str(split_raw).lower() in {"1", "true", "yes", "on"}
        if split_raw is not None
        else cfg.weixin_split_multiline
    )
    if resolved_account_id and resolved_token:
        return WeixinConfig(
            account_id=resolved_account_id,
            token=resolved_token,
            base_url=resolved_base_url,
            cdn_base_url=cdn_base_url,
            split_multiline=split_multiline,
        )
    return resolve_config(
        account_id=resolved_account_id,
        token=resolved_token,
        base_url=resolved_base_url,
        data_dir=cfg.data_dir,
        cdn_base_url=cdn_base_url,
        split_multiline=split_multiline,
    )


async def effective_weixin_allowed_user_ids(db) -> tuple[str, ...]:
    raw = await db.get_setting("weixin_allowed_user_ids")
    cfg = get_config()
    text = (raw if raw is not None else ",".join(cfg.weixin_allowed_user_ids)).strip()
    return tuple(u.strip() for u in text.split(",") if u.strip())


async def effective_weixin_dm_policy(db) -> str:
    raw = await db.get_setting("weixin_dm_policy")
    cfg = get_config()
    policy = (raw or cfg.weixin_dm_policy or "pairing").strip().lower()
    if policy not in {"pairing", "allowlist", "disabled"}:
        return "pairing"
    return policy


def mask_weixin_account_id(account_id: str) -> str:
    aid = (account_id or "").strip()
    if len(aid) <= 8:
        return aid or ""
    return f"{aid[:4]}...{aid[-4:]}"


def mask_dingtalk_client_id(client_id: str) -> str:
    cid = (client_id or "").strip()
    if len(cid) <= 8:
        return cid or ""
    return f"{cid[:4]}...{cid[-4:]}"


async def effective_dingtalk_config(db):
    from .im.platforms.dingtalk import DingTalkConfig

    cfg = get_config()
    client_id = await db.get_setting("dingtalk_client_id")
    client_secret = await db.get_setting("dingtalk_client_secret")
    card_template_id = await db.get_setting("dingtalk_card_template_id")
    robot_code = await db.get_setting("dingtalk_robot_code")
    dm_policy = await effective_dingtalk_dm_policy(db)
    require_mention = await effective_dingtalk_require_mention(db)
    allowed_chat_ids = await effective_dingtalk_allowed_chat_ids(db)
    free_response_chats = await effective_dingtalk_free_response_chats(db)
    mention_patterns = await effective_dingtalk_mention_patterns(db)
    resolved_client_id = (client_id or cfg.dingtalk_client_id or "").strip()
    resolved_client_secret = (client_secret or cfg.dingtalk_client_secret or "").strip()
    resolved_card_template_id = (card_template_id or cfg.dingtalk_card_template_id or "").strip()
    reply_emotion_raw = await db.get_setting("dingtalk_reply_emotion")
    if reply_emotion_raw is None:
        reply_emotion = cfg.dingtalk_reply_emotion
    else:
        reply_emotion = str(reply_emotion_raw).lower() in {"1", "true", "yes", "on"}
    if not resolved_client_id or not resolved_client_secret:
        return None
    return DingTalkConfig(
        client_id=resolved_client_id,
        client_secret=resolved_client_secret,
        card_template_id=resolved_card_template_id,
        robot_code=(robot_code or cfg.dingtalk_robot_code or resolved_client_id).strip(),
        dm_policy=dm_policy,
        require_mention=require_mention,
        allowed_chat_ids=allowed_chat_ids,
        free_response_chats=free_response_chats,
        mention_patterns=mention_patterns,
        reply_emotion=reply_emotion,
    )


async def effective_dingtalk_allowed_user_ids(db) -> tuple[str, ...]:
    raw = await db.get_setting("dingtalk_allowed_user_ids")
    cfg = get_config()
    text = (raw if raw is not None else ",".join(cfg.dingtalk_allowed_user_ids)).strip()
    return tuple(u.strip() for u in text.split(",") if u.strip())


async def effective_dingtalk_dm_policy(db) -> str:
    raw = await db.get_setting("dingtalk_dm_policy")
    cfg = get_config()
    policy = (raw or cfg.dingtalk_dm_policy or "pairing").strip().lower()
    if policy not in {"pairing", "allowlist", "disabled"}:
        return "pairing"
    return policy


async def effective_dingtalk_require_mention(db) -> bool:
    raw = await db.get_setting("dingtalk_require_mention")
    cfg = get_config()
    if raw is not None:
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return cfg.dingtalk_require_mention


async def effective_dingtalk_allowed_chat_ids(db) -> tuple[str, ...]:
    from .im.platforms.dingtalk.gating import parse_id_list

    raw = await db.get_setting("dingtalk_allowed_chat_ids")
    cfg = get_config()
    text = raw if raw is not None else ",".join(cfg.dingtalk_allowed_chat_ids)
    return parse_id_list(text)


async def effective_dingtalk_free_response_chats(db) -> tuple[str, ...]:
    from .im.platforms.dingtalk.gating import parse_id_list

    raw = await db.get_setting("dingtalk_free_response_chats")
    cfg = get_config()
    text = raw if raw is not None else ",".join(cfg.dingtalk_free_response_chats)
    return parse_id_list(text)


async def effective_dingtalk_mention_patterns(db) -> tuple[str, ...]:
    from .im.platforms.dingtalk.gating import parse_mention_patterns

    raw = await db.get_setting("dingtalk_mention_patterns")
    cfg = get_config()
    text = raw if raw is not None else cfg.dingtalk_mention_patterns
    return parse_mention_patterns(text)


async def effective_dingtalk_webhook_url(db) -> str:
    raw = await db.get_setting("dingtalk_webhook_url")
    cfg = get_config()
    return (raw if raw is not None else cfg.dingtalk_webhook_url or "").strip()


async def effective_dingtalk_home_channel(db) -> str:
    raw = await db.get_setting("dingtalk_home_channel")
    cfg = get_config()
    return (raw if raw is not None else cfg.dingtalk_home_channel or "").strip()


async def effective_dingtalk_reply_emotion(db) -> bool:
    raw = await db.get_setting("dingtalk_reply_emotion")
    cfg = get_config()
    if raw is None:
        return cfg.dingtalk_reply_emotion
    return str(raw).lower() in {"1", "true", "yes", "on"}

