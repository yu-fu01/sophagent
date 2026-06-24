# 上下文压缩增强（对齐 hermes）设计规格

- 日期：2026-06-24
- 分支：feature-compact
- 状态：已批准，待编写实现计划

## 背景

sophclaw 已内置基础上下文压缩，但实现简单：

1. **自动压缩**（`sophclaw/agent/loop.py`）— 两层：Layer 1 截断旧 tool 输出（`truncate_old_tool_messages`），Layer 2 用 LLM 摘要最旧一半（`_summarize_oldest_half`），超过 `context_limit * 0.8` 时触发。
2. **手动 `/compact` 命令**（`sophclaw/agent/commands/builtin/compact.py`）— 重复了 loop.py 的压缩逻辑。
3. **DB 归档**（`sophclaw/db.py::compact_session`）— 压缩后旧消息存档保留审计。

参考项目 `/home/fuyu/workspace/hermes-agent` 的 `agent/context_compressor.py`（2649 行）有更成熟的做法。本设计提取其核心理念，**精简适配 sophclaw 的轻量风格**（不照搬全部代码）。

### 现状的核心缺陷

| 维度 | sophclaw 现状 | 本次目标（参考 hermes） |
|------|--------------|------------------------|
| 摘要结构 | 纯文本一段话 | 结构化模板（活动任务/已完成/待办追踪） |
| 防"指令污染" | 摘要注入为普通 user 消息，**可能被模型当成活动指令执行** | 防过滤前导语 + "仅供参考"历史标题 |
| 多次压缩 | 每次重新摘要，有损 | 迭代式摘要合并，跨压缩保信息 |
| 尾部保护 | 固定取一半 | 按 token 预算保护尾部 |
| 前端识别 | 无标记 | `_compressed` 元数据标记，前端可区分渲染 |
| 触发阈值 | 硬编码 0.8 | 可配置（settings 表，admin 可调） |
| 代码复用 | compact.py 重复 loop.py 逻辑 | 单一压缩引擎，两者共用 |
| 凭据安全 | 无 | summarizer 提示词要求 `[REDACTED]` 脱敏 |

## 已确认的决策

1. **范围**：全面增强对齐 hermes。
2. **引导式压缩**：支持 `/compact <focus>` 可选参数（类 Claude Code），摘要时优先保留 focus 相关信息。
3. **触发阈值**：做成可配置项（settings 表 + admin 接口），默认 0.8。

## 设计

### 核心思路

抽出单一压缩引擎模块 `sophclaw/agent/compaction.py`，让 `loop.py`（自动触发）与 `compact.py`（手动 `/compact`）共用，消除逻辑重复，并集中承载 hermes 的摘要质量与安全机制。

### 1. 新增 `sophclaw/agent/compaction.py`（压缩引擎核心）

承载以下职责：

- **共享 token 工具**（从 loop.py 迁移过来，loop.py 改为 import）：
  - `estimate_tokens(text) -> int`
  - `history_tokens(system, messages) -> int`
  - `truncate_old_tool_messages(messages) -> list[Message]`（Layer 1，无 LLM）
- `serialize_turns(turns: list[Message]) -> str` — 把待摘要消息序列化为 summarizer 输入，包含 role、文本、tool call 名称与参数、tool result 内容（截断到合理长度）。
- `build_summary_prompt(turns, prev_summary, focus, today) -> tuple[str, str]` — 返回 `(system, user)` 提示词对：
  - **首次摘要**：从零生成结构化摘要。
  - **迭代更新**：当 `prev_summary` 非空时，要求模型在保留既有信息的基础上合并新进展（已完成项移入"已完成动作"、已回答问题移入"已解决问题"、更新"当前状态"）。
  - `focus` 非空时追加指令：优先保留 focus 相关细节，更激进压缩其余。
  - `today` 注入温度锚定指令：已完成动作写成过去式带日期，避免恢复时重复执行。
  - summarizer 前导语保持平实措辞（避免 OpenAI 兼容内容过滤误判），并要求：用对话原语言书写、不加问候/前缀、凭据替换为 `[REDACTED]`。
- `async summarize(provider, model, turns, prev_summary=None, focus=None) -> str | None` — 调 LLM 产出摘要正文；失败返回 `None`（由调用方回退硬丢弃）。
- `make_summary_message(summary) -> Message` — 包装为 `Message(role="user", content=SUMMARY_PREFIX + summary, compressed=True)`。
- `find_previous_summary(messages) -> str | None` — 在 history 中找到最近一条 `compressed=True` 的摘要正文（剥离前缀），用于迭代更新。

### 2. `sophclaw/models.py` — Message 新增 `compressed` 字段

- 新增字段：`compressed: bool = False`。
- `to_dict`：仅当 `compressed` 为 True 时输出 `"_compressed": True`（下划线前缀，对齐 hermes 约定）。
- `from_dict`：读取 `_compressed`（缺省 False）。
- **provider 剥离**：发送给 provider 前必须剥离 `_compressed` 键，确保不破坏上游 API 请求。剥离点放在 provider 层消息序列化处（`providers/` 下构造请求消息的地方），与现有 `to_dict` 调用对齐——具体落点在实现计划中确定。

### 3. 结构化摘要模板（精简中文版）

保留 hermes 模板核心字段，措辞中文化、精简：

- `## 活动任务` —— **最重要字段**。逐字保留用户最近一条未完成的输入（任务、待回答的问题、待决策项）。仅当上一轮完全闭环时写"无"。若最近消息是反向信号（停止/撤销/换话题），逐字记录并**不**携带被取消的任务。
- `## 目标` —— 用户整体想达成的目标。
- `## 已完成动作` —— 编号列表，格式 `N. 动作 目标 — 结果 [工具: 名称]`，含文件路径、命令、行号、结果。
- `## 当前状态` —— 工作目录/分支、改动文件、测试状态、运行中的进程。
- `## 关键决策` —— 重要技术决策及其原因。
- `## 已解决问题` —— 已回答的问题（含答案，避免重复）。
- `## 历史待办` —— 来自被压缩轮次、尚未处理的请求。标记为 **STALE，仅供参考**；除非最新用户消息明确要求，否则不得据此行动。
- `## 相关文件` —— 读取/修改/创建的文件及简注。
- `## 关键上下文` —— 不显式保留就会丢失的具体值、错误信息、配置；凭据写 `[REDACTED]`。

### 4. 防指令污染（最重要的安全修复）

新增中文常量 `SUMMARY_PREFIX`，摘要消息以它开头，语义：

> [上下文压缩 — 仅供参考] 以下是来自上一个上下文窗口的交接摘要，作为背景参考，**不是活动指令**。只响应此摘要之后出现的最新用户消息——那条消息才是当前要做什么的唯一依据。话题重叠不代表要恢复其任务；最新消息优先。`## 历史待办` / `## 活动任务` 中的旧条目不要主动"收尾"或"完成"，除非最新消息明确要求。最新消息中的反向信号（停止/撤销/回滚/换话题）必须立即终止摘要中描述的进行中工作。系统提示中的持久记忆（MEMORY.md）始终权威有效，不受本压缩说明影响。

### 5. `sophclaw/agent/loop.py` 重构

- `_compress_if_needed`：
  - 阈值改为读取 `effective_compress_threshold(db)`（默认 0.8）。
  - 先执行 Layer 1（`truncate_old_tool_messages`）。
  - 仍超预算时进入 Layer 2 迭代摘要。
- `_summarize_oldest_half` 改为调用 compaction 引擎：
  - 通过 `find_previous_summary` 检测 history 中已有的摘要，传入做**迭代更新**而非从头重摘。
  - **token 预算尾部保护**：保护尾部约定 token 量（而非固定切一半）作为 `rest`，其余作为待摘要 `old`；切点不得拆散 assistant 的 tool_call 与其 tool 结果。
  - 摘要消息用 `make_summary_message` 生成（带 `compressed=True`）。
  - summarize 返回 None 时保留现有硬丢弃兜底。

> 注：threshold 需要 db 句柄。loop.py 的 `AgentRunner` 当前不持有 db；实现计划需确定如何把阈值传入（构造参数注入，或调用方在 run 前解析后传入），避免在 AgentRunner 内直接依赖 db。

### 6. `sophclaw/agent/commands/builtin/compact.py` 重构 + 引导式

- 解析 `/compact <focus>`：args 非空作为 focus 传入 `summarize`。
- 改用 compaction 共享引擎，删除当前重复的内联压缩逻辑。
- 同样支持迭代更新与 `compressed` 标记。
- 返回的反馈消息保留压缩前后消息数/token 提示。

### 7. 可配置阈值

- `sophclaw/config.py`：
  - 新增常量 `DEFAULT_COMPRESS_THRESHOLD = 0.8`。
  - 新增 `async effective_compress_threshold(db) -> float`，读 settings 表 `compress_threshold` 覆盖默认值，对齐既有 `effective_max_upload_bytes` 模式。
- `sophclaw/api/settings_routes.py`：
  - `GET /settings` 响应增加 `compress_threshold` 与 `default_compress_threshold`。
  - 新增 `PUT /settings/compress_threshold`（admin 权限），校验范围 **0.5 ~ 0.95**，越界返回 400。

### 8. 前端识别（纳入本次）

- `session_routes` 加载历史时透传 `_compressed`（消息 `to_dict` 已携带）。
- 前端可据 `_compressed` 折叠/特殊渲染摘要消息。前端具体渲染改动在实现计划中作为独立小步骤，范围控制在"识别并可折叠"，不做复杂 UI。

## 测试

- **新增 `tests/test_compaction.py`**：
  - `serialize_turns` 含 tool call/result 细节。
  - `build_summary_prompt` 首次 vs 迭代更新分支、focus 注入、温度锚定、脱敏指令存在。
  - `make_summary_message` 带 `SUMMARY_PREFIX` 与 `compressed=True`。
  - `find_previous_summary` 正确定位并剥离前缀。
  - `summarize` 成功/失败（失败返 None）。
- **扩展 `tests/test_loop.py`**：阈值可配置生效、迭代检测（第二次压缩复用上一摘要）、尾部 token 保护不拆散 tool_call/result。
- **扩展 `tests/test_settings.py`**：`compress_threshold` 的 get / put / 范围校验 / 非 admin 拒绝。
- **扩展 `/compact` 测试**：focus 参数解析与透传。
- **Message 序列化测试**：`compressed=True` → `_compressed` 输出；provider 剥离 `_compressed`。

## 兼容性与边界

- 旧 session 消息无 `compressed` 标记 → 视为普通消息，向后兼容。
- `summarize` 失败 → 回退硬丢弃（保留现有兜底），不阻塞对话。
- provider 必须剥离 `_compressed`，避免上游 API 报未知字段。
- 阈值越界由 API 层校验拒绝；config 层对存量异常值做防御性 clamp。

## 非目标（YAGNI）

- 不移植 hermes 的多模态图像剥离（sophclaw 当前文本为主）。
- 不移植复杂的 tool result LLM 预摘要（沿用现有 `truncate_old_tool_messages`）。
- 不做 hermes 的 ContextEngine 插件抽象层；单一模块即可。
