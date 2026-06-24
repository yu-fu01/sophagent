# 思考展示（Thinking Display）设计规格

- 日期：2026-06-17
- 分支：`feature-thinking-display`
- 参考原型：`hermes-agent`（reasoning 独立回调 + CLI dim box 展示语义）

## 1. 目标

把思考模型（DeepSeek 等 OpenAI 兼容思考模型）产生的 reasoning 内容，在 Web 聊天界面里**实时流式**展示给用户，并在重开旧会话时展示历史思考。

## 2. 背景与现状

- **OpenAI 系 provider**（`openai_provider.py`）已从流中捕获 `reasoning_content`，攒进 `turn.reasoning`，写库并在下一轮回传给模型——但**从不**作为流事件推给前端，对用户不可见。
- **Anthropic provider** 当前完全不处理 extended thinking（不在本次范围）。
- **agent loop**（`loop.py`）只透出 `text_delta / tool_call / tool_result / done / error`，没有 reasoning 事件。
- **前端**（`web/index.html`）无思考 UI；`openSession` 加载历史时忽略 `reasoning` 字段。
- `Message.reasoning` 已在 `to_dict`/`from_dict` 中持久化，`GET /api/sessions/{id}` 已返回该字段。

## 3. 范围决策

| 维度 | 决策 |
|---|---|
| 覆盖面 | **仅 Web 界面展示**。不动 Anthropic thinking 捕获，不动 OpenAI 兼容层 |
| 实时性 | **实时流式打字机**（思考随模型输出逐字出现） |
| 历史 | **旧会话也展示思考**（reasoning 已持久化） |
| 折叠交互 | **流式时展开，正文开始 / 本轮结束后自动折叠**；历史默认折叠 |

## 4. 架构方案

采用 hermes「reasoning 走独立通道」的模式：新增一条独立的 `reasoning_delta` 事件流，与 `text_delta` 完全平行。

> 备选方案（已否决）：给 `text_delta` 加 `channel` 字段区分 content/reasoning。否决原因——把两类语义不同的流塞进一个事件，loop 和所有 `text_delta` 消费点（含 `openai_compat`）都要加 channel 过滤，改动面更大且易错。hermes 用独立 `reasoning_callback` 也印证了独立通道更清晰。

### 4.1 事件链路

```
openai_provider  --StreamEvent("reasoning_delta")-->  loop  --{"type":"reasoning_delta"}-->  SSE  -->  前端思考面板
```

## 5. 后端改动

### 5.1 `sophagent/models.py`

`StreamEvent.type` 的 Literal 增加 `"reasoning_delta"`：

```python
type: Literal["text_delta", "reasoning_delta", "turn_done"]
```

复用现有 `text` 字段承载思考增量。`AssistantTurn.reasoning` 与 `Message.reasoning` 保持不变（持久化 + 回传逻辑原样保留）。

### 5.2 `sophagent/providers/openai_provider.py`

现在拿到 `reasoning_delta` 仅 append；改为**同时** yield 事件——攒 buffer 用于 `turn_done`/回传，事件用于实时展示，两者不冲突：

```python
reasoning_delta = getattr(delta, "reasoning_content", None)
if reasoning_delta:
    reasoning_parts.append(reasoning_delta)
    yield StreamEvent("reasoning_delta", text=reasoning_delta)
```

Anthropic provider 本次不动（OpenAI 系思考模型是当前唯一会产生 reasoning 的来源）。

### 5.3 `sophagent/agent/loop.py`

`run()` 的事件循环转发新事件：

```python
if ev.type == "text_delta":
    yield {"type": "text_delta", "text": ev.text}
elif ev.type == "reasoning_delta":
    yield {"type": "reasoning_delta", "text": ev.text}
elif ev.type == "turn_done":
    turn = ev.turn
```

### 5.4 无需改动

- `session_routes.py` 的 SSE 为通用 JSON 透传，自动携带新事件。
- `openai_compat.py` 只匹配 `text_delta`/`error`，新事件被天然忽略——符合「不进 OpenAI 兼容层」的范围决策，且不破坏既有行为。
- `GET /api/sessions/{id}` 已在 `m.to_dict()` 返回 `reasoning`，前端历史渲染直接可用。

## 6. 前端改动（`web/index.html`）

参照 hermes CLI 的状态机语义：首个 reasoning token 开框 → 逐字实时流 → 正文 token 一开始即折叠并锁定 → 锁定后忽略迟到 reasoning。

### 6.1 渲染助手 `addThinking()`

仿现有 `addTool`，用 `<details>` 实现：

```js
function addThinking(text) {
  const d = document.createElement("details");
  d.className = "thinking"; d.open = true;          // 流式时默认展开
  const s = document.createElement("summary"); s.textContent = "💡 思考";
  const body = document.createElement("div"); body.className = "think-body";
  body.dataset.raw = text || ""; body.innerHTML = mdToHtml(body.dataset.raw);
  d.append(s, body); $("msgs").appendChild(d); return d;
}
```

思考正文复用现有 `mdToHtml`（先转义、XSS 安全），与 assistant 气泡一致渲染。

### 6.2 流式状态机（`sendMsg` 事件循环）

引入 turn 级引用 `thinkEl`，复刻 hermes 守卫：

| 事件 | 行为 |
|---|---|
| `reasoning_delta` | 若**正文气泡 `bubble` 已存在则忽略**（正文开始即锁定）。否则：`thinkEl` 为空时 `addThinking("")` 开框；把 `ev.text` 追加进 `think-body.dataset.raw` 并实时 `mdToHtml`。 |
| `text_delta` | 首次出现时：若 `thinkEl` 存在 → `thinkEl.open = false`（自动折叠）。然后照常写正文气泡。 |
| `tool_call` | 折叠当前 `thinkEl`；`bubble = null`、`thinkEl = null`（下一轮重新计）。 |
| `done` / `error` | 兜底折叠 `thinkEl`。 |

每个 turn（一次模型调用）对应独立思考面板；工具循环里多轮各自成框。

### 6.3 历史渲染（`openSession`）

assistant 消息若带 `m.reasoning`，在正文气泡前插入**默认折叠**面板：

```js
else if (m.role === "assistant") {
  if (m.reasoning) { const t = addThinking(m.reasoning); t.open = false; }
  for (const tc of m.tool_calls || []) addTool(tc.name, JSON.stringify(tc.arguments), "");
  if (m.content) addBubble("assistant", m.content);
}
```

### 6.4 CSS

新增低调的 `.thinking` 样式（暗色 / 小字 / 左边框），呼应 hermes dim box 观感，与现有 `.tool` 折叠块风格统一。

## 7. 边界处理

- **多轮工具循环**：每次模型调用是独立 turn，各自一个思考面板；`tool_call` 重置 `thinkEl`/`bubble`。
- **正文锁定**：正文 token 一开始即折叠本轮面板并忽略后续迟到 reasoning（hermes `_stream_box_opened` 守卫），避免回答区里再冒思考框。
- **无思考的模型**：不产生 `reasoning_delta`，不出现思考面板，行为与现状一致——向后兼容、零回归。
- **空思考**：`m.reasoning` 为空时历史不渲染面板。
- **停止 / 错误**：`error` 事件兜底折叠面板。

## 8. 测试

### 8.1 后端单测（`tests/`，沿用现有 fake provider）

- fake provider 在流中先 yield 若干 `reasoning_delta` 再 yield `text_delta`。
- 断言 `loop.run()` 透出的事件序列包含 `{"type": "reasoning_delta", "text": ...}`。
- 断言 `text_delta` 内容不含思考文本。
- 断言 `turn.reasoning` 仍被正确攒出并持久化（回传逻辑不回归）。

### 8.2 前端手动验证清单

单文件无构建、无前端测试框架，手动验证：

1. 用 DeepSeek 类思考模型发起对话 → 思考面板实时展开、逐字流入。
2. 正式回答开始 → 思考面板自动折叠。
3. 含工具调用的多轮 → 每轮各自一个思考面板。
4. 重开该历史会话 → assistant 消息上方出现默认折叠的思考面板，可点击展开。
5. 用普通（非思考）模型对话 → 完全不出现思考面板，行为同现状。

## 9. 改动文件清单

| 文件 | 改动 |
|---|---|
| `sophagent/models.py` | `StreamEvent.type` Literal 加 `"reasoning_delta"` |
| `sophagent/providers/openai_provider.py` | reasoning 增量额外 yield 事件 |
| `sophagent/agent/loop.py` | 转发 `reasoning_delta` UI 事件 |
| `web/index.html` | `addThinking()` + 流式状态机 + 历史渲染 + `.thinking` CSS |
| `tests/test_*.py` | 新增 reasoning 流式事件断言 |
