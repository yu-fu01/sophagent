# 凭据脱敏加固 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 用确定性正则脱敏替代「靠 LLM 自觉」的凭据脱敏，在上下文压缩流程的输入与输出双向拦截凭据，确保摘要中不含明文凭据。

**架构：** 新增 `sophagent/agent/redact.py` 提供纯函数 `redact_sensitive_text(text)`，用一组正则把厂商 API key / env 赋值 / JSON 字段 / Auth header / DB 连接串密码 / JWT / 私钥块替换为 `[REDACTED]`，由 `SOPHAGENT_REDACT_SECRETS` 环境变量开关（默认开）。在 `compaction.py` 的 `serialize_turns`（入口，喂给摘要器前）与 `summarize`（出口，摘要产出后）双道调用它。

**技术栈：** Python 3.12、uv、pytest + pytest-asyncio、标准库 `re`/`os`。

**设计规格：** `docs/superpowers/specs/2026-06-24-credential-redaction-design.md`

**关键事实（实现前必读）：**
- 本仓库 pytest 在 `dev` extra，运行测试**必须**用 `uv run --extra dev pytest ...`（`uv run pytest` 会失败）。
- 参考实现：`/home/fuyu/workspace/hermes-agent/agent/redact.py`（hermes 用 `_mask_token` 部分遮罩；本计划改用统一 `[REDACTED]` 占位，更简单且对压缩场景足够）。
- 接入点已确认：`sophagent/agent/compaction.py`
  - `serialize_turns(turns)`（约 141-159 行）：把消息序列化为摘要器输入。`_clip(text, limit=2000)` 辅助函数在第 137 行。
  - `summarize(provider, model, turns, prev_summary, focus)`（约 228-252 行）：调 LLM，`summary = "".join(parts).strip()`，`return summary or None`。
- docker 实测的三个真实泄漏样本（回归测试必须复现）：`sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c`、DB 连接串含 `Pa55w0rd!2026`（形如 `postgres://admin:Pa55w0rd!2026@db.internal:5432/x`）、`AKIA1234567890SOPHNET`。

---

## 文件结构

- **创建** `sophagent/agent/redact.py` — 脱敏模块，单一职责：`redact_sensitive_text(text) -> str` + 模块级正则常量 + `SOPHAGENT_REDACT_SECRETS` 开关。
- **创建** `tests/test_redact.py` — 脱敏函数的单元测试（逐类凭据、不误伤、开关）。
- **修改** `sophagent/agent/compaction.py` — `serialize_turns` 入口脱敏 + `summarize` 出口脱敏。
- **修改** `tests/test_compaction.py` — 扩展：入口/出口脱敏 + 回归样本。

---

## 任务 1：脱敏模块 `redact.py`

**文件：**
- 创建：`sophagent/agent/redact.py`
- 测试：`tests/test_redact.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_redact.py`：

```python
"""凭据脱敏 redact_sensitive_text 的单元测试。"""

import importlib

import pytest

from sophagent.agent import redact as R


# -- 逐类凭据被替换为 [REDACTED] --------------------------------------------

def test_vendor_prefix_openai():
    out = R.redact_sensitive_text("key is sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c done")
    assert "sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c" not in out
    assert "[REDACTED]" in out


def test_vendor_prefix_aws():
    out = R.redact_sensitive_text("aws id AKIA1234567890SOPHNET here")
    assert "AKIA1234567890SOPHNET" not in out
    assert "[REDACTED]" in out


def test_vendor_prefix_github_and_others():
    for secret in ("ghp_abcdefghij1234567890", "xai-abcdefghij1234567890abcdefghij12",
                   "tvly-abcdefghij1234567890"):
        out = R.redact_sensitive_text(f"token={secret}")
        assert secret not in out, secret
        assert "[REDACTED]" in out


def test_env_assignment():
    out = R.redact_sensitive_text("OPENAI_API_KEY=super-secret-value-123")
    assert "super-secret-value-123" not in out
    assert "OPENAI_API_KEY=" in out  # 键名保留
    assert "[REDACTED]" in out


def test_env_assignment_password():
    out = R.redact_sensitive_text("DB_PASSWORD='Pa55w0rd!2026'")
    assert "Pa55w0rd!2026" not in out
    assert "[REDACTED]" in out


def test_json_field():
    out = R.redact_sensitive_text('{"api_key": "abcd1234efgh5678", "name": "ok"}')
    assert "abcd1234efgh5678" not in out
    assert "[REDACTED]" in out
    assert "ok" in out  # 非敏感字段不动


def test_authorization_header():
    out = R.redact_sensitive_text("Authorization: Bearer abcdef.ghijkl.mnopqr")
    assert "abcdef.ghijkl.mnopqr" not in out
    assert "Authorization:" in out
    assert "[REDACTED]" in out


def test_api_key_header():
    out = R.redact_sensitive_text("x-api-key: my-opaque-key-value-9999")
    assert "my-opaque-key-value-9999" not in out
    assert "[REDACTED]" in out


def test_db_connection_string_password():
    out = R.redact_sensitive_text("postgres://admin:Pa55w0rd!2026@db.internal:5432/sophagent")
    assert "Pa55w0rd!2026" not in out
    assert "[REDACTED]" in out
    assert "admin" in out          # 用户名保留
    assert "db.internal" in out    # host 保留


def test_jwt():
    jwt = "eyJhbGciOiJIUzI1Ni}.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
    out = R.redact_sensitive_text(f"token {jwt} end")
    assert jwt not in out
    assert "[REDACTED]" in out


def test_private_key_block():
    pk = ("-----BEGIN RSA PRIVATE KEY-----\n"
          "MIIEowIBAAKCAQEA1234567890abcdef\n"
          "-----END RSA PRIVATE KEY-----")
    out = R.redact_sensitive_text(f"here is the key:\n{pk}\ndone")
    assert "MIIEowIBAAKCAQEA1234567890abcdef" not in out
    assert "[REDACTED]" in out


# -- 不误伤正常文本 ---------------------------------------------------------

def test_normal_text_untouched():
    txt = "这是一段正常的中文说明，包含代码 foo() 和路径 sophagent/agent/loop.py:42。"
    assert R.redact_sensitive_text(txt) == txt


def test_plain_numbers_and_urls_untouched():
    txt = "访问 https://example.com/docs?page=2 共 12345 行，版本 v1.2.3。"
    assert R.redact_sensitive_text(txt) == txt


# -- 开关 -------------------------------------------------------------------

def test_disabled_returns_original(monkeypatch):
    monkeypatch.setenv("SOPHAGENT_REDACT_SECRETS", "0")
    importlib.reload(R)
    try:
        secret = "sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c"
        assert R.redact_sensitive_text(f"key {secret}") == f"key {secret}"
    finally:
        monkeypatch.setenv("SOPHAGENT_REDACT_SECRETS", "1")
        importlib.reload(R)


def test_empty_and_none():
    assert R.redact_sensitive_text("") == ""
    assert R.redact_sensitive_text(None) is None
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run --extra dev pytest tests/test_redact.py -v`
预期：FAIL，`ModuleNotFoundError: No module named 'sophagent.agent.redact'`

- [ ] **步骤 3：编写最少实现代码**

创建 `sophagent/agent/redact.py`：

```python
"""确定性凭据脱敏——把文本中的密钥/令牌/密码替换为 [REDACTED]。

移植自 hermes-agent/agent/redact.py 的核心思路，精简到 sophagent 压缩场景够用：
用一组正则匹配常见凭据形态，统一替换为 [REDACTED]。每条正则前用廉价的子串
预检 gate（如 "=" in text）以降低无凭据文本的扫描开销。

默认开启；设环境变量 SOPHAGENT_REDACT_SECRETS=0/false/no/off 可关闭（调试用）。
"""

from __future__ import annotations

import os
import re

PLACEHOLDER = "[REDACTED]"

_REDACT_ENABLED = os.getenv("SOPHAGENT_REDACT_SECRETS", "true").lower() in {"1", "true", "yes", "on"}

# 厂商 API key 前缀（前缀 + 连续 token 字符，长度下限防误伤）
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",           # OpenAI / Anthropic (sk-ant-*)
    r"sk_live_[A-Za-z0-9]{10,}",        # Stripe live
    r"sk_test_[A-Za-z0-9]{10,}",        # Stripe test
    r"ghp_[A-Za-z0-9]{10,}",            # GitHub PAT classic
    r"github_pat_[A-Za-z0-9_]{10,}",    # GitHub PAT fine-grained
    r"gho_[A-Za-z0-9]{10,}",            # GitHub OAuth
    r"ghs_[A-Za-z0-9]{10,}",            # GitHub server
    r"AKIA[A-Z0-9]{16}",                # AWS Access Key ID
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

# ENV 赋值：KEY=value，KEY 名含敏感词（保留键名，值脱敏）
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

# API-key style header（x-api-key 等，单值无 scheme）
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

    对任意字符串安全调用——不含凭据的文本原样通过。默认启用，可由
    SOPHAGENT_REDACT_SECRETS 环境变量关闭。每条正则前有廉价子串预检。
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
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run --extra dev pytest tests/test_redact.py -v`
预期：全部 passed。

> 若 `test_jwt` 因样本 JWT 形态未命中，检查 `_JWT_RE` 与样本——样本应以 `eyJ` 开头且含点分段。若 `test_env_assignment_password` 的引号包裹未正确处理，检查 `_ENV_ASSIGN_RE` 的 `\2` 反向引用（引号配对）。不要削弱测试，调正则使其覆盖测试样本。

- [ ] **步骤 5：Commit**

```bash
git add sophagent/agent/redact.py tests/test_redact.py
git commit -m "feat(redact): 新增确定性凭据脱敏模块 redact_sensitive_text"
```

---

## 任务 2：接入压缩流程（入口 + 出口双道脱敏）

**文件：**
- 修改：`sophagent/agent/compaction.py`
- 测试：`tests/test_compaction.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_compaction.py` 末尾追加：

```python
# -- 凭据脱敏接入（入口 serialize_turns + 出口 summarize）--------------------

from typing import AsyncIterator as _AI

_LEAK_SAMPLES = [
    "sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c",
    "AKIA1234567890SOPHNET",
]


def test_serialize_turns_redacts_credentials():
    turns = [
        Message(role="user", content="我的 key 是 sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c"),
        Message(role="user", content="连接串 postgres://admin:Pa55w0rd!2026@db.internal:5432/x"),
        Message(role="user", content="还有 AKIA1234567890SOPHNET"),
    ]
    s = C.serialize_turns(turns)
    assert "sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c" not in s
    assert "Pa55w0rd!2026" not in s
    assert "AKIA1234567890SOPHNET" not in s
    assert "[REDACTED]" in s


class _LeakyProvider:
    """模拟 LLM 在摘要里回吐了凭据。"""
    async def chat(self, *, model, system, messages, tools=None,
                   temperature=None, max_tokens=None, thinking=None) -> _AI:
        leaked = ("## 关键上下文\nAPI key 是 sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c，"
                  "DB 密码 Pa55w0rd!2026，AKIA1234567890SOPHNET")
        turn = AssistantTurn(content=leaked, input_tokens=1, output_tokens=1)
        yield StreamEvent("turn_done", turn=turn)


@pytest.mark.asyncio
async def test_summarize_redacts_leaked_credentials_in_output():
    out = await C.summarize(_LeakyProvider(), "m", [Message(role="user", content="hi")])
    assert out is not None
    assert "sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c" not in out
    assert "Pa55w0rd!2026" not in out
    assert "AKIA1234567890SOPHNET" not in out
    assert "[REDACTED]" in out
```

> 注：`Message`/`AssistantTurn`/`StreamEvent` 已在 `tests/test_compaction.py` 顶部导入；`C` 是 `from sophagent.agent import compaction as C`。若顶部导入缺 `AssistantTurn`/`StreamEvent`，按文件现有导入补齐（参考文件中已有的 `_FakeProvider`）。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run --extra dev pytest tests/test_compaction.py -k "redact" -v`
预期：FAIL（凭据未被脱敏，仍出现在 `serialize_turns` 输出与 `summarize` 返回值中）。

- [ ] **步骤 3：编写最少实现代码**

修改 `sophagent/agent/compaction.py`：

(a) 顶部 import 区（与现有 `from ..models import Message` 等放一起）新增：

```python
from .redact import redact_sensitive_text
```

(b) `serialize_turns` 入口脱敏——把序列化时的每条文本/参数过一遍脱敏。将函数内对消息内容的取用包裹起来。改为：

```python
def serialize_turns(turns: list[Message]) -> str:
    """Serialize messages into summarizer input, keeping tool call/result detail.
    Credentials are redacted here so secrets never reach the summarizer LLM."""
    lines: list[str] = []
    for m in turns:
        if m.role == "tool":
            lines.append(f"[工具结果] {_clip(redact_sensitive_text(m.content))}")
        elif m.tool_calls:
            calls = "; ".join(
                f"{tc.name}({redact_sensitive_text(json.dumps(tc.arguments, ensure_ascii=False))[:300]})"
                for tc in m.tool_calls
            )
            text = redact_sensitive_text((m.content or "").strip())
            lines.append(f"[assistant] {_clip(text)}" + (f"\n  调用工具: {calls}" if calls else ""))
        elif m.content:
            lines.append(f"[{m.role}] {_clip(redact_sensitive_text(m.content))}")
    joined = "\n".join(lines)
    if len(joined) > 60_000:
        joined = joined[:60_000] + "\n…[更早内容已截断]"
    return joined
```

(c) `summarize` 出口脱敏——返回前对摘要正文过一遍。把结尾两行改为：

```python
    summary = redact_sensitive_text("".join(parts).strip())
    return summary or None
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run --extra dev pytest tests/test_compaction.py -k "redact" -v`
预期：2 passed。

再跑该文件全量与全量套件：
`uv run --extra dev pytest tests/test_compaction.py -v` 应全过（原有摘要测试不回归——注意原有 `test_serialize_turns_includes_tool_calls_and_results` 用的内容不含凭据，脱敏不影响它）。

- [ ] **步骤 5：Commit**

```bash
git add sophagent/agent/compaction.py tests/test_compaction.py
git commit -m "feat(compaction): 压缩流程入口+出口双道凭据脱敏"
```

---

## 任务 3：回归验证 + 全量

**文件：** 无新增（用已写测试做端到端确认）

- [ ] **步骤 1：全量测试**

运行：`uv run --extra dev pytest -q`
预期：全部 passed（原 158 + 任务1的 ~14 + 任务2的 2 = ~174）。

- [ ] **步骤 2：确认三个真实泄漏样本被拦（手动核对）**

运行一次性脚本确认 docker 实测的三个样本全部被脱敏：

```bash
uv run --extra dev python -c "
from sophagent.agent.redact import redact_sensitive_text as r
samples = [
    'sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c',
    'postgres://admin:Pa55w0rd!2026@db.internal:5432/sophagent',
    'AKIA1234567890SOPHNET',
]
for s in samples:
    out = r(s)
    leaked = any(x in out for x in ['9f8a7b6c5d4e3f2a1b0c','Pa55w0rd!2026','SOPHNET'])
    print(('LEAK!' if leaked else 'OK   '), '->', out)
"
```
预期：三行全部 `OK`，输出含 `[REDACTED]`，无明文残留。

- [ ] **步骤 3：Commit（如有未提交的收尾改动；否则跳过）**

本任务通常无代码改动。若步骤 1/2 暴露问题需修复，修复后：
```bash
git add -A && git commit -m "test(redact): 回归确认三个真实泄漏样本被拦截"
```

---

## 自检结果

**规格覆盖度：**
- 新增 `redact.py` + `redact_sensitive_text` → 任务 1
- 覆盖模式（厂商前缀/env/JSON/Auth header/api-key header/DB 连接串/JWT/私钥）→ 任务 1 正则 + 测试逐类覆盖
- `SOPHAGENT_REDACT_SECRETS` 开关默认开 → 任务 1（`_REDACT_ENABLED` + `test_disabled_returns_original`）
- 入口脱敏（serialize_turns）→ 任务 2(b) + `test_serialize_turns_redacts_credentials`
- 出口脱敏（summarize）→ 任务 2(c) + `test_summarize_redacts_leaked_credentials_in_output`
- 保留 prompt 软提示不依赖它 → 未改 `SUMMARIZER_SYSTEM`，自动满足
- 回归复现三个真实样本 → 任务 1 测试（sk-/AKIA/DB密码）+ 任务 3 步骤 2 脚本
- 不误伤正常文本 → `test_normal_text_untouched`/`test_plain_numbers_and_urls_untouched`

**占位符扫描：** 无 TODO/待定；每个代码步骤含完整代码。

**类型一致性：** `redact_sensitive_text` 签名（单参数 `text`，返回脱敏文本/None）在任务 1 定义，任务 2 的 import 与三处调用一致；`PLACEHOLDER`/`_REDACT_ENABLED` 仅模块内用；compaction.py 的 `serialize_turns`/`summarize`/`_clip` 签名与现有代码一致（仅在内部包裹脱敏，未改对外签名）。
