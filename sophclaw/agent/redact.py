"""确定性凭据脱敏——把文本中的密钥/令牌/密码替换为 [REDACTED]。

移植自 hermes-agent/agent/redact.py 的核心思路,精简到 sophclaw 压缩场景够用：
用一组正则匹配常见凭据形态,统一替换为 [REDACTED]。每条正则前用廉价的子串
预检 gate（如 "=" in text）以降低无凭据文本的扫描开销。

默认开启；设环境变量 SOPHCLAW_REDACT_SECRETS=0/false/no/off 可关闭（调试用）。
"""

from __future__ import annotations

import os
import re

PLACEHOLDER = "[REDACTED]"

_REDACT_ENABLED = os.getenv("SOPHCLAW_REDACT_SECRETS", "true").lower() in {"1", "true", "yes", "on"}

# 厂商 API key 前缀（前缀 + 连续 token 字符,长度下限防误伤）
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",           # OpenAI / Anthropic (sk-ant-*)
    r"sk_live_[A-Za-z0-9]{10,}",        # Stripe live
    r"sk_test_[A-Za-z0-9]{10,}",        # Stripe test
    r"ghp_[A-Za-z0-9]{10,}",            # GitHub PAT classic
    r"github_pat_[A-Za-z0-9_]{10,}",    # GitHub PAT fine-grained
    r"gho_[A-Za-z0-9]{10,}",            # GitHub OAuth
    r"ghs_[A-Za-z0-9]{10,}",            # GitHub server
    r"AKIA[A-Z0-9]{16,}",               # AWS Access Key ID（≥16,贪婪吃完整段）
    r"AIza[A-Za-z0-9_-]{30,}",          # Google API key
    r"xox[baprs]-[A-Za-z0-9-]{10,}",    # Slack
    r"xai-[A-Za-z0-9]{20,}",            # xAI (Grok)
    r"tvly-[A-Za-z0-9]{10,}",           # Tavily
    r"hf_[A-Za-z0-9]{10,}",             # HuggingFace
]
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])"
)
# gate 子串：这些前缀片段任一出现才跑 _PREFIX_RE
_PREFIX_GATES = ("sk-", "sk_", "ghp_", "github_pat_", "gho_", "ghs_",
                 "AKIA", "AIza", "xox", "xai-", "tvly-", "hf_")

# ENV 赋值：KEY=value,KEY 名含敏感词（保留键名,值脱敏）
_SECRET_ENV_NAMES = r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)"
_ENV_ASSIGN_RE = re.compile(
    rf"([A-Z0-9_]{{0,50}}{_SECRET_ENV_NAMES}[A-Z0-9_]{{0,50}})\s*=\s*(['\"]?)(\S+?)\2(?=\s|$)",
    re.IGNORECASE,
)

# JSON 字段："apiKey": "value" 等
_JSON_KEY_NAMES = (
    r"(?:api_?key|token|secret|password|access_token|refresh_token|auth_token|bearer)"
)
_JSON_FIELD_RE = re.compile(
    rf'("{_JSON_KEY_NAMES}")(\s*:\s*)"([^"]+)"',
    re.IGNORECASE,
)

# Authorization / Proxy-Authorization header（保留 header 名与 scheme 词）
_AUTH_HEADER_RE = re.compile(
    r"((?:Proxy-)?Authorization:\s*)([A-Za-z][\w.+-]*\s+)?(\S+)",
    re.IGNORECASE,
)

# API-key style header（x-api-key 等,单值无 scheme）
_SECRET_HEADER_NAMES = (
    r"(?:x-api-key|x-goog-api-key|api-key|apikey|x-api-token|x-auth-token|x-access-token)"
)
_SECRET_HEADER_RE = re.compile(rf"({_SECRET_HEADER_NAMES}\s*:\s*)(\S+)", re.IGNORECASE)

# 数据库连接串密码：protocol://user:PASSWORD@host
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:/\s]+:)([^@/\s]+)(@)",
    re.IGNORECASE,
)

# JWT：eyJ 开头的 1~3 段 base64
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_=-]{4,}){0,2}")

# 私钥块
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)


def redact_sensitive_text(text):
    """把 ``text`` 中的凭据替换为 [REDACTED]。非字符串原样返回；空/None 直接返回。

    对任意字符串安全调用——不含凭据的文本原样通过。默认启用,可由
    SOPHCLAW_REDACT_SECRETS 环境变量关闭。每条正则前有廉价子串预检。
    """
    if text is None:
        return None
    if not isinstance(text, str):
        return text
    if not text or not _REDACT_ENABLED:
        return text

    # 厂商前缀
    if any(g in text for g in _PREFIX_GATES):
        text = _PREFIX_RE.sub(PLACEHOLDER, text)

    # ENV 赋值
    if "=" in text:
        text = _ENV_ASSIGN_RE.sub(lambda m: f"{m.group(1)}={m.group(2)}{PLACEHOLDER}{m.group(2)}", text)

    # JSON 字段
    if ":" in text and '"' in text:
        text = _JSON_FIELD_RE.sub(lambda m: f'{m.group(1)}{m.group(2)}"{PLACEHOLDER}"', text)

    # Authorization header
    if "uthorization" in text:
        text = _AUTH_HEADER_RE.sub(
            lambda m: m.group(1) + (m.group(2) or "") + PLACEHOLDER, text
        )

    # API-key header
    if ":" in text:
        text = _SECRET_HEADER_RE.sub(lambda m: m.group(1) + PLACEHOLDER, text)

    # 私钥块
    if "BEGIN" in text and "-----" in text:
        text = _PRIVATE_KEY_RE.sub(PLACEHOLDER, text)

    # DB 连接串密码
    if "://" in text:
        text = _DB_CONNSTR_RE.sub(lambda m: f"{m.group(1)}{PLACEHOLDER}{m.group(3)}", text)

    # JWT
    if "eyJ" in text:
        text = _JWT_RE.sub(PLACEHOLDER, text)

    return text
