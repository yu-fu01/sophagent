# 凭据脱敏加固设计规格

- 日期：2026-06-24
- 分支：feature-redact
- 状态：已批准，待编写实现计划

## 背景

上一个功能「上下文压缩增强」（已合并 master `12fe7f0`）在 docker 真实环境验证时发现一个安全缺陷：**凭据脱敏失效**。

当前压缩摘要的脱敏**只靠 prompt 指令**——`compaction.py` 的 `SUMMARIZER_SYSTEM` 要求摘要器把凭据替换为 `[REDACTED]`。在 docker 用 DeepSeek-V4-Flash 实测时，故意埋入对话的三个敏感值被摘要器**原样泄漏**：

- 硬编码密钥 `sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c`
- DB 连接串密码 `Pa55w0rd!2026`
- API key `AKIA1234567890SOPHNET`

（摘要里同时出现了 `[REDACTED]` 字样，说明模型部分理解了指令，但未可靠贯彻。）

**根因**：脱敏依赖较小/快的模型自觉执行，不可靠。摘要会落库（`messages` 表）、经 API 返回前端、并作为上下文重新喂给后续模型轮次——凭据被持久化并二次传播。

**参考**：hermes-agent 的 `agent/redact.py`（521 行）提供确定性正则脱敏 `redact_sensitive_text`，并在压缩器里**入口和出口双向调用**（`context_compressor.py:1182` 序列化输入脱敏、`:1413` 摘要输出脱敏），不依赖 LLM 自觉。本设计移植其核心思路，精简到 sophagent 够用的量。

## 目标

用确定性正则脱敏替代/兜底"靠 LLM 自觉"的脱敏，在压缩流程的输入与输出双向拦截凭据，确保摘要中不含明文凭据。

## 设计

### 1. 新增 `sophagent/agent/redact.py`（脱敏模块）

单一职责模块，唯一公开入口：

```python
def redact_sensitive_text(text: str) -> str
```

对文本跑一组正则，把命中的凭据替换为 `[REDACTED]`，返回脱敏后的文本。纯函数、无副作用、不抛异常（正则 `sub` 不会抛）。

**覆盖的凭据模式**（取 hermes 高频项，YAGNI 裁剪）：

- **厂商 API key 前缀**：`sk-`（OpenAI/Anthropic）、`sk_live_`/`sk_test_`（Stripe）、`AKIA[A-Z0-9]{16}`（AWS）、`ghp_`/`github_pat_`/`gho_`/`ghs_`（GitHub）、`xai-`、`tvly-`、`AIza`（Google）、`hf_`、`xox[baprs]-`（Slack）等常见前缀（前缀 + 连续 token 字符，长度下限防误伤）
- **环境变量赋值**：`KEY=value`，其中 KEY 名含 `API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH`（保留键名，值替换为 `[REDACTED]`）
- **JSON 字段**：`"password"|"token"|"api_key"|"secret"|"access_token"|"refresh_token"|"auth_token"|"bearer"` 等键的字符串值
- **Authorization / API-key header**：`Authorization: <scheme> <token>`、`x-api-key: <value>`、`api-key:`、`x-auth-token:` 等（保留 header 名与 scheme 词，值脱敏）
- **数据库连接串密码**：`postgres|postgresql|mysql|mongodb(+srv)|redis|amqp://user:PASSWORD@host` 的密码段
- **JWT**：`eyJ` 开头的 1~3 段 base64
- **私钥块**：`-----BEGIN ... PRIVATE KEY----- ... -----END ... PRIVATE KEY-----`

**不移植**（YAGNI）：电话号码、URL query 参数、表单 body、HTTP 请求行 query、`mask_secret` 显示截断助手——这些 sophagent 当前压缩场景用不到。

**替换占位符**：统一用 `[REDACTED]`，与现有 `SUMMARIZER_SYSTEM` prompt 中的措辞一致。

### 2. 接入压缩流程（双道脱敏）

在 `sophagent/agent/compaction.py` 接两道，对齐 hermes：

**入口脱敏** —— `serialize_turns(turns)` 把对话序列化为摘要器输入时，对每条消息内容与 tool call 参数先过 `redact_sensitive_text`。效果：凭据不进入摘要 LLM 的 prompt。

**出口脱敏** —— `summarize(...)` 拿到 LLM 摘要正文后，`return` 前再过一遍 `redact_sensitive_text`。效果：即使模型回吐凭据也被拦掉。`make_summary_message` 包装的摘要正文已脱敏 → 落库、回前端、二次喂模型都安全。

保留现有 `SUMMARIZER_SYSTEM` 里的脱敏指令作为第三层软提示（无害），但系统**不再依赖**它。

### 3. 开关与错误处理

**开关** —— 对齐 hermes 的 `HERMES_REDACT_SECRETS`，加环境变量 `SOPHAGENT_REDACT_SECRETS`：

```python
_REDACT_ENABLED = os.getenv("SOPHAGENT_REDACT_SECRETS", "true").lower() in {"1", "true", "yes", "on"}
```

默认开启。设为 `0`/`false`/`no`/`off` 时 `redact_sensitive_text` 直接返回原文，给本地调试留逃生口。

**错误处理** —— 脱敏是纯文本正则替换，`re.sub` 不抛异常，无需额外 try 包裹。保持简单，不过度防御。开关读取在模块加载时求值一次。

### 4. 测试（TDD）

**新增 `tests/test_redact.py`**：
- 逐类凭据被替换为 `[REDACTED]`：各厂商前缀（sk-/AKIA/ghp_/xai-/tvly- 等）、env 赋值、JSON 字段、Authorization header、x-api-key header、DB 连接串密码、JWT、私钥块
- 正常文本不误伤（普通中英文、代码、不含凭据的 URL/数字）
- 开关关闭（`SOPHAGENT_REDACT_SECRETS=0`）时原样返回

**扩展 `tests/test_compaction.py`**：
- `serialize_turns` 入口脱敏：含凭据的消息序列化后不含原值
- `summarize` 出口脱敏：用 fake provider 模拟 LLM 回吐凭据 → `summarize` 返回值已脱敏

**回归测试**（复现 docker 实测泄漏）：
- 把 `sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c`、DB 连接串含 `Pa55w0rd!2026`、`AKIA1234567890SOPHNET` 三个真实泄漏样本喂入 `redact_sensitive_text` 与 `serialize_turns`/`summarize` → 确认全部被拦

## 兼容性与边界

- 对已落库的旧摘要无追溯效果（仅对新压缩生效）——可接受，本设计针对未来泄漏。
- 脱敏可能对极少数正常文本误伤（如正文里出现形如 key 的字符串）——用长度下限和前缀边界降低误伤；摘要是有损压缩，偶发误伤可接受，安全优先。
- 开关默认开，生产环境无需配置即安全。

## 非目标（YAGNI）

- 不做全局出口脱敏（所有消息持久化处）——本次只聚焦压缩流程，那是泄漏高发区且范围可控。
- 不移植 hermes 的电话/URL query/表单 body/显示截断等模式。
- 不对原始用户消息或 tool 消息做持久化脱敏（它们是用户自己的数据，且改动面大、易误伤）——仅在「喂给摘要器」和「摘要产出」这两个点拦截。
