# 动态 Provider 与用量观测设计规格（REQ3.1–3.4）

- 日期：2026-06-17
- 分支：`feature-model-optional`
- 参考原型：`hermes-agent` / `hermes-webui`（provider 管理、模型列表、缓存命中率算法、context indicator）

## 1. 目标

| REQ | 目标 |
|---|---|
| 3.1 | Web 上动态管理 provider（含 `api_url`/`apikey`/`model`），并能在会话里临时切换 |
| 3.2 | 按 provider 拉取其可用模型列表 |
| 3.3 | 在会话里选择「思考模式 / 快速模式」 |
| 3.4 | 显示 token 用量、上下文长度、缓存命中率 |

## 2. 背景与现状

- **Provider 是服务端静态配置**：`config.py` 的 `_load_providers` 从 `providers.yaml` / `$SOPHAGENT_PROVIDERS` 一次性加载到 `get_config().providers`（`dict[str, ProviderConfig]`）。README 与代码注释明确：**API key 永不入库**，agent 只按名字引用 provider。
- 多处**直接读静态 dict**：`api/agent_routes.py::_validate`、`providers/__init__.py::get_provider`、`agent/loop.py`（`get_config().providers[agent.provider].context_limit`）。
- **Agent** 存 `provider`（名字）+ `model`（字符串），Agents 页用文本框手填 model（`web/index.html` `agentForm`）。
- **Token 用量**：`AssistantTurn.input_tokens/output_tokens` 由适配器产出，runner 累加进 `self.usage`，仅在 `done` 事件透出；**无缓存指标**，聊天 UI 也**完全不显示**用量。
- **无思考/快速模式开关**；OpenAI 系思考模型的 reasoning 已通过 `reasoning_delta` 流式展示（见 thinking-display 规格）。
- **DB 模式**：`db.py` 用 `SCHEMA`（`CREATE TABLE IF NOT EXISTS`）建表 + `_migrate_structure()`（守卫式 `ALTER TABLE ADD COLUMN` / 重建表）做幂等迁移。

## 3. 范围决策

| 维度 | 决策 |
|---|---|
| 3.1 切换层级 | **Provider 管理页（入库）+ 会话级覆盖 model/思考模式** |
| 3.1 归属与权限 | **仅管理员全局管理**；`providers.yaml` 保留为**只读内置源** |
| key 存储 | **对称加密入库**（用现有 `config.secret` 派生密钥）+ API 响应**掩码** |
| 3.3 实现 | **统一「思考/快速」开关 → 映射各 provider 原生参数** |
| 3.3 OpenAI 映射 | 思考 → `reasoning_effort="high"`；**快速模式后期细化**（首版不带该参数，用 provider 默认） |
| 3.4 缓存命中率 | **照搬 hermes 算法**：`cache_read /（input + cache_read + cache_write）× 100` |
| 3.4 展示 | **会话状态栏累计 + 每轮小标签** |

非目标（YAGNI）：provider 按组/按用户隔离；agent 级思考模式默认值；快速模式的精细参数映射。

## 4. 架构方案（方案 A：统一 ProviderRegistry）

把当前「启动时一次性加载的静态 dict」升级为「**内置（yaml，只读）+ 托管（DB）合并**」的动态注册表。

> 备选方案（已否决）：启动时把 yaml 导入 DB、运行时只读 DB。否决原因——破坏「yaml 只读内置」语义，且首启迁移 / 重复导入需处理幂等，反而更绕。

### 4.1 ProviderRegistry

新增 `sophagent/providers/registry.py`：

```python
@dataclass
class ResolvedProvider:
    name: str
    api_mode: str            # "openai" | "anthropic"
    api_key: str             # 解密后的明文，仅驻内存
    base_url: str | None
    context_limit: int
    source: str              # "builtin" | "db"

class ProviderRegistry:
    def __init__(self, db, config): ...
    async def refresh(self) -> None: ...          # 重新合并 builtin + db，bump 版本号
    def resolve(self, name: str) -> ResolvedProvider: ...   # db 优先，builtin 兜底
    def names(self) -> list[str]: ...
    def client(self, name: str) -> Provider: ...  # 按 (name, 指纹) 缓存 Provider 客户端
```

- **合并顺序**：同名时 DB 覆盖 builtin（DB 优先）。
- **客户端缓存**：key = `(name, fingerprint)`，`fingerprint` 由 `api_mode/base_url/api_key/context_limit` 哈希得出；provider 改动后指纹变化 → 自动重建连接池，旧条目丢弃。
- **生命周期**：`main.py` 启动时 `app.state.providers = ProviderRegistry(db, cfg)` 并 `await refresh()`；provider CRUD 后调用 `refresh()`。

### 4.2 调用方改造

| 位置 | 改动 |
|---|---|
| `providers/__init__.py::get_provider` | 保留薄封装，改为委托 `app.state.providers.client(name)`；或直接在调用点用 registry |
| `agent/runtime.py::build_runner` | 取 `registry.resolve(effective_provider)` 与 `registry.client(...)`，传入 runner |
| `agent/loop.py` | runner 不再读 `get_config().providers[...]`，改用注入的 `ResolvedProvider`（拿 `context_limit` 与 client） |
| `api/agent_routes.py::_validate` | provider 合法性改查 `registry.names()` |
| `api/openai_compat.py` | 同步走 registry（按需）|

## 5. 数据模型

### 5.1 新表 `providers`（加入 `SCHEMA`）

```sql
CREATE TABLE IF NOT EXISTS providers (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  api_mode TEXT NOT NULL,            -- 'openai' | 'anthropic'
  base_url TEXT,
  api_key_enc TEXT,                  -- Fernet 密文（base64 文本）
  context_limit INTEGER NOT NULL DEFAULT 100000,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

### 5.2 `sessions` 新增覆盖列（`_migrate_structure` 守卫式 ALTER）

```sql
ALTER TABLE sessions ADD COLUMN override_provider TEXT;   -- NULL = 用 agent 的
ALTER TABLE sessions ADD COLUMN override_model TEXT;      -- NULL = 用 agent 的
ALTER TABLE sessions ADD COLUMN thinking_mode TEXT;       -- NULL/'default'|'thinking'|'fast'
```

迁移按现有写法：`if session_cols and "override_provider" not in session_cols: ALTER ...`。

### 5.3 加密模块 `sophagent/crypto.py`

```python
# 用 config.secret 经 HKDF-SHA256 派生 32 字节 → urlsafe_b64 → Fernet
def get_fernet(secret: str) -> Fernet: ...
def encrypt(plaintext: str, secret: str) -> str: ...
def decrypt(token: str, secret: str) -> str: ...
def mask_key(plaintext: str) -> str:   # "sk-abc...wxyz" -> "sk-***wxyz"
```

- 依赖 `cryptography`（新增依赖；Fernet=AES-128-CBC+HMAC，足够）。
- 解密失败（如 secret 轮换）→ 记日志并视该 provider 为「key 缺失」，不崩溃。

## 6. 后端 API

### 6.1 Provider 管理（admin only）— `api/provider_routes.py`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/providers` | 列出 builtin + db，**key 掩码**，带 `source`、`api_mode`、`base_url`、`context_limit` |
| POST | `/api/providers` | 新建 db provider（明文 key 入参 → 加密入库），201 |
| PUT | `/api/providers/{name}` | 改 db provider；key 留空表示不变；builtin 不可改 → 400/403 |
| DELETE | `/api/providers/{name}` | 删 db provider；builtin 不可删 |
| GET | `/api/providers/{name}/models` | 见 6.2 |

- 鉴权：复用 admin 判定（`db.is_admin`）。
- 写操作后 `await app.state.providers.refresh()`。
- 请求/响应模型加入 `models.py`：`ProviderCreate`（`name/api_mode/base_url/api_key/context_limit`）、`ProviderPatch`。
- 删除/改名后引用它的 agent 会失效——返回时给出**警告字段**（哪些 agent 仍引用该 provider），但不强制级联（与现有「agent 引用 yaml provider 删除即失效」的宽松语义一致）。

### 6.2 模型列表 — `GET /api/providers/{name}/models`

```python
# openai 模式: AsyncOpenAI(...).models.list() -> [m.id ...]
# anthropic 模式: AsyncAnthropic(...).models.list() -> [m.id ...]
```

- 结果在 registry 内**短期缓存**（如 5 分钟，按 name）。
- provider 不支持 / 报错 → 返回 `{"models": [], "error": "..."}`，前端回退到手填。

## 7. 思考 / 快速模式（REQ3.3）

### 7.1 协议层 `providers/base.py` + `models.py`

`Provider.chat()` 增参 `thinking: str | None = None`（取值 `"thinking"|"fast"|None`）。

### 7.2 OpenAI 适配器

```python
if thinking == "thinking":
    kwargs["reasoning_effort"] = "high"
# fast / None: 首版不加参数，用 provider 默认（后期细化）
```

不支持 `reasoning_effort` 的端点：捕获 `TypeError`/400 后去掉该参数重试（沿用现有对 `stream_options` 的容错思路）。

### 7.3 Anthropic 适配器

```python
if thinking == "thinking":
    kwargs["thinking"] = {"type": "enabled", "budget_tokens": 4096}
    # extended thinking 要求 temperature 不设或为 1；冲突时移除 temperature
# fast / None: 不带 thinking
```

### 7.4 贯通路径

会话 `thinking_mode`（覆盖列）→ `build_runner` → runner 在 `_call_model` 调 `provider.chat(..., thinking=effective_thinking)`。`_summarize_oldest_half` 的内部摘要调用**不传** thinking（始终走快速路径，省成本）。

## 8. 会话级覆盖（REQ3.1）

- runner 计算 **effective provider/model**：`override_* or agent.*`。
- `build_runner(...)` 新增入参 `override_provider/override_model/thinking_mode`（由 chat 路由从 session 行读出传入）。
- API：`PATCH /api/sessions/{id}`（创建者可改）写入三列；返回更新后的 session。
- `GET /api/sessions/{id}` 已返回整行 → 自动带出覆盖列与可用于回显。

## 9. 用量 / 上下文 / 缓存（REQ3.4）

### 9.1 适配器读取缓存 token

- **OpenAI**：`chunk.usage.prompt_tokens_details.cached_tokens` → `cache_read_tokens`（OpenAI 自动缓存，无需额外参数）。
- **Anthropic**：`message_start.usage.cache_read_input_tokens` / `cache_creation_input_tokens`。为让缓存命中率有意义，给 system block 与 tools 加 `cache_control: {"type": "ephemeral"}`（照 hermes 思路启用 prompt caching）。

### 9.2 模型与事件

- `AssistantTurn` 增 `cache_read_tokens: int = 0`、`cache_write_tokens: int = 0`。
- runner `self.usage` 累加这两个字段。
- 新增 **per-turn** 事件：每个 assistant turn 结束后 yield
  `{"type": "turn_usage", "input": …, "output": …, "cache_read": …, "cache_hit": <pct|null>}`。
- `done` 事件扩展：`usage`（累计含 cache）+ `context_length`（用现有 `history_tokens(system, history)` 估算）+ `context_limit`（来自 `ResolvedProvider`）+ `cache_hit`（累计）。

### 9.3 缓存命中率（照搬 hermes，落在后端）

新增 `sophagent/usage.py`：

```python
def cache_hit_percent(cache_read, prompt_total):
    if cache_read <= 0 or prompt_total <= 0:
        return None
    return min(100, round(cache_read / prompt_total * 100))
```

`prompt_total = input_tokens + cache_read_tokens + cache_write_tokens`（与 hermes 口径一致）。统一在后端算，避免前端两处显示漂移。

## 10. 前端改动（`web/index.html`）

### 10.1 Provider 管理卡片（Admin 页）

`renderAdmin()` 增「Providers」卡片：表格列 `name / api_mode / base_url / key(掩码) / context_limit / source / 操作`。builtin 行只读；db 行可编辑/删除。表单字段：name、api_mode（select）、base_url、api_key（password，编辑时留空=不变）、context_limit。

### 10.2 模型下拉

`agentForm` 的 model 输入改为 **datalist / select + 手填兜底**：选 provider 后调 `/api/providers/{name}/models` 填充候选；失败则纯文本输入。

### 10.3 会话工具栏

composer 上方新增一行：`provider`（select，来自 registry）、`model`（同 10.2 联动）、`思考模式`（select：默认/思考/快速）。改动即 `PATCH /api/sessions/{id}` 持久化；打开会话时回显 session 覆盖值（无覆盖则显示 agent 默认占位）。

### 10.4 用量状态栏 + 每轮标签

- **状态栏**（composer 上方或底部细条）：累计 `↑input ↓output`、`上下文 N / limit`（进度条）、`缓存命中 X%`。消费 `done` 的 `usage/context_length/context_limit/cache_hit` 更新。
- **每轮小标签**：助手气泡下方追加一行 dim 小字（`本轮 ↑a ↓b · 缓存 c%`），消费 `turn_usage`。
- 打开历史会话时状态栏先置空/置灰，待下一轮刷新（历史用量不回算，YAGNI）。

## 11. 边界处理

- **provider 被删但 agent 仍引用**：runner 启动时 `registry.resolve` 抛错 → chat 返回 `error` 事件提示「provider 不存在」，不崩溃。
- **同名 builtin 与 db**：DB 优先；列表里都展示并标 `source`，builtin 只读。
- **key 留空更新**：PUT 时 `api_key` 为空 → 保留原密文。
- **secret 轮换 / 解密失败**：该 provider 视为 key 缺失，调用时报清晰错误，不影响其他 provider。
- **缓存字段缺失**：第三方端点不返回 `cached_tokens` → 缓存数为 0、命中率为 `null`，UI 显示「—」。
- **思考参数不被支持**：容错重试去参数（见 7.2）。
- **会话覆盖为空**：一律回落 agent 定义，行为同现状（向后兼容）。

## 12. 测试（沿用 fake provider，无需真实 key）

### 12.1 后端单测
- `crypto`：encrypt→decrypt 往返、`mask_key` 输出、错误密文不崩。
- `providers` CRUD：创建后 `GET` 返回掩码 key、`source=db`；builtin 改/删被拒；refresh 后 registry 可解析。
- registry 合并：同名 db 覆盖 builtin；指纹变化后 client 重建。
- 会话覆盖：设置 `override_*`/`thinking_mode` 后，断言传给 fake provider 的 kwargs（`model`、`thinking`/`reasoning_effort`）符合预期。
- 用量：fake provider 产出带 `cache_read/write` 的 turn → 断言 `turn_usage` 与 `done.usage/context_length/cache_hit` 字段正确；`cache_hit_percent` 边界（0、null、>100 截断）。
- 权限：非 admin 调 provider 写接口 → 403。

### 12.2 前端手动验证清单
1. admin 在 Admin 页新增一个 db provider（填 key）→ 列表显示掩码、source=db。
2. Agents 页选该 provider → model 下拉自动拉到模型列表；选/手填均可保存。
3. 会话工具栏切 provider/model/思考模式 → 刷新页面后保持（已持久化）。
4. 发消息 → 状态栏累计用量与上下文进度更新；每条助手回复下方出现本轮小标签。
5. 用支持缓存的 provider 连续多轮 → 缓存命中率 > 0。
6. 删除被某 agent 引用的 db provider → 该 agent 会话发消息得到清晰 error，不崩。

## 13. 改动文件清单

| 文件 | 改动 |
|---|---|
| `pyproject.toml` | 新增依赖 `cryptography` |
| `sophagent/crypto.py` | **新增**：Fernet 加解密 + 掩码 |
| `sophagent/usage.py` | **新增**：`cache_hit_percent` |
| `sophagent/providers/registry.py` | **新增**：`ProviderRegistry` + `ResolvedProvider` |
| `sophagent/providers/base.py` | `chat()` 增 `thinking` 参数 |
| `sophagent/providers/openai_provider.py` | 读 `cached_tokens`；`reasoning_effort` 映射 + 容错 |
| `sophagent/providers/anthropic_provider.py` | 读 cache token；`cache_control`；`thinking` 映射 |
| `sophagent/providers/__init__.py` | `get_provider` 委托 registry |
| `sophagent/models.py` | `AssistantTurn` 增 cache 字段；`StreamEvent` 增 `turn_usage`；`AgentDef`/session 相关；`ProviderCreate/Patch`、`SessionOverridePatch` pydantic 模型 |
| `sophagent/db.py` | `providers` 表入 SCHEMA；sessions 覆盖列迁移；provider CRUD 与 session override 方法 |
| `sophagent/config.py` | `_load_providers` 标记 builtin 源（供 registry 合并）|
| `sophagent/main.py` | 启动初始化 `app.state.providers` 并 `refresh()` |
| `sophagent/agent/runtime.py` | `build_runner` 注入 effective provider/model/thinking + ResolvedProvider |
| `sophagent/agent/loop.py` | 用注入的 ResolvedProvider；发 `turn_usage`；`done` 扩展用量字段 |
| `sophagent/api/provider_routes.py` | **新增**：provider CRUD + 模型列表 |
| `sophagent/api/__init__.py` | 注册 provider 路由 |
| `sophagent/api/agent_routes.py` | `_validate` 改查 registry |
| `sophagent/api/session_routes.py` | `PATCH` 写覆盖列；chat 路由读覆盖列传入 runner |
| `web/index.html` | Provider 管理卡片、model 下拉、会话工具栏、用量状态栏 + 每轮标签 |
| `tests/test_*.py` | crypto / provider CRUD / registry / 覆盖 / 用量 / 权限 断言 |

## 14. 实现里程碑（供 writing-plans 拆分）

1. **M1 Provider 持久化**：crypto + providers 表 + registry + CRUD API/UI + 调用方接入。
2. **M2 模型列表**：`/models` 端点 + 前端下拉。
3. **M3 会话覆盖 + 思考/快速**：session 覆盖列 + PATCH + chat 参数 + 适配器映射 + 工具栏。
4. **M4 用量/缓存**：适配器缓存字段 + `usage.py` + `turn_usage`/`done` 扩展 + 状态栏与每轮标签。
