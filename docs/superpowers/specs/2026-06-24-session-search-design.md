# 会话搜索设计（参考 hermes）

> 子项目 ②／4。`session_search` 工具：FTS5 全文检索历史对话，让 agent 回忆几周前聊过的内容。整体 roadmap 见 `2026-06-24-memory-enhancement-design.md`。

## 背景与目标

hermes 的 `session_search` 用 SQLite FTS5 对全部历史会话做全文检索，无 LLM 调用、无摘要、无截断，按需召回过往上下文。sophagent 已把每条消息存进 `messages` 表（`content` 为 `Message.to_json()`），具备做 FTS 的基础。

### 目标

为 agent 增加 `session_search` 工具，支持三种调用形态（靠参数推断，无 `mode` 参数）：

1. **发现**（`query`）：FTS5 检索 → 按 session 去重 → top N 个会话，每个带命中片段 + 命中点附近窗口。
2. **浏览**（无参）：按时间倒序列最近会话。
3. **翻阅**（`session_id` + `around_message_id`）：返回锚点 ±window 切片，发现后取更多上下文。

### 关键约束：多用户隔离

sophagent 是多用户服务器，hermes 是单用户。**每个查询强制 `sessions.user_id = ctx.user_id`**——agent 只能搜索调用者本人的会话。这是不可妥协的安全边界。

### 非目标（YAGNI）

- bookends（首尾各 3 条消息）——±window + 翻阅已能重建上下文，裁掉。
- 索引 tool 消息——只索引 `user` + `assistant`（工具输出多为噪声）。
- `sort` / 复杂 `role_filter` 参数——v1 用 FTS5 相关性默认排序。
- 跨 session lineage 去重（hermes 有 session 派生关系，sophagent 没有）。

## 设计

### A. 数据模型：FTS5 虚表

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text,
  message_id UNINDEXED,
  session_id UNINDEXED,
  user_id UNINDEXED,
  role UNINDEXED,
  tokenize = 'unicode61'
);
```

- 只索引 `Message.content` 非空的 `user` / `assistant` 消息。
- `user_id` 冗余存入（避免每次查询都 JOIN sessions 才能过滤；UNINDEXED 列可在 `WHERE` 中与 `MATCH` 共用）。

**同步维护**（在 `db.py` 现有写路径内，纯 Python，不用触发器——因为 `messages.content` 是 JSON，触发器难解析）：

| 写路径 | FTS 动作 |
|---|---|
| `append_messages` | 为每条 user/assistant 且 content 非空的新消息插入 FTS 行（需拿到各行 message_id） |
| `truncate_from`（undo/re-edit 删消息） | 删除对应 message_id 的 FTS 行 |
| `compact_session`（归档原始 + 插入压缩历史） | 原始行归档保留 → FTS 保留（检索真实历史）；新插入的压缩消息 → 同 append 规则入 FTS |

**回填迁移**：`connect()` 末尾，若 `messages_fts` 为空而 `messages` 非空 → 从存量 user/assistant 消息回填（一次性，幂等：以空表为判据）。

`append_messages` 需调整：当前用 `executemany` 拿不到各行 id，改为逐行 `INSERT` 拿 `lastrowid`（每轮消息数很小，无性能问题），插入后写对应 FTS 行。`user_id` 由 `session_id` 反查一次。

### B. 工具 `session_search`

注册到工具注册表，scoped 到 `ctx.user_id`、`ctx.db`。参数：

```
query: str             # 给 → 发现形态
session_id: str        # 给 session_id+around_message_id → 翻阅形态
around_message_id: int
limit: int = 5         # 发现：返回会话数
window: int = 5        # 命中/锚点上下文条数
```

形态推断：有 `query` → 发现；有 `session_id`+`around_message_id` → 翻阅；都没有 → 浏览。

**发现**返回（每个会话）：
```
session_id, title, when(updated_at), snippet(FTS5 highlight),
match_message_id, messages: [{id, role, text, is_anchor}], (±window 条)
```
按 FTS5 相关性取命中行，按 session 去重（每 session 取最佳命中），取 top `limit`。

**翻阅**返回：校验 `session_id` 归属当前用户后，返回该 session 内锚点 ±window 条消息（`{id, role, text}`）+ `messages_before/after` 计数（让 agent 判断是否到边界）。无 FTS。

**浏览**返回：当前用户最近 `limit` 个会话（`session_id, title, when, preview(首条 user 消息截断)`）。

输出为紧凑文本（与现有工具一致返回 `str`），不返回 JSON 对象——保持与 registry `dispatch` 的 `str` 约定。格式人类/LLM 可读，每会话一块。

### C. 安全

- 发现/浏览：`WHERE user_id = ctx.user_id`（FTS 表冗余列）/ `sessions.user_id`。
- 翻阅：先 `get_session(session_id, ctx.user_id)`，None 则返回 `Error: session not found`，杜绝越权读他人会话。
- FTS5 query 语法错误（用户传了非法 MATCH）→ 捕获异常，返回友好错误而非 500。

### D. 注入 + 提示（`agent/prompt.py`）

当 agent 工具含 `session_search` 时，system prompt 加一行引导：
> 当用户提到过往对话（"上次""之前提过""记得吗"）或你怀疑存在相关历史上下文时，先用 session_search 回忆，而不是让用户重复。

### E. 测试（`tests/test_session_search.py`，fake provider / 临时库）

- FTS 回填：旧库（有 messages 无 fts）connect 后能搜到存量消息。
- append 同步：新消息可被搜到；tool 消息不入索引。
- truncate 同步：删消息后搜不到。
- **用户隔离**：bob 搜不到 alice 的会话（发现 + 翻阅越权均拒绝）。
- 发现：返回命中会话 + ±window 窗口 + anchor 标记。
- 浏览：无参返回最近会话。
- 翻阅：session_id+around 返回切片；越权 session_id 返回 not found。
- FTS5 非法查询语法被优雅处理。

## 影响面

| 文件 | 改动 |
|---|---|
| `sophagent/db.py` | SCHEMA 加 `messages_fts`；`append_messages` 逐行插入 + FTS 同步；`truncate_from`/`compact_session` 同步；回填；新增 `search_messages`/`session_window`/`recent_sessions` 查询方法 |
| `sophagent/tools/session_search.py` | 新工具，三形态分发 + 输出格式化 |
| `sophagent/tools/__init__.py` | 注册新工具 |
| `sophagent/agent/prompt.py` | session_search 引导语 |
| `tests/test_session_search.py` | 新增 |

无前端变更。无 API 路由变更（工具对 agent 透明）。
