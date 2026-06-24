# 后台自改进 review 设计（参考 hermes）

> 子项目 ③／4。hermes 自进化的核心引擎：turn 结束后台重放对话，自动写记忆 /
> 改 skill。整体 roadmap 见 `2026-06-24-memory-enhancement-design.md`。

## 背景

hermes 在每个 turn 后 fork 一个后台线程（`spawn_background_review_thread`），用
review 提示词重放对话，让一个工具受限的 review agent 决定是否写记忆 / 改 skill，
没东西就回 "Nothing to save."。默认主模型（暖缓存），通知事后推送到聊天。

sophagent 已具备前台 `memory` / `skill_manage` 工具与无常驻 agent 的 turn 模型
（WS-only 网关，`SessionState` 驱动 turn、`transport` 长驻绑定）。本子项目补上
**自动化引擎**：把"前台靠 agent 自己想起来写"升级为"每轮后台自动 review"。

## 目标

1. **后台解耦执行**：turn 的用户可见答复流式完成后，spawn 一个**独立 asyncio 后台
   任务**跑 review，不占用户答复延迟、不阻塞 session 锁（贴近 hermes 的 fire-and-forget）。
2. **combined review**：一次模型 pass 同时审视记忆 + skill（移植精简版 hermes
   `_COMBINED_REVIEW_PROMPT`）。
3. **out-of-turn 通知**：review 写入后，经 `SessionState` 直接 `transport.emit` 推一个
   `review` 通知事件；客户端断开则丢弃（写入已落库，通知尽力而为）。
4. **记忆安全扫描**（②延期至此）：前台与 review 写入共用 `memory` 工具，在工具内加
   注入 / 外泄 / 不可见 Unicode 扫描，一处全覆盖。

### 关键决策（已确认）

- 落地方式：**真·后台解耦**（独立后台任务 + out-of-turn 推送）。
- 总开关 `self_improve_enabled`：**默认开**。
- review 范围：combined（记忆 + skill 一次过）。
- 模型：主模型（暖缓存）。不引入 hermes 的 aux 便宜模型路由（YAGNI）。

### 非目标（YAGNI）

- 写入审批门控（→ ④）。
- aux 便宜模型 + digest 重放（hermes 的成本优化，以后需要再加）。
- 后台 curator 整合 / skill 去重（hermes 大规模特性）。
- 持久化的 pending 通知队列（断开即丢，不补投）。

## 设计

### A. review 执行核心（`sophagent/agent/review.py`，transport 无关、可单测）

```python
@dataclass
class ReviewResult:
    changed: bool
    summary: str          # "💾 已保存 2 条记忆，patch 了 skill 'x'"，或 "Nothing to save."
    actions: list[str]    # 结构化变更项，便于通知/测试断言

REVIEW_TOOLS = ("memory", "skills_list", "skill_view", "skill_manage")

async def run_review(*, db, skill_store, agent, user_id, history) -> ReviewResult: ...
```

实现：
1. 复制一份 `AgentDef`，`tools` 收窄为 `REVIEW_TOOLS ∩ agent.tools`（agent 没开
   memory 也没开 skill_manage → 直接返回 `changed=False`，不跑模型）。
2. 用 `build_runner(..., on_persist=None)` 构造 review runner——**review 对话不写入
   session 消息日志**，只有 memory/skill 工具的 DB / 文件副作用落地。
3. `runner.run(COMBINED_REVIEW_PROMPT)` 驱动到结束，**丢弃 text，扫描 `tool_call` /
   `tool_result` 事件**统计 memory(add/replace/remove) 与 skill_manage(create/patch/
   write_file) 的成功变更，组装 `ReviewResult`。
4. `max_iterations` 设低（如 6），防失控循环。review runner 直接用 `AgentRunner.run`
   （非 `run_turns`），天然**不递归**触发下一个 review。

### B. 后台调度 + 通知（网关层 `gateway/methods.py` + `session_state.py`）

- `_start_turn` 给 `state.start_turn(...)` 传 `on_done=_make_review_hook(ctx, state)`。
- `_runner` 在 turn 生成器结束后调 `on_done`；hook 内：
  - 若 `config.self_improve_enabled` 关 → 直接返回。
  - 若 `state` 已有 review 在飞（`state.review_running`）→ 跳过（防叠加）。
  - 否则 `asyncio.create_task(_review_and_notify(...))` 并置 `review_running=True`。
- `_review_and_notify`：从 DB 取最新 agent + history → `run_review(...)` → 若
  `changed` 调 `state.push_review_notice(result)`；`finally` 清 `review_running`。
- `SessionState.push_review_notice(result)`：构造 `review` 帧，**直接 emit 到
  `self.transport`**（不进 turn 事件缓冲；断开即丢）。

> 并发安全：review 与用户下一轮可能交叠。记忆是冻结快照语义（下一轮开局才载入），
> 写入是按用户独立行 / 文件，交叠只影响"何时可见"，不破坏正确性——与 hermes 同等。

### C. 记忆安全扫描（`sophagent/agent/memory_guard.py` + 接进 `tools/memory.py`）

`scan_memory(content) -> str | None`（命中返回拒绝原因，否则 None）：
- **prompt 注入**：`ignore (previous|above) instructions`、`disregard ... system`、
  `you are now ...` 之类（大小写无关）。
- **凭据外泄**：写 `authorized_keys`、`curl/wget ... (token|key|secret)`、明文私钥头
  `-----BEGIN ... PRIVATE KEY-----`。可复用 `agent/redact.py` 已有的敏感模式。
- **不可见 Unicode**：零宽字符（U+200B–200D、FEFF）、双向控制符（U+202A–202E、
  2066–2069）。
- 接入点：`memory` 工具 `add` / `replace`，在写库前调用；命中返回
  `Error: rejected by safety scan (<reason>)`，前台与 review 写入都过此关。

### D. 配置（`config.py`）

```python
self_improve_enabled: bool = True       # 后台 review 总开关
review_max_iterations: int = 6          # review runner 迭代上限
```
环境变量 `SOPHAGENT_SELF_IMPROVE`（`0`/`false` 关）。

### E. 测试

**review 核心（`tests/test_review.py`，fake provider 脚本化）**
- 脚本一个 memory add 的 review turn → `run_review` 返回 `changed=True` 且记忆已落库。
- 脚本 "Nothing to save."（无工具调用）→ `changed=False`，无写入。
- review **不污染 session 消息日志**（session messages 不增）。
- agent 未开 memory/skill 工具 → 直接 `changed=False`，不调用模型。
- 工具白名单生效：review runner 的 tools ⊆ REVIEW_TOOLS。

**安全扫描（`tests/test_memory.py` 扩展）**
- 注入文本被拒；凭据外泄被拒；零宽 Unicode 被拒；正常文本通过。
- review 写入也过扫描（注入内容不落库）。

**网关调度（`tests/test_gateway_ws.py` 或 `test_review.py`）**
- `self_improve_enabled=False` → turn 后不 spawn review。
- review_running 防叠加：连发不并行起两个 review。
- `push_review_notice` 经 fake transport 投递 `review` 事件；断开 transport 时静默丢弃。

## 影响面

| 文件 | 改动 |
|---|---|
| `sophagent/agent/review.py` | 新增：`run_review` + `ReviewResult` + COMBINED_REVIEW_PROMPT |
| `sophagent/agent/memory_guard.py` | 新增：`scan_memory` 安全扫描 |
| `sophagent/tools/memory.py` | add/replace 接入 `scan_memory` |
| `sophagent/gateway/session_state.py` | `review_running` 标志 + `push_review_notice` |
| `sophagent/gateway/methods.py` | `_start_turn` 挂 `on_done` review hook |
| `sophagent/gateway/protocol.py` | `review` 事件类型映射（如需要） |
| `sophagent/config.py` | `self_improve_enabled` / `review_max_iterations` |
| `tests/` | `test_review.py` 新增 + `test_memory.py` 扩展 |

前端可后续加通知卡片渲染（本子项目只保证事件投递，渲染留给前端迭代）。
