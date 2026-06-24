# 记忆写入审批门控设计（参考 hermes）

> 子项目 ④／4（收尾）。`write_approval` 开关：开启后记忆写入（前台 + 后台 review）
> 暂存进 pending 队列等用户审批。整体 roadmap 见
> `2026-06-24-memory-enhancement-design.md`。

## 背景

hermes 的 `write_approval`：开启后 agent 的记忆写入不直接落库，而是 stage 为
pending，用户用 `/memory pending|approve|reject` 审批。这能挡住"agent 把对我的错误
假设写进了画像"。sophagent 已有①记忆、③后台 review 自动写入——审批门控给用户对自动
写入的最终控制权。

## 决策（已确认）

- **范围**：只做记忆审批（memory）。skill 审批（diff UX 更复杂）本期不做。
- **门控粒度**：**全局 admin 开关**，复用 sophagent 现有 settings / `effective_*`
  模式（与 `compress_threshold`、`max_upload_bytes` 一致）。pending 队列**按用户隔离**。

### 非目标（YAGNI）

- skill 写入审批（→ 以后）。
- per-user 门控（多用户服务器用 admin 全局策略更合适）。
- pending 的 Web UI 审批卡片（本期走 slash 命令；前端渲染后续迭代）。

## 设计

### A. 数据模型：`pending_writes` 表

```sql
CREATE TABLE IF NOT EXISTS pending_writes (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'memory',
  op TEXT NOT NULL,                       -- add | replace | remove
  target TEXT NOT NULL DEFAULT 'memory',  -- memory | user
  content TEXT NOT NULL DEFAULT '',
  memory_id INTEGER,                      -- replace/remove 的目标
  origin TEXT NOT NULL DEFAULT 'foreground',  -- foreground | review
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_user ON pending_writes(user_id, id);
```

db 方法：`pending_add(...)`、`pending_list(user_id)`、`pending_get(id, user_id)`、
`pending_remove(id, user_id)`、`pending_clear(user_id)`。

### B. 门控开关（`config.py` + settings 路由）

- `effective_write_approval(db) -> bool`：读 `db.get_setting("write_approval")`，
  `"true"` → True，缺省 False（默认不门控，对齐 hermes 默认 `write_approval: false`）。
- GET `/api/settings` 增加 `write_approval` 字段。
- PUT `/api/settings/write_approval`（admin）：布尔开关。

### C. memory 工具门控（`tools/memory.py`）

`add` / `replace` / `remove` 在执行实际写库前：

1. **安全扫描照旧先跑**（注入直接拒，不进 pending——脏数据不该暂存）。
2. 若 `effective_write_approval(db)` 为 True：把操作 stage 进 `pending_writes`
   （origin 取自 `ctx.services.get("write_origin", "foreground")`），返回
   `Staged {op} for approval [pending {id}] — review with /memory pending`。
   **不**立即跑容量/去重检查（审批执行时再走正常写入逻辑，避免暂存期状态漂移）。
3. 否则维持现状直接写。

`remove` 也门控（删除同样需审批）。`read` 不门控。

### D. review 来源标记（`agent/review.py`）

review runner 的 ToolContext 注入 `services["write_origin"]="review"`，使后台自动写入
stage 时标记 origin=review（便于审批列表区分 `[auto]`）。门控开启时，review 的记忆写入
进入 pending；`ReviewResult` 把 "Staged ... for approval" 也计为一次 action，通知显示
"💾 已暂存 N 条待审批"。

### E. 审批 slash 命令（`agent/commands/builtin/memory.py`）

注册 `/memory`，子命令：
- `/memory pending` — 列出当前用户的 pending（`[id] op target: content预览  [auto?]`）。
- `/memory approve <id|all>` — 执行暂存操作（经正常 memory 写入逻辑：容量/去重/扫描），
  成功后从队列移除；返回逐条结果。
- `/memory reject <id|all>` — 丢弃，不执行。
- 无参或未知子命令 → 用法提示 + 当前门控状态（`effective_write_approval`）。

审批执行 = 把 pending 行的 op 重新跑一遍真实写入（add→memory_add 等），**绕过门控**
（审批本身就是放行）——通过一个内部 `_apply_pending(db, row)` 直写，不再经工具门控分支。

### F. 测试（`tests/test_write_approval.py`）

- 门控关（默认）：memory add 直接落库（不进 pending）。
- 门控开：add/replace/remove 进 pending，记忆表不变。
- 安全扫描在 stage 前生效：注入内容门控开时仍被拒、不进 pending。
- `/memory pending` 列出本用户暂存；`approve <id>` 执行并出队、记忆落库；`reject` 丢弃。
- `approve all` / `reject all`。
- **pending 按用户隔离**：bob approve 不到 alice 的。
- review 写入门控开时 origin=review 进 pending。
- 审批执行走正常写入逻辑：approve 一个超容量的 add 触发整合提示（不静默落库）。
- settings 路由：admin PUT write_approval；非 admin 改被拒；GET 返回当前值。

## 影响面

| 文件 | 改动 |
|---|---|
| `sophagent/db.py` | `pending_writes` 表 + pending_* 方法 |
| `sophagent/config.py` | `effective_write_approval` |
| `sophagent/tools/memory.py` | add/replace/remove 门控分支 + stage |
| `sophagent/agent/review.py` | 注入 `write_origin=review` |
| `sophagent/agent/commands/builtin/memory.py` | 新增 `/memory` 审批命令 + `_apply_pending` |
| `sophagent/agent/commands/__init__.py` | 注册 `/memory` |
| `sophagent/api/settings_routes.py` | write_approval GET/PUT |
| `tests/test_write_approval.py` | 新增 |

前端可后续加 pending 审批卡片；本期保证 slash 命令通路可用。
