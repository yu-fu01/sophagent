# 活动折叠（Worklog Collapse）设计规格

- 日期：2026-06-17
- 分支：`fix-think-disp-too-long`
- 参考原型：`hermes-webui` 的 worklog（工作日志 / 活动折叠）机制
- 前置：`2026-06-17-thinking-display-design.md`（已落地 `reasoning_delta` 事件与 thinking 展示）

## 1. 目标

修复「thinking 展示过长」问题：当一轮里 agent 多次产生思考 + 工具调用时，聊天区会平铺堆出一长串「💡 思考 / 🔧 terminal / 🔧 terminal / …」框。本设计把**一轮内连续的思考 + 工具活动统一折叠为一个可展开的活动组**，组头只显示一行聚合摘要（例如「思考并运行了 2 条命令、读取了 1 个文件」），点击展开才显示明细。最终回答正文不在折叠组内，照常显示。

## 2. 背景与现状

- 前端是单文件 `web/index.html`（约 850 行，无构建、原生 JS）。
- 聊天区把活动**平铺**渲染：
  - `addThinking()`（`web/index.html:344`）→ `details.thinking`（"💡 思考"）
  - `addTool()`（`web/index.html:352`）→ `details.tool`（"🔧 name"）
  - `addBubble()`（`web/index.html:315`）→ user / assistant / err 气泡
- 流式入口 `sendMsg()`（`web/index.html:362-418`）逐事件 append；历史入口 `openSession()`（`web/index.html:232-250`）按存库消息重建。
- 流事件类型：`reasoning_delta {text}`、`text_delta {text}`、`tool_call {id,name,arguments}`、`tool_result {id,preview}`、`error {message}`。
- 历史消息字段：`m.reasoning`（字符串）、`m.tool_calls[]`（`{name, arguments}`，**无结果预览、无 error 标记**）、`m.content`。
- sophagent 工具集：`terminal`、`python_exec`、`read_file`、`write_file`、`edit_file`、`list_dir`、`web_fetch`、`web_search`、`delegate_task`、`memory`、`skills_list`、`skill_view`、`skill_manage`。

## 3. 范围决策

| 维度 | 决策 |
|---|---|
| 方案 | **方案 A：移植 hermes 完整 worklog 机制**（核心逻辑忠实移植，外围 hermes 耦合按 sophagent 现实裁剪） |
| 分组边界 | **整轮**的思考 + 工具合并为一个折叠组；最终回答正文留在组外 |
| 流式行为 | 进行中的组**默认展开**（可见实时思考/工具输出），摘要头实时更新；正文开始或本轮结束后**自动折叠**为一行摘要 |
| 历史 | 重开旧会话时，每个 assistant 回合重建为**默认折叠**的活动组 |
| 摘要文案 | **中文** |
| 折叠粒度 | hermes 的 step→tool-group 内层嵌套**简化为单层**（整轮一个组，组内平铺 thinking-card + tool-card-row） |

## 4. DOM 模型

每一轮（一次用户发送后的完整流式过程）渲染为一个活动容器 `details.worklog`：

```html
<details class="worklog" open>                          <!-- 流式 open；结束/历史 折叠 -->
  <summary class="wl-head">
    <span class="wl-caret">▸</span>
    <span class="wl-sum">思考并运行了 2 条命令、读取了 1 个文件</span>
  </summary>
  <div class="wl-body">
    <div class="thinking-card">…💡 思考内容（markdown）…</div>            <!-- 0 或 1 个 -->
    <div class="tool-card-row" data-tool-kind="shell" data-tool-done="true">
      🔧 terminal · ls -la        → 结果预览
    </div>
    <div class="tool-card-row" data-tool-kind="read" data-tool-done="true">🔧 read_file · a.py</div>
  </div>
</details>
<div class="msg assistant">最终回答正文…</div>            <!-- 组外，照常显示 -->
```

层级（照搬 hermes 三层概念，内层简化）：
- **turn**：一个 `details.worklog`。
- **rows**：组内平铺 `thinking-card`（最多 1 个）+ 多个 `tool-card-row`。
- hermes 的 `step → tool-group` 内层嵌套不移植（整轮折叠时冗余）。

属性标记照搬，供摘要算法与 CSS 使用：`data-tool-kind`、`data-tool-done`、`data-tool-error`、`tool-card-row` 上的 `data-id`（对应 `tool_call.id`，用于匹配 `tool_result`）。

### 分组边界规则

- 流式开始时**惰性创建**当前 worklog（首个 `reasoning_delta` 或 `tool_call` 到来时）。
- `reasoning_delta` → 写入/更新当前 worklog 内的 thinking-card。
- `tool_call` / `tool_result` → 进入当前 worklog 的 tool-card-row。
- `text_delta`（正文）→ **结束当前 worklog**（去 `open` 折叠、定格完成摘要），正文气泡 append 在组**外**。
- 正文之后若再来 `tool_call` → **新开一个 worklog**。
- 本轮结束（stream done / `finally` / `error`）→ 折叠仍处于 open 的当前 worklog。

典型形态：`[worklog(折叠)] → 最终回答气泡`；多步形态：`[组1] 中间正文 [组2] 最终正文`。

## 5. 摘要算法（移植 hermes 逻辑，中文文案）

移植 hermes 的 `_toolActionKind` + `_toolWorklogSummaries` + 多工具合并规则。

### 5.1 kind 映射（按工具名子串判断，照搬 hermes 思路并适配）

| 工具 | kind |
|---|---|
| `terminal`、`python_exec` | shell |
| `read_file`、`skill_view` | read |
| `write_file`、`edit_file`、`skill_manage` | write |
| `list_dir`、`skills_list` | list |
| `web_search` | search |
| `web_fetch` | web |
| `delegate_task` | delegate |
| `memory` | memory |
| 其它 | unknown |

判定顺序须保证 `web_search` 归 search、`web_fetch` 归 web（先判 search 关键字再判 web，或显式按名表）。实现以「显式名表优先，子串兜底」为准，避免歧义。

### 5.2 文案模板（done / doneMany；running / runningMany）

| kind | 1 个（done） | N 个（doneMany） | 进行中（running / runningMany） |
|---|---|---|---|
| shell | 运行了 1 条命令 | 运行了 {n} 条命令 | 运行命令中 / 运行 {n} 条命令中 |
| read | 读取了 1 个文件 | 读取了 {n} 个文件 | 读取文件中 / 读取 {n} 个文件中 |
| write | 修改了 1 个文件 | 修改了 {n} 个文件 | 修改文件中 / 修改 {n} 个文件中 |
| list | 列出了目录 | 列出了 {n} 次 | 列出目录中 / 列出 {n} 次中 |
| search | 搜索了 1 次 | 搜索了 {n} 次 | 搜索中 / 搜索 {n} 次中 |
| web | 访问了 1 个网页 | 访问了 {n} 个网页 | 访问网页中 / 访问 {n} 个网页中 |
| delegate | 委派了 1 个任务 | 委派了 {n} 个任务 | 委派任务中 / 委派 {n} 个任务中 |
| memory | 更新了记忆 | 更新了 {n} 次记忆 | 更新记忆中 |
| unknown | 调用了 1 个工具 | 调用了 {n} 个工具 | 调用工具中 |

### 5.3 合并与前缀规则（照搬 hermes）

- 固定排序 `order = ['shell','read','write','search','web','list','delegate','memory','unknown']`。
- 按 kind 分别计数 running / done；按 order 依次产出片段。
- 多个片段用「、」连接（中文无需首字母大小写转换）。
- 失败：任一 `data-tool-error=true` 计入，末尾追加「{n} 个失败」。
- **思考前缀**：本轮含思考时，摘要前加「思考并」（如「思考并运行了 2 条命令、读取了 1 个文件」）。
- **纯思考无工具**：摘要 = 进行中「思考中…」/ 完成「已思考」。
- **运行中**：组内任一 `data-tool-done=false` 时，摘要走 running 文案（如「运行命令中…」）。

## 6. 流式构建（改造 `sendMsg`）

改造 `web/index.html:378-409` 的事件循环：
- 维护当前 worklog 引用 `wl`（惰性创建）、组内 thinking-card 引用。
- `reasoning_delta`：无 `wl` 则创建；无 thinking-card 则建；累加文本、`mdToHtml` 重渲染；`syncWorklogSummary(wl)`。
- `tool_call`：无 `wl` 则创建；追加 `tool-card-row`（`data-tool-done=false`、`data-id=ev.id`）；`syncWorklogSummary(wl)`。
- `tool_result`：按 `data-id` 找 row，填结果预览，置 `data-tool-done=true`；`syncWorklogSummary(wl)`。
- `text_delta`：若存在 `wl` 则 `closeWorklog(wl)`（去 `open`、定格 done 摘要、置空 `wl`）；正文走现有 `addBubble("assistant")` append 组外。
- `error`：`closeWorklog(wl)`；`addBubble("err", …)`。
- `finally`：`closeWorklog(wl)`。

新增/改造的函数（替换现有 `addThinking`/`addTool`）：
- `ensureWorklog()` → 创建并 append `details.worklog`，返回引用。
- `worklogThinkingCard(wl)` → 获取/创建组内 thinking-card。
- `appendToolRow(wl, name, args, id)` → 创建 `tool-card-row`，返回引用。
- `setToolResult(row, preview)` / `setToolDone(row)`。
- `toolKind(name)` / `worklogSummary(wl)` / `syncWorklogSummary(wl)`。
- `closeWorklog(wl)`。

## 7. 历史重建（改造 `openSession`）

改造 `web/index.html:240-247`：每条 assistant 消息重建一个**默认折叠**的 `details.worklog`：
- `m.reasoning` → thinking-card（折叠）。
- `m.tool_calls[]` → 每个一个 `tool-card-row`，全部按 `data-tool-done=true`、无 error、无结果预览（sophagent 未存）。
- 调用 `syncWorklogSummary` 生成定格摘要。
- `m.content` → 走 `addBubble("assistant")`，组外。
- 与流式构建**共用** `ensureWorklog`/`appendToolRow`/`worklogSummary` 等函数。
- 若某消息既无 reasoning 又无 tool_calls，则不建 worklog（只显示正文）。

## 8. 折叠状态机

- 基于原生 `<details open>`，点击 summary 切换——**不移植** hermes 的手写 max-height JS 状态机与 disclosure-key/capture-restore。
- 默认：流式中 `open`；结束 / 历史去掉 `open`（折叠）。
- caret 方向用纯 CSS（`details[open] .wl-caret`）。

## 9. CSS

新增类（沿用现有变量 `--panel`/`--panel2`/`--border`/`--dim`/`--accent`/`--red`）：
- `.worklog`：左侧细边或缩进对齐现有 thinking 视觉；`details` 去除默认 marker。
- `.wl-head`：flex 行，hover 背景，`cursor:pointer`，`color:var(--dim)`。
- `.wl-caret`：`▸`，`details[open] .wl-caret{transform:rotate(90deg)}`，加 `transition`。
- `.wl-sum`：摘要文字，dim 色、可斜体。
- `.wl-body`：组内容器，缩进 + 左细线。
- `.thinking-card`：复用现有 `details.thinking .think-body` 的排版（`mdToHtml` 渲染、`max-height` 滚动）。
- `.tool-card-row`：复用现有 `details.tool` 的紧凑样式；`pre` 结果预览 `max-height` 滚动；`data-tool-error=true` 时红色标记。

## 10. 裁剪清单（hermes 特有，不移植）

- SSE / `messages.js` 那套（sophagent 有自己的 stream loop）。
- 子 agent / 委派进度专用 UI、transparent stream 模式。
- i18n（`t()`）——用静态中文。
- `li()` 图标库——用 unicode / 内联字符。
- `step → tool-group` 内层嵌套。
- localStorage 持久化、跨 re-render 的 disclosure capture/restore、全局展开偏好。

## 11. 测试与验收

- **摘要算法单测思路**：`worklogSummary` 是纯函数（输入 rows 的 kind/done/error，输出字符串），可独立验证：单工具、多 kind 合并、运行中、失败计数、思考前缀、纯思考。
- **手动验收**（参考 docker 测试环境，DeepSeek-V4-Flash provider，端口 8848）：
  1. 发起一个会触发多次工具调用的请求 → 流式时组展开可见实时活动 → 正文开始后组自动折叠为一行摘要。
  2. 摘要文案正确（如「思考并运行了 2 条命令、读取了 1 个文件」）。
  3. 点击摘要头可展开/折叠。
  4. 刷新 / 重开会话 → 历史回合渲染为默认折叠的活动组，展开后明细正确。
  5. 多步（正文→工具→正文）形态分成多个组，正文气泡在组外。

## 12. 验收标准

- 一轮多步活动不再平铺成长列，而是折叠为一行聚合摘要，可展开看明细。
- 流式体验：进行中展开、结束折叠。
- 历史会话同样折叠。
- 改动集中在 `web/index.html`（CSS + JS），无后端改动。
