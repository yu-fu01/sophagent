# SSE 移除 + IM 网关（Telegram）设计规格

- 日期：2026-06-24
- 分支：`feat-sse-im`（worktree `feat-sse-im`）
- 参考：`/home/fuyu/workspace/hermes-agent` 的 `gateway/platforms/`（多平台 IM 桥接）、`gateway/stream_dispatch.py`、`tui_gateway/`

## 1. 目标

两个独立需求，一并在此 worktree 完成：

- **REQ1：完全移除 SSE，只保留 WS。** 删除 REST 对话流（`/chat` SSE）与控制类 HTTP 路由（`/stop`、`/truncate`），前端对话通路只走 WebSocket JSON-RPC 网关。
- **REQ2：新增 IM 网关（Telegram，仿 hermes）。** 用户在 Telegram 给 bot 发消息 → 网关路由到 sophagent agent → 流式回复推回该 Telegram chat。配对码绑定 Telegram 身份到 sophagent 用户。

## 2. 背景

- sophagent 已有 JSON-RPC over WebSocket 网关（`sophagent/gateway/`，本会话上一里程碑），承载对话流式推送 + 客户端上行控制命令（send/stop/resume/edit/slash/queue），支持 detach + 重连回放。
- 共享 turn 核心 `sophagent/agent/turn.py`（`run_turns` / `pre_submit`）同时被 SSE 路由与 WS `prompt.submit` 调用。
- `Transport` 抽象（`gateway/transport.py`）：`WSTransport` 把 agent 事件发到浏览器。**IM 网关 = 再加一类 Transport**，把事件发到 IM 平台。这是 REQ2 的架构关键：不重写 agent，复用 `run_turns` + `SessionManager` + `db`。
- hermes 的 IM 网关是多平台聊天桥接（Telegram/Discord/微信/飞书/Signal…），核心抽象：Platform Adapter + `MessageEvent` 归一化 + 会话映射 + 流式分发。Telegram 出站用 **edit-in-place 流式**（`editMessageText` 原地更新）。本规格仅做 Telegram。

## 3. 范围决策

| 维度 | 决策 |
|---|---|
| IM 平台 | 仅 Telegram（`getUpdates` 长轮询，无需公网 URL/SSL，docker 本地可跑）。多平台扩展留接口但本期不实现 |
| 进程模型 | **in-process**。Telegram 轮询作为 lifespan 启动的后台 asyncio task，配 `SOPHAGENT_TELEGRAM_BOT_TOKEN` 才起。直接共享 db/manager，不另开进程（sophagent 是轻量单进程服务） |
| 身份绑定 | **配对码**（仿 hermes）。sophagent 用户在 web UI 生成一次性码 → Telegram 端 `/pair <code>` 绑定到该用户 + 某 agent |
| agent 选择 | 固定在配对时（配对码绑定 user+agent）。`/new` 重置 session；v1 不做 `/agent` 切换（YAGNI） |
| 会话模型 | 一个 `chat_id` ↔ 一个 sophagent session（持久，绑定时创建）。IM 与 web 共享同一 session，双向可见 |
| 出站投递 | **流式编辑**（edit-in-place），对齐 hermes。限频节流；最终 edit 落完整文本 |
| reasoning | IM 不显示思考（忽略 `reasoning.delta`，防刷屏） |
| 工具调用 | v1 忽略 `tool.call`/`tool.result` 细节（不单独显示）；仅正文流式 |
| detach/replay | IM 不复用 `session_state` 的 detach/重连回放（IM 无"重连"概念）；用独立轻量 driver 直驱 `run_turns` + `TelegramTransport` |
| 依赖 | 不加重型 SDK；`httpx` 直连 Telegram Bot API |
| SSE 删除范围 | 删 `/chat`、`/stop`、`/truncate`；REST 保留 login/users/sessions 列表/files/settings/agents/groups/providers/skills/commands/openai-compat |

## 4. REQ1：移除 SSE

### 4.1 删除的后端路由与代码

`api/session_routes.py`：

- 删 `POST /{session_id}/chat`（SSE `StreamingResponse`）及其 `_start_sse_turn`。
- 删 `POST /{session_id}/stop`（WS `session.interrupt` 已替代）。
- 删 `POST /{session_id}/truncate`（前端迁移到 WS 后不再用，见 4.2）。
- 移除 `StreamingResponse` import、`text/event-stream` 相关。

### 4.2 前端迁移

`web/index.html` 当前"恢复/重新编辑"仍调 HTTP `/truncate`：

- **重新编辑**：改用 WS `message.edit`（已存在：截断 `message_id` 及之后 + 立即开轮）。前端 `editing` 块从 `api('/truncate')` + `openSession` + 重发，改为单次 `wsSend("message.edit", {session_id, message_id, content})`，按 ack `{deleted, started}` 接流式事件。
- **恢复**（仅截断不开轮）：新增 WS 方法 `session.truncate`（见 4.3）。前端 `restoreFrom` 改调它，ack 后 `openSession` 重载。

### 4.3 新增 WS 方法 `session.truncate`

- params：`{session_id, message_id}`
- 行为：校验归属 → 忙时拒绝（`4009`）→ `db.truncate_from(session_id, message_id)` → `db.touch_session` → 返回 `{deleted}`。
- 注册进 `gateway/methods.py` 的 `METHODS` 表。
- 协议表更新：`session.truncate` 列入 client→server 方法清单。

### 4.4 测试迁移

`tests/test_api.py` 中走 `/chat` SSE 的对话流测试改为 WS TestClient：

- 复用 `tests/test_gateway_ws.py` 的 `_send`/`_recv`/`_recv_response`/`_collect_events_until` harness（提到 conftest 或共享 helper）。
- 迁移用例：`test_chat_flow_and_persistence`、`test_queued_chat_turns_persist_in_order_without_duplicates`、`test_stop_during_busy_turn_closes_stream_and_clears_queue`、`test_agent_tool_call_via_chat`、`test_reasoning_streamed_and_persisted_via_chat`、`test_retry_replaces_last_assistant_without_duplicating_user`、`test_skill_self_evolution_via_chat`、`test_memory_tool_and_injection`、`test_get_session_returns_message_ids`、`test_truncate_restores_to_user_message` 等——按 WS 事件序列断言。
- `test_overrides_usage.py` 的 chat 用例同理迁移。

非对话 REST 测试（sessions 列表/agents/files/settings/groups）不动。

## 5. REQ2：IM 网关（Telegram）

### 5.1 模块划分（新建 `sophagent/im/`）

```
sophagent/
├── im/                       # 新增：IM 网关（Telegram）
│   ├── adapter.py            # Telegram 适配器：getUpdates 轮询 → MessageEvent；httpx 直连 Bot API
│   ├── transport.py          # TelegramTransport(Transport)：agent 事件 → edit-in-place 流式（限频）
│   ├── pairing.py            # 配对码签发/校验；im_bindings / im_pair_codes 读写
│   ├── driver.py             # 入站调度：解析绑定+session → 跑 run_turns(TelegramTransport)
│   └── commands.py           # IM 命令：/pair /new /stop /help
├── agent/turn.py             # 复用：run_turns
├── gateway/                  # 复用：Transport 抽象、protocol
└── db.py                     # 扩展：im_bindings / im_pair_codes 两表
```

### 5.2 数据流

```
Telegram ──getUpdates──> adapter ──MessageEvent──> driver
                                                    │ /开头 → commands(/pair /new /stop /help)
                                                    │ 否则：
                                                    ├─ 查 im_bindings(chat_id) → (user_id, agent_id, session_id)
                                                    ├─ 未绑定 → 回复"请先 /pair <code>"
                                                    └─ run_turns(session_id, user_input, TelegramTransport(chat_id))
                                                           │ text_delta → edit-in-place（本轮首帧 sendMessage）
                                                           │ done → 最终 edit，清 message_id
                                                           │ reasoning/tool/queued_next → 忽略
Telegram <──sendMessage/editMessageText──  TelegramTransport.on_event
```

### 5.3 入站（adapter.py）

- `httpx` 长轮询 `getUpdates`（long_poll timeout），offset 持久推进（内存即可，重启从最新开始——可接受）。
- 解析 `message.text`、`message.chat.id`、`message.from_user.id` → `MessageEvent{text, chat_id, from_id}`。
- 不处理媒体/sticker/回调（v1 仅文本；非文本消息回"暂只支持文本"）。

### 5.4 出站（transport.py `TelegramTransport`）

`TelegramTransport` **不**实现 WS 的 `gateway.Transport`（IM 不需要 JSON-RPC wire 帧）。它暴露 `on_event(ev: dict)`，直接消费 sophagent 内部事件（`text_delta`/`done`/`error`/…），按语义投递到 Telegram：

- `text_delta`（`ev.text`）：累积文本。**本轮首帧** → `sendMessage` 发一条，记 `message_id`；后续帧 → 限频 `editMessageText`（最小 600ms 一次，防 ~30 edit/min 速率限制）。
- `done`：最终 `editMessageText` 落完整文本，**清空 `message_id`**（下一轮首帧重新 `sendMessage` 新消息）。
- `error`：`editMessageText` 成错误提示（无 `message_id` 则新发一条）。
- `reasoning_delta`、`tool_call`、`tool_result`、`turn_usage`、`queued_next`、`session.info`、`turn.settled`：**忽略**（IM 不显示；多轮由 `done` 清 `message_id` 自然衔接，`queued_next` 不需特殊处理）。
- 限频：持 `last_edit_at` + `pending_text`；未到节流间隔只更新 `pending_text`，下个 tick 或 `done` 时 flush。

### 5.5 driver.py

- `handle_inbound(event: MessageEvent)`：
  1. `/` 开头 → `commands.dispatch`。
  2. 查 `im_bindings`。无绑定 → `sendMessage("请先 /pair <code> 绑定")` 返回。
  3. 取 `session_id`。若 `manager.is_busy(session_id)` → `manager.try_put` 入队（复用 WS prompt.submit 的排队语义）；否则起 task 跑 `run_turns`。
  4. `run_turns(session_id=..., user_input=text, db, manager, skill_store, user_id=绑定用户)`，task 内 `async for ev in run_turns(...): await transport.on_event(ev)`（transport 绑定到 chat_id）。
  5. `manager.register_task(session_id, task)`（`/stop` 复用 `manager.stop`）。
- 单 chat 串行：per-`chat_id` 一个 `asyncio.Lock`（或复用 `manager.lock_for(session_id)`，因 session_id 唯一）。

### 5.6 配对（pairing.py + DB）

**DB 新增两表**（`db.py` 建表 + 方法）：

```sql
CREATE TABLE im_pair_codes (
  code TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  agent_id INTEGER NOT NULL,
  expires_at TEXT NOT NULL,
  used INTEGER DEFAULT 0
);
CREATE TABLE im_bindings (
  platform TEXT NOT NULL,        -- 'telegram'
  chat_id TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  agent_id INTEGER NOT NULL,
  session_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (platform, chat_id)
);
```

DB 方法：`create_pair_code(user_id, agent_id) -> code`、`consume_pair_code(code) -> (user_id, agent_id)|None`（校验未用+未过期，标记 used）、`upsert_binding(platform, chat_id, user_id, agent_id, session_id)`、`get_binding(platform, chat_id)`。

**配对流程**：
1. sophagent 用户在 web UI「IM 绑定」页选 agent → `POST /api/im/pair-code {agent_id}`（新 REST 路由，需登录）→ 返回 8 位码（TTL 10min）。
2. Telegram 端 `/pair <code>` → `pairing.consume_pair_code` → 写 `im_bindings`、`db.create_session`（绑定的 user+agent）→ 回复"✅ 已绑定，agent=<name>，发消息开始对话"。
3. 同一 chat 重复 `/pair` → 覆盖绑定（换 agent）。

**web UI**：「IM 绑定」入口（settings 或新页）：选 agent + 生成码 + 展示码 + "发给 Telegram bot: /pair <code>"。轻量，复用现有 tab 风格。

### 5.7 命令（commands.py）

| 命令 | 行为 |
|---|---|
| `/pair <code>` | 配对绑定（见 5.6） |
| `/new` | 结束当前 session、新建绑定到同一 (user, agent) 的 session；更新 `im_bindings.session_id` |
| `/stop` | `manager.stop(session_id)` 中断当前 turn |
| `/help` | 列出命令 |

非 `/` 开头的文本 → 走 driver 的 turn 路径。sophagent 现有斜杠命令（`/compact` `/model` 等）**不在 IM 暴露**（v1 IM 只有上述 4 条；避免与平台命令冲突）。

### 5.8 进程集成（main.py lifespan）

- lifespan 启动末尾：若 `SOPHAGENT_TELEGRAM_BOT_TOKEN` 非空 → `asyncio.create_task(im.adapter.run_polling())`；存 task 引用，shutdown 时 cancel。
- 不配 token → IM 网关不启用，零开销。

### 5.9 配置（config.py）

新增：
- `telegram_bot_token: str`（env `SOPHAGENT_TELEGRAM_BOT_TOKEN`，默认空）
- `telegram_allowed_user_ids: tuple[int, ...]`（env `SOPHAGENT_TELEGRAM_ALLOWED_USER_IDS`，逗号分隔，默认空=不额外限制，仅配对码控制）

### 5.10 依赖

`httpx` 直连 Telegram Bot API（`getUpdates` / `sendMessage` / `editMessageText`）。若 pyproject 未显式声明 httpx 则补（dev 已有）。不加 `python-telegram-bot` SDK。

## 6. 测试

`uv run --extra dev pytest`。

| 层级 | 覆盖 |
|---|---|
| REQ1 | 迁移后的 WS 对话流测试（chat/queued/stop/retry/tool/reasoning/skill/memory/truncate/overrides）全绿；确认无 `/chat`/`/stop`/`/truncate` 路由残留（404） |
| `im/pairing.py` | 配对码签发/校验/过期/重复使用/覆盖绑定 |
| `im/driver.py` | mock Telegram API（httpx mock）→ 入站文本 → 驱动 `run_turns`（EchoProvider）→ 断言 `sendMessage`/`editMessageText` 调用序列、限频间隔、最终文本；未绑定回复提示；`/pair`/`/new`/`/stop` 命令 |
| `im/transport.py` | 限频节流逻辑、首帧 sendMessage 后续 edit、reasoning/tool 忽略 |
| 集成 | 配对 → 发消息 → 流式 edit 回推（mock Telegram） |

前端无自动化测试（与现状一致），手动验证 IM 绑定页 + 真机 Telegram（docker + 用户自有 bot token）。

## 7. 边界与限制

- IM 仅 Telegram，文本消息；媒体/按钮/回调不支持。
- agent 固定配对时，无 `/agent` 切换；`/new` 重置 session。
- 不复用 `session_state` 的 detach/重连回放（IM 无重连语义）。
- `getUpdates` offset 内存推进，重启可能丢重启窗口内的消息（可接受；可后续持久化 offset）。
- 工具调用细节不在 IM 显示（v1 忽略）。
- REQ1 后 REST 仍保留 openai-compat `/v1`（独立通道，非对话 SSE）。
