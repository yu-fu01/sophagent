# 对话停止 / 恢复 / 重新编辑（Chat Stop · Restore · Re-edit）设计规格

- 日期：2026-06-18
- 分支：`feature-chat-resend`
- 需求：REQ4.1（停止）、REQ4.2（恢复）、REQ4.3（重新编辑）

## 1. 目标

在 Web 聊天界面为每轮对话补齐三类操作：

- **REQ4.1 停止**：消息发出后、agent 回答完毕前，输入区右侧显示停止按钮，可强行中断当前轮。
- **REQ4.2 恢复**：agent 回答完毕后，每条历史用户气泡提供「恢复」按钮；点击后删除该用户消息及其后所有内容，把会话回退到该问题之前。防误操作：每次点击都弹确认。
- **REQ4.3 重新编辑**：最新气泡下方提供「重新编辑」按钮；点击把最新一条用户消息原文填入输入框供编辑，**发送时**才删除原用户消息及其回答，并以编辑后文本作为新轮次发送。放弃编辑则原对话保留。

## 2. 背景与现状

- **停止**：后端 `POST /api/sessions/{id}/stop` + `SessionManager.stop()`（取消 asyncio task）已存在；`session_routes.chat` 的 worker 在 `CancelledError` 时向 SSE 队列投 `{"type":"error","message":"stopped by user"}`。前端 `#stop` 按钮与 `stopTurn()` 已在 `sendMsg` 流式期间切换显示。停止后未完成的 assistant 文本不落库（assistant 消息仅在 `turn_done` 时持久化）。
- **恢复 / 重新编辑**：尚无。消息以扁平有序列表存于 `messages` 表（自增 `id`、`archived` 标志），无"轮次"概念；`get_session` 当前返回 `m.to_dict()`，不含 `id`。
- 前端单文件 `web/index.html`：`openSession` 按角色渲染气泡，用户消息渲染为右对齐气泡（`renderUserContent`），`[附加文件: ...]` 行渲染为文件图标。`[Earlier conversation summary]` 合成用户消息不渲染气泡。

## 3. 范围决策

| 维度 | 决策 |
|---|---|
| 停止 | 后端已具备；本次仅前端润色（停止提示改中性，不显示为红色错误） |
| 恢复按钮位置 | 仅用户气泡下方右侧 |
| 恢复删除边界 | 删除该用户消息及其后所有内容（inclusive） |
| 重新编辑对象 | 始终是最新一条用户消息（跳过合成 summary 消息） |
| 重新编辑按钮位置 | 最新气泡下方（不论最新是助手还是用户） |
| 重新编辑删除时机 | 发送时才删除（先 truncate 再 chat） |
| 截断机制 | 单一 `POST /truncate` 端点，restore 与 re-edit 复用 |
| 持久化 | 截断直接作用于 DB（`archived=0` 行）；多端/刷新一致 |

## 4. 架构方案

新增一个截断端点作为恢复与重新编辑的共同原语：

> 备选方案（已否决）：给 `ChatRequest` 加 `replace_from` 字段，由 chat worker 在锁内原子截断再跑。否决原因——产生两条截断代码路径（端点 + worker）、且修改 `ChatRequest` 影响面更大；本应用单会话单用户，前端"先 truncate 再 chat"两步的竞态可忽略。单端点更简单、复用度高。

### 4.1 交互流

```
恢复：  用户气泡「↩ 恢复」 → confirm → POST /truncate {message_id} → openSession(重载)
重新编辑： 最新气泡「✎ 重新编辑」 → 填入输入框 + 记录 editing.mid
           → 用户点 Send → POST /truncate {message_id: editing.mid} → POST /chat {content} → 流式渲染
停止：   流式中「■ Stop」 → POST /stop → SSE 收到 stopped by user → 中性提示
```

### 4.2 截断语义

`truncate_from(session_id, message_id)` 删除 `messages WHERE session_id=? AND archived=0 AND id>=?`。
- 含该用户消息本身（inclusive），满足"删该用户消息及之后"。
- 只动 `archived=0` 活跃行；历史压缩归档行（`archived=1`）保留为审计，不影响 `load_messages`。
- 若 `message_id` 对应的消息已被压缩归档（不在活跃日志），前端不会为它渲染气泡，故不会触发该路径。

## 5. 后端改动

### 5.1 `sophagent/db.py`

新增两个方法：

```python
async def load_messages_with_ids(self, session_id: str) -> list[tuple[int, Message]]:
    rows = await self._all(
        "SELECT id, content FROM messages WHERE session_id=? AND archived=0 ORDER BY id",
        (session_id,),
    )
    return [(r["id"], Message.from_json(r["content"])) for r in rows]

async def truncate_from(self, session_id: str, message_id: int) -> int:
    cur = await self.conn.execute(
        "DELETE FROM messages WHERE session_id=? AND archived=0 AND id>=?",
        (session_id, message_id),
    )
    await self.conn.commit()
    return cur.rowcount
```

`load_messages`（无 id）保持不变，供 chat worker 使用。

### 5.2 `sophagent/api/session_routes.py`

`get_session` 改用 `load_messages_with_ids`，回传 `id`：

```python
messages = await db.load_messages_with_ids(session_id)
return {**dict(session), "messages": [{"id": mid, **m.to_dict()} for mid, m in messages]}
```

新增端点：

```python
class TruncateRequest(BaseModel):
    message_id: int

@router.post("/{session_id}/truncate")
async def truncate_session(session_id: str, req: TruncateRequest, request: Request, user=Depends(require_user)):
    db = request.app.state.db
    manager = request.app.state.manager
    if await _owned_or_managed(request, session_id, user) is None:
        raise HTTPException(404, "session not found")
    if manager.is_busy(session_id):
        raise HTTPException(409, "session is running a turn")
    deleted = await db.truncate_from(session_id, req.message_id)
    return {"ok": True, "deleted": deleted}
```

`TruncateRequest` 加到 `models.py` 的 API models 区（与 `ChatRequest` 相邻）。`is_busy` 复用现有 `SessionManager.is_busy`（按 session lock 判定）。

### 5.3 无需改动

- `ChatRequest`、`agent/loop.py`、`agent/runtime.py`、`manager.py`：不变。
- `compact_session` / 压缩逻辑：不变（截断只读 `archived` 标志）。

## 6. 前端改动（`web/index.html`）

### 6.1 消息 id 追踪

`openSession` 遍历 `detail.messages`（每条现含 `.id`）：
- 用户消息（非 `[Earlier conversation summary]`）：`addBubble("user", m.content)` 返回的元素上设 `el.dataset.mid = m.id`；同时更新 `lastUserMid = m.id`、`lastUserText = m.content`。
- 渲染完所有消息后，在 `#msgs` 末尾挂「✎ 重新编辑」按钮（若 `lastUserMid` 存在且非流式）。

### 6.2 恢复按钮

`addBubble("user", text)` 在气泡内追加操作行：

```html
<div class="msg-actions"><button class="ghost restore-btn">↩ 恢复</button></div>
```

CSS：`.msg.user .msg-actions{display:flex;justify-content:flex-end;margin-top:6px}`。
绑定 `onclick = () => restoreFrom(parseInt(el.dataset.mid))`。
非用户气泡不放操作行。

### 6.3 重新编辑按钮

末尾按钮：

```js
function renderReeditBtn() {
  // 移除旧按钮，若 lastUserMid 且 !streaming 则在 #msgs 末尾追加「✎ 重新编辑」
}
```

点击 → `$("input").value = lastUserText; editing = {mid: lastUserMid}; $("input").focus()`。

### 6.4 发送逻辑

`sendMsg` 开头：

```js
if (editing) {
  await api(`/api/sessions/${currentSid}/truncate`, {method:"POST", body:{message_id: editing.mid}});
  editing = null;
}
```

随后沿用现有发送流程（追加用户气泡、SSE 流式渲染）。流式期间 `streaming=true`，恢复/重新编辑按钮隐藏。

### 6.5 恢复处理

```js
async function restoreFrom(mid) {
  if (!confirm("此次对话之后的内容都将删除，确定重置？")) return;
  await api(`/api/sessions/${currentSid}/truncate`, {method:"POST", body:{message_id: mid}});
  await openSession(currentSid);
}
```

### 6.6 停止润色

SSE `error` 事件：若 `ev.message === "stopped by user"`，渲染为中性 dim 提示气泡（复用 `addBubble` 加 `stopped` 类，灰色斜体）而非红色 `err` 气泡；其他错误仍红色。

### 6.7 流式态按钮显隐

`sendMsg` 进入流式时给 `document.body` 加 class `streaming`，结束时移除。CSS：

```css
body.streaming .msg-actions, body.streaming #reedit-btn { display:none !important; }
```

## 7. 边界与并发

- truncate 与 chat 均经 `is_busy` 守卫：流式中 truncate 返回 409。
- 截断只动 `archived=0`；归档审计行不受影响。
- 合成 `[Earlier conversation summary]` 用户消息不渲染气泡 → 无恢复/编辑按钮。
- 重新编辑放弃（未发送）→ 不删除，原对话保留。
- 已知限制：停止后未完成的 assistant 文本不持久化（reload 后消失），与"强行停止"语义一致，本次不扩展。

## 8. 测试

`tests/test_api.py`（沿用 `client` + `bob` + `agent_id` fixture 与 fake_provider 模式）：

- `test_get_session_returns_message_ids`：一轮对话后 `get_session` 的 `messages[*].id` 存在且递增。
- `test_truncate_restores_to_user_message`：两轮对话 → 取第 1 条 user msg 的 id → `POST /truncate` → `get_session` 仅剩该 msg 之前的内容，断言消息数与角色序列。
- `test_truncate_busy_409`：构造流式未结束（或显式 busy）时 truncate 返回 409。
- `test_truncate_isolation`：他人会话 truncate 返回 404。

前端无自动化测试（与现状一致），手动验证停止/恢复/重新编辑三条交互。
