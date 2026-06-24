# 记忆增强设计（参考 hermes）

> 子项目 ①／4。本文档只覆盖「记忆增强」。其余三块各有独立的 spec → plan → 实现周期。

## 背景与目标

用户要求「参考 `hermes-agent` 实现自进化和记忆功能」。sophclaw 当前已有：

- **记忆**：`memory` 工具（read/add/replace/remove），按用户存 DB，开局冻结快照注入 system prompt，有单条字符上限 + 条数上限。
- **自进化**：`skill_manage`（create/patch/delete）+ skill 索引注入，全靠 agent 在前台对话里自己决定。

hermes 比 sophclaw 多出的能力被拆为四个子项目，按依赖排序：

| 顺序 | 子项目 | 状态 |
|---|---|---|
| **①** | **记忆增强**（MEMORY/USER 双存储、总字符容量与满额整合、双段注入） | **本文档** |
| ② | 会话搜索（FTS5 全文检索 + `session_search` 工具） | 待办 |
| ③ | 后台自改进 review（turn 后异步重放，自动写记忆/改 skill + WebUI 通知；**含记忆安全扫描**） | 待办 |
| ④ | 写入审批门控（`write_approval` 开关 + pending 审批队列） | 待办 |

**架构约束**：sophclaw 是无常驻 agent 的多用户服务器（每 turn 从 DB 加载历史 → 跑 → 释放）。记忆按用户隔离，只注入该用户自己的 session，不存在跨用户注入路径——因此**安全扫描推迟到 ③**（后台自动写入时风险更高，届时统一把关）。

### 本子项目目标

把单一记忆存储升级为 hermes 风格的双存储 + token 预算容量模型：

1. **双存储**：`memory`（agent 笔记：环境/约定/经验）与 `user`（用户画像：身份/偏好/沟通风格）逻辑分区，各自独立限额。
2. **总字符容量模型**：每个 target 用总字符上限而非条数上限；写满时返回结构化提示，引导 agent 同轮整合后重试。
3. **去重**：拒绝同一 target 内的 exact duplicate。
4. **双段注入**：system prompt 中拆成 MEMORY 段 + USER PROFILE 段，各带 usage 头。

### 非目标（YAGNI）

- 安全扫描（→ ③）、写入审批（→ ④）、会话搜索（→ ②）、后台自动写入（→ ③）。
- 外部记忆 provider 插件（hermes 有 8 个，sophclaw 不引入）。
- 子串匹配的 replace/remove（hermes 用 `old_text` 子串；sophclaw 已用稳定的 `memory_id`，保持不变，更简单可靠）。

## 设计

### A. 数据模型

`memory` 表加一列 `target`：

```sql
ALTER TABLE memory ADD COLUMN target TEXT NOT NULL DEFAULT 'memory';
```

- 取值 `memory` | `user`。
- 迁移在 `db._migrate_structure()` 内，用 `_columns("memory")` guard，幂等。旧条目自动归入 `memory`（agent 笔记），零破坏。
- 新建库的 `SCHEMA` 同步加上该列与默认值。

`db.py` 方法签名调整：

| 方法 | 变更 |
|---|---|
| `memory_list(user_id, target=None)` | `target=None` 返回全部（注入时分两段用）；传 target 时过滤 |
| `memory_add(user_id, content, target)` | 增加 target 参数 |
| `memory_replace(memory_id, user_id, content)` | 不变（按 id 定位，天然带 target） |
| `memory_remove(memory_id, user_id)` | 不变 |

### B. 容量模型（对齐 hermes）

移除条数上限，改用每 target 的总字符上限。`config.py`：

```python
memory_total_chars: int = 2200   # MEMORY 段总量（~800 token）
user_total_chars: int = 1375     # USER 段总量（~500 token）
memory_max_chars: int = 500      # 单条上限（保留）
# 移除 memory_max_items
```

总量按某 target 内所有条目 `content` 字符数之和计。

### C. memory 工具改造（`tools/memory.py`）

工具 schema 增加 `target` 参数：

```
target: enum ["memory", "user"]，默认 "memory"
```

各 action 行为：

- **read**：列出该 target 的条目（`[id] content`）。
- **add**：
  1. 校验单条 ≤ `memory_max_chars`，否则报错。
  2. 去重：该 target 内已有 exact duplicate → 返回成功 + `(no duplicate added)`，不写入。
  3. 容量：若 `当前总量 + 新条目` > 该 target 上限 → **不报死错**，返回结构化提示：
     ```
     Memory at {used}/{limit} chars. Adding this entry ({n} chars) would exceed
     the limit. Consolidate now: use 'replace' to merge overlapping entries into
     shorter ones or 'remove' stale entries (see current_entries below), then
     retry this add — all in this turn.
     current_entries:
     [id] ...
     ```
     文案对齐 hermes，引导 agent 同轮整合。
  4. 通过 → 写入，返回 `Saved memory [id] to {target}`。
- **replace**：同样受单条 + 总量约束（用新内容替换旧条目后若超总量则拒绝并给整合提示，因为换成更长条目也会溢出）。
- **remove**：不变。

容量与去重检查抽到一个纯函数（便于单测）：给定现有条目列表 + 目标 target + 新内容，返回 `ok | duplicate | too_long | over_capacity(used, limit, entries)`。

### D. system prompt 注入（`agent/prompt.py`）

`build_system_prompt` 的 `memories` 入参从 `list[str]` 改为按 target 分组（例如 `dict[str, list[str]]` 或两个列表）。渲染为两段，对齐 hermes 的 usage 头：

```
## MEMORY (your notes) [67% — 1474/2200 chars]
Facts you saved about the environment, conventions, and lessons learned:
- ...

## USER PROFILE [40% — 550/1375 chars]
What you know about this user — identity, preferences, communication style:
- ...
```

- 某段为空时显示占位（如 `(none yet)`），保持冻结快照语义。
- usage 头让 agent 知道剩余容量，便于主动整合（对齐 hermes「>80% 先整合」实践）。
- 注入处（`runtime.build_runner` / 调用 `build_system_prompt` 的地方）改为一次性 `memory_list(user_id)` 拿全量再按 target 分组。

工具说明（tool description）补一句：区分 `memory`（环境/约定/经验）与 `user`（用户身份/偏好/沟通风格）的写入指引，让 agent 知道该往哪个 target 写。

### E. 错误处理

- DB 不可用：维持现状返回 `Error: memory unavailable`。
- 未知 target：返回明确错误（`Error: target must be 'memory' or 'user'`）。
- 未知 action：维持现状。

### F. 测试（`tests/`，全部用 fake provider）

- **迁移**：旧 schema（无 target 列）启动后加列成功，旧条目 `target='memory'`。
- **双存储**：往 `memory` 和 `user` 各写，read 分别只返回对应 target。
- **容量整合**：写到接近上限，再 add 触发 over_capacity 结构化提示（含 current_entries + usage）；replace 成更短条目后 add 成功。
- **单条上限**：超 `memory_max_chars` 拒绝。
- **去重**：重复 add 返回 no-duplicate，不增加条目。
- **注入**：`build_system_prompt` 渲染出 MEMORY + USER PROFILE 两段及正确 usage 头；空段占位。
- 复用现有 `tests/conftest.py` 夹具。

## 影响面

| 文件 | 改动 |
|---|---|
| `sophclaw/db.py` | SCHEMA 加 target 列；`_migrate_structure` 加迁移；`memory_list/add` 签名 |
| `sophclaw/config.py` | 容量配置项调整 |
| `sophclaw/tools/memory.py` | target 参数、容量/去重纯函数、整合提示文案 |
| `sophclaw/agent/prompt.py` | 双段注入 + usage 头 |
| `sophclaw/agent/runtime.py`（或注入调用处） | 按 target 分组传入 |
| `tests/` | 新增/扩展记忆测试 |

无 API 路由变更，无前端变更（记忆对前端透明）。
