# WebSocket 网关（JSON-RPC over WebSocket）设计规格

- 日期：2026-06-24
- 分支：`feature-ws`
- 参考：`/home/fuyu/workspace/hermes-agent` 的 `tui_gateway/`（JSON-RPC over WebSocket 网关）

## 1. 目标

为 sophagent 引入 WebSocket 通道，承载**对话流式推送**与**客户端上行控制命令**（发送/停止/恢复/重新编辑/斜杠/排队），并支持**断线 detach + 重连回放**——客户端在 turn 运行中掉线时，turn 在后台继续，重连后回放漏掉的事件。

协议采用 JSON-RPC 2.0（双向、请求带 id / 事件无 id），与 hermes `tui_gateway` 线兼容。

## 2. 背景与现状

- **框架**：FastAPI + uvicorn（单进程 asyncio），`sophagent/main.py` 的 `create_app()` 挂载 REST 路由。
- **当前对话通道**：`POST /api/sessions/{id}/chat` 返回 `StreamingResponse`（SSE，换行分隔 JSON）。`AgentRunner.run()` 是 async generator，逐个 yield 事件（`text_delta`/`reasoning_delta`/`tool_call`/`tool_result`/`done`/`error` 等）。
- **单向痛点**：SSE 仅 server→client；客户端发停止/恢复/重新编辑/斜杠/排队都得另开 HTTP POST，无法在一条连接上双向交互、也无法做断线重连续传。
- **消息队列**：`SessionManager`（`sophagent/agent/manager.py`）已实现 in-process `asyncio.Queue`，每 session 上限 3 条，忙时自动入队、turn 结束 `queued_next` 消费。
- **并发控制**：`manager.lock_for(session_id)`（per-session 序列化）+ 全局 `Semaphore(max_concurrent_turns=32)`。
- **已有控制 feature**：停止（`POST /stop` + `manager.stop()`）、恢复/重新编辑（`POST /truncate` + `db.truncate_from`）、斜杠命令。
- **认证**：JWT Bearer（`auth.create_token`/`decode_token`，`require_user` 依赖），PyJWT，默认 24h TTL。
- **存储**：SQLite（aiosqlite），session 与 messages 表，消息带 `id` 与 `archived` 标志。
- **前端**：单文件 SPA `web/index.html`，用 `fetch` reader 消费 SSE。

## 3. 范围决策

| 维度 | 决策 |
|---|---|
| WS 承载范围 | 对话流式推送 + 客户端上行控制命令（send/stop/resume/edit/slash/queue）。REST 保留登录、session 列表、文件、settings 等非对话操作 |
| 协议格式 | JSON-RPC 2.0（newline-delimited），与 hermes 线兼容。事件 type 复用 sophagent 现有名，仅套 RPC 信封 |
| 旧通道迁移 | 双轨过渡：阶段1 新增 WS + 保留 SSE/控制 HTTP 路由；阶段2 稳定后删除旧对话路由 |
| 断线策略 | detach + 重连回放：ws 断开 → turn 后台继续、事件写缓冲 → 重连从水位回放 |
| 事件缓冲 | 仅缓存当前进行中 turn 的 `deque(maxlen=500)`，无条件写入、turn 落库后清空；重连回放该 turn 全量事件 |
| Grace 窗口 | 默认 60s（配置项），超时无重连 → reap（task cancel，DB 历史已落不丢） |
| 认证 | query param `?token=<jwt>`（浏览器 WS 不可设自定义 header）+ Origin/Host 校验 |
| 多端同时在线 | 不支持。单活跃 transport，重连即接管（YAGNI） |
| 一次性 ticket 认证 | 不做。本期 query param JWT；access log 脱敏（后续可选） |
| 跨进程/水平扩展 | 不做。session_state 内存态，符合 sophagent 单进程现状 |

## 4. 架构方案

参照 hermes `tui_gateway/`，在 sophagent 新建 `gateway/` 子包，作为 JSON-RPC 网关层。**复用**现有 `AgentRunner`、`SessionManager`、`db`、`auth`，不重写 agent 逻辑；在 `AgentRunner.run()` 与传输之间插入一层 transport 抽象。

> 选型说明：评估过三条路线。
> - 方案 A（per-session 事件缓冲 + 水位回放，纯 async）：最轻，但仅是"通道"层面。
> - 方案 B（完整移植 hermes JSON-RPC 状态机）：双向 RPC + detach/reap 状态机 + contextvar transport 绑定。
> - 方案 C（断线整体序列化内存）：放弃实时性、内存不可控。
> **采用 B**，并在 detach 机制上融合 A 的事件缓冲（见 4.3）——hermes 原版 detach 丢弃中间 token，靠落库最终结果恢复；本规格要求回放漏掉的输出，故 detach 期间写缓冲而非丢弃。

### 4.1 模块划分

```
sophagent/
├── gateway/                  # 新增：JSON-RPC over WebSocket 网关
│   ├── protocol.py           # JSON-RPC 帧构造 + 错误码常量 + sophagent 事件类型
│   ├── transport.py          # Transport Protocol + WSTransport(async) + contextvar 绑定
│   ├── dispatcher.py         # dispatch(req, transport) 路由到 method handler
│   ├── session_state.py      # 运行态：agent task/running/history_version/事件缓冲/detach·grace
│   ├── methods.py            # prompt.submit / session.interrupt / session.resume / slash.exec / message.edit / queue.submit
│   ├── auth.py               # query param JWT 校验 + Origin/Host 校验
│   └── ws.py                 # handle_ws：accept→ready→loop→teardown(reap/detach)
├── agent/                    # 复用：AgentRunner、manager、runtime、loop
├── api/                      # 复用：现有 REST（阶段1保留），新增 ws 路由注册
└── ...
```

### 4.2 数据流

```
Client ──ws──> handle_ws ──> dispatch(req, transport)
                                  ├─ 快方法(session.list/interrupt/resume) → 内联 result
                                  └─ prompt.submit/slash.exec/message.edit → 起 asyncio.Task 跑 AgentRunner.run()
                                         │ 绑定 contextvar=transport（copy_context 进入 task）
                                         每个 yield 事件 ──> session.emit(ev)
                                                          ├─ 写 events 缓冲（无条件）
                                                          └─ 在线 → transport.emit → ws.send_text
                                                              detach 中 → 不 send（已在缓冲）
Client <──ws── 事件流(重连时先回放缓冲未读部分 + 实时)
```

### 4.3 detach 事件缓冲机制（核心）

每个 session 的运行态（`session_state.py`）持有：

```python
events: deque(maxlen=500)     # 仅缓存当前进行中 turn 的事件；turn 落库后清空
task: asyncio.Task            # 正在跑的 turn（detach 中不取消）
running: bool
history_version: int         # 消息落库后自增
_grace: asyncio.TimerHandle | None   # detach 后的 grace 定时器
```

回放语义对齐 `history_version`：客户端 `openSession` 已从 DB 加载全部**已落库** turn 的历史，因此 ws 重连**不重放历史 turn**，只回放当前**进行中 turn** 的缓冲事件。

- **turn 开始** → `events` 清空，准备接收本轮事件。
- **turn 产事件 → `session.emit(ev)`** → `events.append(ev)`（无条件，超 maxlen 截断最早条目），再推给当前 transport（若在线）。
- **ws 断开** → session 的 transport 切到 `DetachedTransport`（写仍进缓冲，但不再 send），task 继续；启动 grace timer（默认 60s）。
- **重连** → `session.resume`：新 `WSTransport` 接管，回放当前进行中 turn 的 `events[0:]` 全量（若无进行中 turn 则缓冲为空、无回放），再接实时流；取消 grace timer。
- **turn 结束** → 落库（`db.append_messages`），`history_version += 1`，发 `session.info`，**清空 `events`**。
- **grace 到期仍无重连** → reap：cancel task、清理 session_state（DB 历史已落，对话不丢）。

> maxlen 截断：针对单个进行中 turn 的 token 量；极端超长轮丢最早中间 token，但保留尾部 `tool_result`/`message.complete` 且最终结果已落库，重连仍可见完整最终消息。

## 5. 协议规格

线格式：newline-delimited JSON-RPC 2.0，双向。

### 5.1 Client → Server（请求，带 id）

| method | params | 说明 | 快/慢 |
|---|---|---|---|
| `session.resume` | `{session_id}` | 恢复/接管 session，含重连回放 | 快 |
| `prompt.submit` | `{session_id, content}` | 发消息驱动 turn；忙时自动入队 | 慢（Task） |
| `session.interrupt` | `{session_id}` | 停止当前 turn | 快 |
| `message.edit` | `{session_id, before_user_ordinal, content}` | 重新编辑（截断后重发） | 慢 |
| `slash.exec` | `{session_id, command}` | 斜杠命令 | 慢 |
| `queue.submit` | `{session_id, content}` | 显式入队（复用 `manager.try_put`） | 快 |

排队语义：`prompt.submit` 忙时自动走 `queue.submit` 逻辑，返回 `{queued:true, position}`，并通过 `queued_next` 事件推送。

### 5.2 Server → Client

- **响应**（匹配 id）：`{"jsonrpc":"2.0","id":"1","result":{...}}` 或 `{"jsonrpc":"2.0","id":"1","error":{"code":-32xxx,"message":"..."}}`
- **事件**（无 id）：`{"jsonrpc":"2.0","method":"event","params":{"type":<ev>,"session_id":"...","payload":{...}}}`

事件 type 复用 sophagent 现有名（不改语义）：`gateway.ready`、`message.start`、`message.delta`（原 text_delta）、`reasoning.delta`、`tool.call`、`tool.result`、`message.complete`（原 done）、`queued_next`、`session.info`（含 `history_version`）、`error`。

### 5.3 错误码

- JSON-RPC 标准：`-32700` parse error / `-32600` invalid request / `-32603` internal error / `-32601` method not found
- 业务码：`4001` 未认证 / `4401` session 不属于该用户 / `4009` session busy（护栏，submit 应自动排队不触发）

## 6. 与现有架构的集成

### 6.1 共享 turn 核心

将 `session_routes.py` 的 `_run_one_turn` 核心提取为共享函数（`sophagent/agent/turn.py` 或 `SessionManager` 方法），HTTP SSE 路由与 RPC `prompt.submit` **都调用它**，差异仅在"事件往哪送"：HTTP 走 `StreamingResponse` 的 `asyncio.Queue`，RPC 走 `session.emit`（缓冲+transport）。turn 逻辑单一来源，双轨语义一致。`SessionManager` 的 lock/semaphore/消息队列（3 条上限）原样复用。

### 6.2 双轨过渡

- **阶段1（共存）**：新增 `gateway/` + 注册 `@app.websocket("/ws")`；现有 SSE chat、stop/resume/edit/slash 路由**全部保留**。前端切到 ws，跑稳。
- **阶段2（删旧）**：稳定后移除 SSE chat 与控制类 HTTP 路由，只留登录、session 列表、文件、settings 等非对话 REST。

### 6.3 认证（`gateway/auth.py`）

`handle_ws` 在 `accept` 前手动校验：query param 取 `token` → `auth.decode_token` + `db.get_user`，失败 `ws.close(code=1008)`。session 归属校验复用 `db.get_session(session_id, user_id)`。Origin/Host 校验：校验 `Host` 头与配置绑定主机一致（防 DNS rebinding，同 hermes）。

> token 进 URL 会落 access log：配套 `/ws` 路径脱敏；后续可选换成一次性 ticket（本期不做）。

## 7. 前端改动（`web/index.html`）

- **建连**：`ws://host/ws?token=<jwt>`，等 `gateway.ready` 后进入交互。
- **收**：按事件 type 分发，与现有 SSE 事件处理同构（`message.delta` 追加气泡、`tool.call`/`tool.result` 渲染工具块、`queued_next` 更新队列、`session.info` 记 history_version 水位）。
- **发**：用户消息/停止/恢复/重新编辑/斜杠 → 发对应 JSON-RPC request，按 id 配对 response。
- **重连**：`onclose` 指数退避重连，成功发 `session.resume`，靠缓冲回放补齐漏看事件（无需用户重发）。
- **灰度开关**：过渡期前端开关控制走 ws 还是 SSE（`?ws=1` 或 localStorage flag），便于回退。

## 8. 测试

`uv run --extra dev pytest`，无新增外部依赖（fastapi 已含 ws 测试支持 + anyio）。

| 层级 | 覆盖 |
|---|---|
| `protocol.py` | 帧构造/解析、错误码、未知 method（-32601） |
| `transport.py` | WSTransport emit 失败处理、contextvar 绑定/重置 |
| `dispatcher.py` | 快/慢方法路由、非法请求、busy 护栏 |
| `session_state.py` | detach→缓冲→重连回放→grace→reap 全状态机、maxlen 截断、并发 task |
| 集成 | `TestClient` ws：认证失败(1008)、submit 流式、interrupt、重连同会话回放、断线后台继续 |
| 回归 | 现有 SSE 路由在阶段1仍通过（共用 turn 核心，语义不变） |

前端无自动化测试（与现状一致），手动验证发送/停止/恢复/重新编辑/斜杠/断线重连六条交互。

## 9. 边界与限制

- 不支持同 session 多端同时在线（单活跃 transport，重连接管）。
- 一次性 ticket / 子协议认证不做（query param JWT）。
- 不做跨进程/水平扩展（session_state 内存态）。
- maxlen 截断下，极端超长单轮最早中间 token 会丢，最终消息已落库仍可见。
- 已知：阶段2 删旧前两套通道并存，需前端灰度开关避免行为漂移（共用 turn 核心已降低风险）。
