# 定时任务独立 session + 分组 + 气泡时间戳 — 设计规格

- 日期：2026-06-26
- 分支：feat/cron-new
- 状态：已批准设计，待实现

## 背景与动机

当前 cron 任务绑定到用户发起 `cron create` 命令时所在的**聊天 session**，到点直接在该 session 产出输出。这带来两个问题：

1. 定时任务的输出和正常人机对话混在同一个 session 里，互相干扰、难以区分。
2. 多个定时任务都往同一个会话灌内容，历史迅速膨胀、可读性差（压测时已观察到单 session 积压 3000+ 条消息的病态情况）。

本次改动让每个定时任务拥有自己独立的 session，并把这些 session 收纳到用户专属的「定时任务」组，与聊天分离。同时给所有聊天气泡补上时间戳，提升可读性。

## 需求

- **REQ1**：新建定时任务时，单独新建一个 session 发送消息，而不是用当前 session。
- **REQ2**：新建多个定时任务时，每个任务分配一个独立 session；这些 session 都归入用户专属的「定时任务」（cron）组，与其他聊天 session 分开。
- **REQ3**：所有 session 的聊天气泡下方加时间戳，格式形如 `06-23 11:06`。

## 已确认决策

1. **cron 组范围**：每用户一个 cron 组（与现有「每用户个人组」模型一致，多用户互不可见）。
2. **`cron list` 范围**：列出当前用户的全部 cron 任务（跨 session 聚合）。
3. **前端分区**：侧栏新增可折叠「⏰ 定时任务」分区，普通聊天 session 在上，cron session 收入分区。

## 设计

### 1. 数据层（`sophagent/db.py`）

- `groups` 表新增列 `is_cron_group INTEGER NOT NULL DEFAULT 0`（镜像现有 `is_admin_group`）。
- 启动迁移：幂等 `ALTER TABLE groups ADD COLUMN is_cron_group ...`（检查 pragma table_info，缺列才加），与现有迁移风格一致。
- `create_group` 增加 `is_cron_group: bool = False` 参数。
- 新增 `get_or_create_cron_group(user_id) -> int`：查 `owner_id=user_id AND is_cron_group=1` 的组；不存在则 `create_group("定时任务", user_id, is_cron_group=True)`。幂等。
- `list_sessions` JOIN `groups g ON g.id=s.group_id`，结果多带 `g.is_cron_group AS is_cron`。
- `load_messages_with_ids` 的 SELECT 增加 `created_at`，返回三元组 `(id, Message, created_at)`；调用方（API）负责把 `created_at` 拼进消息 dict。

### 2. cron 工具（`sophagent/tools/cron.py`）

**create**：
1. `session = await db.get_session(ctx.session_id)`；取 `agent_id = session["agent_id"]`（新 session 复用当前 agent）。
2. `cron_gid = await db.get_or_create_cron_group(ctx.user_id)`。
3. `new_sid = uuid4().hex`；`await db.create_session(new_sid, ctx.user_id, agent_id, cron_gid, title=name)`。
4. `create_cron_job(...)` 时 `session_id=new_sid`（其余字段不变）。
5. 返回提示中说明已创建独立会话，可在「定时任务」分区查看输出。

循环任务（interval/cron）跨多次触发**复用同一个** session（不每次新建）。

**list**：改用 `db.list_cron_jobs(ctx.user_id)`，列出该用户全部任务，每行附其 `session_id`。

**delete**：先按 owner 取 job，删除其专属 session（`cron_jobs.session_id` 为 `ON DELETE CASCADE`，删 session 会连带删 job）。附带效果：用户在侧栏删除某 cron session 时，对应 job 自动清除。

**pause / resume**：不变（已按 owner + job_id）。

### 3. 调度器（`sophagent/cron/scheduler.py`）

零改动。它本就 fire 到 `job.session_id`，现在该值是专属 cron session。

### 4. API（`sophagent/api/session_routes.py`）

`get_session` 返回的每条消息 dict 增加 `created_at` 字段（来自 `load_messages_with_ids`）。实时流式（WS）路径不变，前端对实时气泡用当前时间。

### 5. 前端（`web/index.html`）

- `loadSessions`：按 `s.is_cron` 把会话拆为「普通」与「cron」两组；普通组照旧渲染在上，cron 组渲染进一个可折叠 `<details>`「⏰ 定时任务」分区。
- `addBubble(cls, text, mid, ts)`：新增 `ts` 参数。user/assistant 气泡下方挂一行小号灰字时间戳。
- 历史渲染（openSession 循环）传 `m.created_at`；实时增量气泡传当前时间 `new Date()`。
- 时间格式化 `fmtTs(iso) -> "MM-DD HH:MM"`，本地时区（`now()` 存 UTC，前端 `new Date(iso)` 自动转本地）。
- 仅 user/assistant 气泡加时间戳；summary、工具/worklog 行不加。

## 测试

### 后端单测（`tests/test_cron.py` 或新增）
- `get_or_create_cron_group` 幂等，且组 `is_cron_group=1`。
- cron 工具 `create` → 产出一个新 session，归属 cron 组，job.session_id 指向它；两次 create 得到不同 session。
- `cron list` 按用户聚合返回跨 session 的全部任务。
- `cron delete` → job 与其 cron session 同时消失（cascade）。
- `load_messages_with_ids` / API `get_session` 带出 `created_at`。

### 前端人工验证（8849 docker）
- 任意 session 气泡下出现 `MM-DD HH:MM` 时间戳。
- 通过对话让 agent 建定时任务 → 侧栏出现「⏰ 定时任务」分区，任务输出落在独立 session、不污染聊天 session。

## 范围外（YAGNI）

cron session 改名、每次触发新建 session、时间戳悬浮显示秒/时区切换、按 group 的多级树形会话列表。
