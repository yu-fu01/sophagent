# 移除 SSE（只保留 WS）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 删除 REST 对话流（`/chat` SSE）与控制类 HTTP 路由（`/stop`、`/truncate`），sophagent 对话通路只走 WebSocket JSON-RPC 网关。

**架构：** 前端与测试从 `/chat` SSE 迁到 WS（`prompt.submit`/`session.interrupt`/新增 `session.truncate`/`message.edit`）。后端删三个路由 + `StreamingResponse`。WS 网关与共享 turn 核心 `run_turns` 不变。

**技术栈：** FastAPI、Starlette TestClient（`websocket_connect`）、pytest。

**参考规格：** `docs/superpowers/specs/2026-06-24-sse-removal-and-im-gateway-design.md` §4。

---

## 文件结构

- 修改：`tests/conftest.py` — 新增 WS 测试 helper（`ws_token`/`ws_send`/`ws_recv_frame`/`ws_response`/`ws_events_until`），保留 `sse_events` 直到最后删除。
- 修改：`tests/test_api.py` — 11 个走 `/chat` SSE 的测试迁 WS。
- 修改：`tests/test_overrides_usage.py` — 5 个走 `/chat` SSE 的测试迁 WS。
- 修改：`sophagent/gateway/methods.py` — 新增 `session.truncate` 方法。
- 修改：`sophagent/gateway/protocol.py` — 无（错误码已有 `ERR_BUSY`/`ERR_FORBIDDEN`）。
- 修改：`sophagent/api/session_routes.py` — 删 `/chat`、`/stop`、`/truncate` 路由与 `_start_sse_turn`、`StreamingResponse`。
- 修改：`web/index.html` — 重新编辑走 `message.edit`、恢复走 `session.truncate`。

---

## 任务 1：conftest 加 WS helper

**文件：** 修改 `tests/conftest.py`（在 `sse_events` 之后追加）

- [ ] **步骤 1：追加 WS helper**

在 `tests/conftest.py` 末尾（`sse_events` 函数之后）追加：

```python
# ---- WebSocket test helpers (gateway /ws, JSON-RPC) -----------------------

def ws_token(auth: dict) -> str:
    return auth["Authorization"].split(" ", 1)[1]

def ws_send(ws, method: str, req_id, **params) -> None:
    ws.send_text(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))

def ws_recv_frame(ws) -> dict:
    return json.loads(ws.receive_text())

def ws_response(ws, req_id) -> dict:
    """读帧直到匹配 req_id 的 response/error。"""
    while True:
        f = ws_recv_frame(ws)
        if f.get("id") == req_id:
            return f

def ws_events_until(ws, wire_type: str) -> list[dict]:
    """收集 event 帧（params）直到 type==wire_type（含）或 error。"""
    seen = []
    while True:
        f = ws_recv_frame(ws)
        if f.get("method") == "event":
            p = f["params"]
            seen.append(p)
            if p["type"] == wire_type or p["type"] == "error":
                return seen
```

- [ ] **步骤 2：确认导入**

`conftest.py` 顶部已有 `import json`（`sse_events` 用）。无需新导入。

- [ ] **步骤 3：跑现有套件确认无破坏**

运行：`uv run --extra dev pytest tests/test_gateway_ws.py -q`
预期：PASS（helper 未被引用，不影响）。

- [ ] **步骤 4：Commit**

```bash
git add tests/conftest.py
git commit -m "test: 加 WebSocket 测试 helper（ws_send/ws_response/ws_events_until）"
```

---

## 任务 2：新增 WS 方法 `session.truncate`

**文件：**
- 修改：`sophagent/gateway/methods.py`（新增 `m_session_truncate`，注册进 `METHODS`）
- 测试：`tests/test_gateway_ws.py`（追加用例）

- [ ] **步骤 1：写失败测试**

在 `tests/test_gateway_ws.py` 末尾追加：

```python
def test_session_truncate_via_ws(client, bob, agent_id):
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    # 两条 turn 产生 4 条消息
    for c in ("first", "second"):
        with client.websocket_connect(_ws_url(client, bob["Authorization"].split(" ", 1)[1])) as ws:
            _recv(ws)
            _send(ws, "session.resume", 1, session_id=sid); _recv_response(ws, 1)
            _send(ws, "prompt.submit", 2, session_id=sid, content=c)
            _recv_response(ws, 2)
            _collect_events_until(ws, "turn.settled")
    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    second_user_id = detail["messages"][2]["id"]

    with client.websocket_connect(_ws_url(client, bob["Authorization"].split(" ", 1)[1])) as ws:
        _recv(ws)
        _send(ws, "session.resume", 1, session_id=sid); _recv_response(ws, 1)
        _send(ws, "session.truncate", 3, session_id=sid, message_id=second_user_id)
        resp = _recv_response(ws, 3)
        assert resp["result"]["deleted"] >= 2
    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "first"


def test_session_truncate_busy_returns_4009(client, bob, agent_id):
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    with client.websocket_connect(_ws_url(client, bob["Authorization"].split(" ", 1)[1])) as ws:
        _recv(ws)
        _send(ws, "session.resume", 1, session_id=sid); _recv_response(ws, 1)
        client.app.state.manager.is_busy = lambda s: True
        try:
            _send(ws, "session.truncate", 2, session_id=sid, message_id=1)
            assert _recv_response(ws, 2)["error"]["code"] == 4009
        finally:
            client.app.state.manager.is_busy = lambda s: False


def test_session_truncate_other_user_forbidden(client, bob, agent_id, admin):
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    admin_tok = admin["Authorization"].split(" ", 1)[1]
    with client.websocket_connect(_ws_url(client, admin_tok)) as ws:
        _recv(ws)
        _send(ws, "session.truncate", 1, session_id=sid, message_id=1)
        assert _recv_response(ws, 1)["error"]["code"] == 4401
```

- [ ] **步骤 2：运行验证失败**

运行：`uv run --extra dev pytest tests/test_gateway_ws.py::test_session_truncate_via_ws -q`
预期：FAIL（`method not found: session.truncate`，-32601）。

- [ ] **步骤 3：实现 `m_session_truncate`**

在 `sophagent/gateway/methods.py` 的 `m_queue_submit` 之前插入：

```python
async def m_session_truncate(params: dict[str, Any], ctx: GatewayContext) -> dict[str, Any]:
    """Restore: drop the message with ``message_id`` and everything after it.
    No turn is started (unlike ``message.edit``). Refused while a turn runs."""
    session_id = params["session_id"]
    message_id = params["message_id"]
    if ctx.manager.is_busy(session_id):
        raise GatewayError(protocol.ERR_BUSY, "session is running a turn")
    await _require_session(ctx, session_id)   # 归属校验（4401）
    deleted = await ctx.db.truncate_from(session_id, message_id)
    await ctx.db.touch_session(session_id)
    return {"deleted": deleted}
```

在 `METHODS` 字典追加一行：

```python
    "session.truncate": m_session_truncate,
```

- [ ] **步骤 4：运行验证通过**

运行：`uv run --extra dev pytest tests/test_gateway_ws.py -q`
预期：PASS（含 3 个新用例）。

- [ ] **步骤 5：Commit**

```bash
git add sophagent/gateway/methods.py tests/test_gateway_ws.py
git commit -m "feat(gateway): 新增 session.truncate WS 方法（恢复用，仅截断不开轮）"
```

---

## 任务 3：迁移 test_api.py 简单 chat 测试

**文件：** 修改 `tests/test_api.py`

把 `from conftest import sse_events` 改为 `from conftest import ws_token, ws_send, ws_recv_frame, ws_response, ws_events_until`。

- [ ] **步骤 1：改导入行**

`tests/test_api.py:8` 改为：

```python
from conftest import ws_token, ws_send, ws_recv_frame, ws_response, ws_events_until  # noqa: F401
```

- [ ] **步骤 2：重写 `test_chat_flow_and_persistence`**

替换 `tests/test_api.py` 中该函数整体为：

```python
def test_chat_flow_and_persistence(client, bob, agent_id):
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)  # ready
        ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=sid, content="hello there")
        assert ws_response(ws, 2)["result"]["kind"] == "start"
        events = ws_events_until(ws, "turn.settled")
    assert any(e["type"] == "message.delta" and "hello there" in e["payload"]["text"] for e in events)
    assert any(e["type"] == "message.complete" for e in events)
    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["title"] == "hello there"
    # 第二轮看到历史
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=sid, content="again")
        ws_response(ws, 2)
        ws_events_until(ws, "turn.settled")
    assert len(client.get(f"/api/sessions/{sid}", headers=bob).json()["messages"]) == 4
```

- [ ] **步骤 3：重写 `test_agent_tool_call_via_chat`**

```python
def test_agent_tool_call_via_chat(client, bob, agent_id):
    client.provider.script = [
        AssistantTurn(tool_calls=[ToolCall(id="c1", name="write_file",
                                           arguments={"path": "note.txt", "content": "saved"})]),
        AssistantTurn(content="done", stop_reason="stop"),
    ]
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=sid, content="save a note")
        ws_response(ws, 2)
        events = ws_events_until(ws, "message.complete")
    types = [e["type"] for e in events]
    assert types == ["turn.usage", "tool.call", "tool.result", "message.delta", "turn.usage", "message.complete"]
```

- [ ] **步骤 4：重写 `test_reasoning_streamed_and_persisted_via_chat`**

```python
def test_reasoning_streamed_and_persisted_via_chat(client, bob, agent_id):
    client.provider.script = [
        AssistantTurn(content="the answer", reasoning="let me think", stop_reason="stop"),
    ]
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=sid, content="ponder this")
        ws_response(ws, 2)
        events = ws_events_until(ws, "turn.settled")
    assert any(e["type"] == "reasoning.delta" and e["payload"]["text"] == "let me think" for e in events)
    assert any(e["type"] == "message.delta" and e["payload"]["text"] == "the answer" for e in events)
    assert not any(e["type"] == "message.delta" and "let me think" in e["payload"]["text"] for e in events)
    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    asst = next(m for m in detail["messages"] if m["role"] == "assistant")
    assert asst["reasoning"] == "let me think"
```

- [ ] **步骤 5：重写 `test_skill_self_evolution_via_chat`**

```python
def test_skill_self_evolution_via_chat(client, admin, bob, agent_id):
    client.provider.script = [
        AssistantTurn(tool_calls=[ToolCall(id="c1", name="skill_manage",
                                           arguments={"action": "create", "name": "greet", "content": SKILL_MD})]),
        AssistantTurn(content="skill created", stop_reason="stop"),
    ]
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=sid, content="learn to greet")
        ws_response(ws, 2)
        events = ws_events_until(ws, "turn.settled")
    assert any(e["type"] == "tool.result" and '"ok": true' in e["payload"]["preview"] for e in events)
    names = [s["name"] for s in client.get("/api/skills", headers=bob).json()]
    assert "greet" in names
    assert "warmly" in client.get("/api/skills/greet", headers=bob).json()["content"]
    assert client.put("/api/skills/greet", json={"content": SKILL_MD}, headers=bob).status_code == 403
    assert client.delete("/api/skills/greet", headers=admin).status_code == 200
```

- [ ] **步骤 6：重写 `test_memory_tool_and_injection`**

```python
def test_memory_tool_and_injection(client, bob, agent_id):
    client.provider.script = [
        AssistantTurn(tool_calls=[ToolCall(id="c1", name="memory",
                                           arguments={"action": "add", "content": "bob likes tea"})]),
        AssistantTurn(content="noted", stop_reason="stop"),
    ]
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=sid, content="remember I like tea")
        ws_response(ws, 2)
        ws_events_until(ws, "turn.settled")
    sid2 = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=sid2); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=sid2, content="what do I like?")
        ws_response(ws, 2)
        events = ws_events_until(ws, "turn.settled")
    assert any(e["type"] == "message.complete" for e in events)
```

- [ ] **步骤 7：重写 `test_get_session_returns_message_ids`**

```python
def test_get_session_returns_message_ids(client, bob, agent_id):
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    for c in ("first", "second"):
        with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
            ws_recv_frame(ws)
            ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
            ws_send(ws, "prompt.submit", 2, session_id=sid, content=c)
            ws_response(ws, 2)
            ws_events_until(ws, "turn.settled")
    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    ids = [m["id"] for m in detail["messages"]]
    assert len(ids) == 4
    assert ids == sorted(ids)
    assert all(isinstance(i, int) for i in ids)
```

- [ ] **步骤 8：运行验证**

运行：`uv run --extra dev pytest tests/test_api.py -q -k "chat_flow or tool_call or reasoning or skill_self or memory or get_session"`
预期：PASS（6 个迁移用例）。

- [ ] **步骤 9：Commit**

```bash
git add tests/test_api.py
git commit -m "test: 简单 chat 用例迁移到 WS（chat_flow/tool/reasoning/skill/memory/ids）"
```

---

## 任务 4：迁移 queue 测试（去线程）

**文件：** 修改 `tests/test_api.py`

SSE 版用线程是因为 `client.post(/chat)` 阻塞到流结束；WS `prompt.submit` 立即返回 ack，**无需线程**。

- [ ] **步骤 1：重写 `test_queued_chat_turns_persist_in_order_without_duplicates`**

替换该函数整体为：

```python
def test_queued_chat_turns_persist_in_order_without_duplicates(client, bob, agent_id):
    started = threading.Event()
    release = threading.Event()
    provider_calls = []

    async def blocking_chat(*, model, system, messages, tools=None,
                            temperature=None, max_tokens=None, thinking=None):
        provider_calls.append([m.content for m in messages])
        if len(provider_calls) == 1:
            started.set()
            await asyncio.to_thread(release.wait, 5)
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        turn = AssistantTurn(content=f"echo: {last_user}", stop_reason="stop",
                             input_tokens=10, output_tokens=5)
        yield StreamEvent("text_delta", text=turn.content)
        yield StreamEvent("turn_done", turn=turn)

    client.provider.chat = blocking_chat
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]

    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)  # ready
        ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
        # 首轮（provider 首次调用阻塞）
        ws_send(ws, "prompt.submit", 2, session_id=sid, content="first")
        assert ws_response(ws, 2)["result"]["kind"] == "start"
        assert started.wait(2)
        # 排队三条
        for pos, content in enumerate(["second", "third", "fourth"], start=1):
            ws_send(ws, "prompt.submit", 10 + pos, session_id=sid, content=content)
            r = ws_response(ws, 10 + pos)
            assert r["result"] == {"kind": "queued", "position": pos}, r
        # 溢出 → 4009
        ws_send(ws, "prompt.submit", 99, session_id=sid, content="fifth")
        assert ws_response(ws, 99)["error"]["code"] == 4009
        release.set()
        events = ws_events_until(ws, "turn.settled")

    qnext = [e["payload"]["content"] for e in events if e["type"] == "queued_next"]
    assert qnext == ["second", "third", "fourth"]
    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    assert [(m["role"], m["content"]) for m in detail["messages"]] == [
        ("user", "first"), ("assistant", "echo: first"),
        ("user", "second"), ("assistant", "echo: second"),
        ("user", "third"), ("assistant", "echo: third"),
        ("user", "fourth"), ("assistant", "echo: fourth"),
    ]
    assert provider_calls == [
        ["first"],
        ["first", "echo: first", "second"],
        ["first", "echo: first", "second", "echo: second", "third"],
        ["first", "echo: first", "second", "echo: second", "third", "echo: third", "fourth"],
    ]
```

- [ ] **步骤 2：运行验证**

运行：`uv run --extra dev pytest tests/test_api.py::test_queued_chat_turns_persist_in_order_without_duplicates -q`
预期：PASS。

- [ ] **步骤 3：Commit**

```bash
git add tests/test_api.py
git commit -m "test: queue 用例迁移到 WS（去线程，单连接顺序 submit/queue/overflow）"
```

---

## 任务 5：迁移 stop 测试（去线程）

**文件：** 修改 `tests/test_api.py`

- [ ] **步骤 1：重写 `test_stop_during_busy_turn_closes_stream_and_clears_queue`**

```python
def test_stop_during_busy_turn_closes_stream_and_clears_queue(client, bob, agent_id):
    started = threading.Event()
    release = threading.Event()

    async def blocking_chat(*, model, system, messages, tools=None,
                            temperature=None, max_tokens=None, thinking=None):
        started.set()
        await asyncio.to_thread(release.wait, 5)
        turn = AssistantTurn(content="too late", stop_reason="stop",
                             input_tokens=10, output_tokens=5)
        yield StreamEvent("text_delta", text=turn.content)
        yield StreamEvent("turn_done", turn=turn)

    client.provider.chat = blocking_chat
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]

    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=sid, content="first")
        ws_response(ws, 2)
        assert started.wait(2)
        ws_send(ws, "prompt.submit", 3, session_id=sid, content="second")
        assert ws_response(ws, 3)["result"] == {"kind": "queued", "position": 1}
        ws_send(ws, "session.interrupt", 4, session_id=sid)
        assert ws_response(ws, 4)["result"]["stopped"] is True
        events = ws_events_until(ws, "turn.settled")
    release.set()  # 放行被取消的 provider 线程
    assert any(e["type"] == "error" and e["payload"]["message"] == "stopped by user" for e in events)
    assert client.app.state.manager.pending_count(sid) == 0
```

- [ ] **步骤 2：运行验证**

运行：`uv run --extra dev pytest tests/test_api.py::test_stop_during_busy_turn_closes_stream_and_clears_queue -q`
预期：PASS。

- [ ] **步骤 3：Commit**

```bash
git add tests/test_api.py
git commit -m "test: stop 用例迁移到 WS（session.interrupt，去线程）"
```

---

## 任务 6：迁移 retry 测试

**文件：** 修改 `tests/test_api.py`

- [ ] **步骤 1：重写 `test_retry_replaces_last_assistant_without_duplicating_user`**

```python
def test_retry_replaces_last_assistant_without_duplicating_user(client, bob, agent_id):
    provider_messages = []

    async def scripted_chat(*, model, system, messages, tools=None,
                            temperature=None, max_tokens=None, thinking=None):
        provider_messages.append([(m.role, m.content) for m in messages])
        turn = client.provider.script.pop(0)
        if turn.content:
            yield StreamEvent("text_delta", text=turn.content)
        yield StreamEvent("turn_done", turn=turn)

    client.provider.chat = scripted_chat
    client.provider.script = [
        AssistantTurn(content="old answer", stop_reason="stop"),
        AssistantTurn(content="new answer", stop_reason="stop"),
    ]
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=sid, content="question")
        ws_response(ws, 2)
        ws_events_until(ws, "turn.settled")
        # /retry → 服务端截断后开轮（ack kind=retry）
        ws_send(ws, "prompt.submit", 3, session_id=sid, content="/retry")
        assert ws_response(ws, 3)["result"]["kind"] == "retry"
        events = ws_events_until(ws, "turn.settled")
    assert any(e["type"] == "message.delta" and e["payload"]["text"] == "new answer" for e in events)
    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    assert [(m["role"], m["content"]) for m in detail["messages"]] == [
        ("user", "question"), ("assistant", "new answer"),
    ]
    assert provider_messages == [
        [("user", "question")],
        [("user", "question")],
    ]
```

- [ ] **步骤 2：运行验证**

运行：`uv run --extra dev pytest tests/test_api.py::test_retry_replaces_last_assistant_without_duplicating_user -q`
预期：PASS。

- [ ] **步骤 3：Commit**

```bash
git add tests/test_api.py
git commit -m "test: retry 用例迁移到 WS（prompt.submit /retry → ack kind=retry）"
```

---

## 任务 7：迁移 truncate + isolation 测试

**文件：** 修改 `tests/test_api.py`

`/truncate` HTTP 即将删除；改用 WS `session.truncate`（任务 2 已加）。

- [ ] **步骤 1：重写 `test_truncate_restores_to_user_message`**

```python
def test_truncate_restores_to_user_message(client, bob, agent_id):
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    for c in ("first", "second"):
        with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
            ws_recv_frame(ws)
            ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
            ws_send(ws, "prompt.submit", 2, session_id=sid, content=c)
            ws_response(ws, 2)
            ws_events_until(ws, "turn.settled")
    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
    second_user_id = detail["messages"][2]["id"]
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
        ws_send(ws, "session.truncate", 2, session_id=sid, message_id=second_user_id)
        assert ws_response(ws, 2)["result"]["deleted"] >= 2
    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "first"
```

- [ ] **步骤 2：重写 `test_truncate_busy_returns_409` → 4009**

替换整函数为：

```python
def test_truncate_busy_returns_4009(client, bob, agent_id):
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)  # ready
        ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
        client.app.state.manager.is_busy = lambda session_id: True
        try:
            ws_send(ws, "session.truncate", 2, session_id=sid, message_id=1)
            assert ws_response(ws, 2)["error"]["code"] == 4009
        finally:
            client.app.state.manager.is_busy = lambda session_id: False
```

- [ ] **步骤 3：重写 `test_truncate_isolation`**

```python
def test_truncate_isolation(client, admin, bob, agent_id):
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=sid, content="mine")
        ws_response(ws, 2)
        ws_events_until(ws, "turn.settled")
    client.post("/api/users", json={"username": "carol", "password": "carolpw1"}, headers=admin)
    carol = {"Authorization": f"Bearer {client.post('/api/auth/login', json={'username': 'carol', 'password': 'carolpw1'}).json()['token']}"}
    with client.websocket_connect(f"/ws?token={ws_token(carol)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.truncate", 1, session_id=sid, message_id=1)
        assert ws_response(ws, 1)["error"]["code"] == 4401
```

- [ ] **步骤 4：重写 `test_session_isolation` 中的 `/chat` 404 断言**

该函数第 198 行 `assert client.post(f"/api/sessions/{sid}/chat", json={"content": "x"}, headers=bob).status_code == 404` 删除 `/chat` 后失效。把该行替换为 WS 归属校验：

```python
    # bob 不能在 carol 的 session 上 resume（4401）
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=sid)
        assert ws_response(ws, 1)["error"]["code"] == 4401
```

（保留该函数其余行：get 404、delete 403/404、list 为空。）

- [ ] **步骤 5：运行验证**

运行：`uv run --extra dev pytest tests/test_api.py -q -k "truncate or isolation"`
预期：PASS。

- [ ] **步骤 6：Commit**

```bash
git add tests/test_api.py
git commit -m "test: truncate/isolation 用例迁移到 WS（session.truncate，4009/4401）"
```

---

## 任务 8：迁移 test_overrides_usage.py

**文件：** 修改 `tests/test_overrides_usage.py`

- [ ] **步骤 1：加导入**

文件顶部追加：

```python
from conftest import ws_token, ws_send, ws_recv_frame, ws_response, ws_events_until
```

- [ ] **步骤 2：重写 `test_session_override_thinking_passed_to_provider`**

```python
def test_session_override_thinking_passed_to_provider(client, bob, agent_id):
    s = client.post("/api/sessions", headers=bob, json={"agent_id": agent_id}).json()
    client.patch(f"/api/sessions/{s['id']}", headers=bob,
                 json={"thinking_mode": "thinking", "override_model": "big-model"})
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=s["id"]); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=s["id"], content="hi")
        ws_response(ws, 2)
        ws_events_until(ws, "turn.settled")
    assert client.provider.last_kwargs["thinking"] == "thinking"
    assert client.provider.last_kwargs["model"] == "big-model"
```

- [ ] **步骤 3：重写 `test_no_override_falls_back_to_agent`**

```python
def test_no_override_falls_back_to_agent(client, bob, agent_id):
    s = client.post("/api/sessions", headers=bob, json={"agent_id": agent_id}).json()
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=s["id"]); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=s["id"], content="hi")
        ws_response(ws, 2)
        ws_events_until(ws, "turn.settled")
    assert client.provider.last_kwargs["model"] == "test-model"
    assert client.provider.last_kwargs["thinking"] is None
```

- [ ] **步骤 4：重写 `test_thinking_default_folds_to_none`**

```python
def test_thinking_default_folds_to_none(client, bob, agent_id):
    s = client.post("/api/sessions", headers=bob, json={"agent_id": agent_id}).json()
    client.patch(f"/api/sessions/{s['id']}", headers=bob, json={"thinking_mode": "default"})
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=s["id"]); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=s["id"], content="hi")
        ws_response(ws, 2)
        ws_events_until(ws, "turn.settled")
    assert client.provider.last_kwargs["thinking"] is None
```

- [ ] **步骤 5：重写 `test_turn_usage_and_done_usage`**

```python
def test_turn_usage_and_done_usage(client, bob, agent_id):
    from sophagent.models import AssistantTurn
    client.provider.script = [AssistantTurn(content="hi", stop_reason="stop",
        input_tokens=100, output_tokens=20, cache_read_tokens=40)]
    s = client.post("/api/sessions", headers=bob, json={"agent_id": agent_id}).json()
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=s["id"]); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=s["id"], content="yo")
        ws_response(ws, 2)
        events = ws_events_until(ws, "turn.settled")
    turn_usage = next(e["payload"] for e in events if e["type"] == "turn.usage")
    assert turn_usage["cache_read_tokens"] == 40 and turn_usage["cache_hit"] == 29
    complete = next(e["payload"] for e in events if e["type"] == "message.complete")
    assert complete["usage"]["input_tokens"] == 100
    assert "context_length" in complete and "context_limit" in complete
```

- [ ] **步骤 6：重写 `test_unknown_override_provider_falls_back`**

```python
def test_unknown_override_provider_falls_back(client, bob, agent_id):
    s = client.post("/api/sessions", headers=bob, json={"agent_id": agent_id}).json()
    client.patch(f"/api/sessions/{s['id']}", headers=bob,
                 json={"override_provider": "ghost-provider"})
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=s["id"]); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=s["id"], content="hi")
        ws_response(ws, 2)
        events = ws_events_until(ws, "turn.settled")
    assert any(e["type"] == "message.complete" for e in events)
    assert not any(e["type"] == "error" for e in events)
```

- [ ] **步骤 7：运行验证**

运行：`uv run --extra dev pytest tests/test_overrides_usage.py -q`
预期：PASS（5 个迁移用例 + `test_cache_hit_percent` 不动）。

- [ ] **步骤 8：Commit**

```bash
git add tests/test_overrides_usage.py
git commit -m "test: overrides 用例迁移到 WS（thinking/usage/fallback）"
```

---

## 任务 9：删 SSE 路由 + 前端迁移

**文件：**
- 修改：`sophagent/api/session_routes.py` — 删 `/chat`、`/stop`、`/truncate`、`_start_sse_turn`、`StreamingResponse` import。
- 修改：`web/index.html` — 重新编辑走 `message.edit`、恢复走 `session.truncate`。
- 修改：`tests/conftest.py` — 删 `sse_events` helper（已无人用）。

- [ ] **步骤 1：删后端路由**

`sophagent/api/session_routes.py`：

1. 删除 `import asyncio`（仍用于？检查：删 `_start_sse_turn` 后 `asyncio.Queue`/`create_task` 不再用 → 删 `import asyncio`）。`import json` 仍用（其它路由？检查后保留/删）。实际仅 `chat`/`_start_sse_turn` 用 `asyncio`/`json`，删后这两个 import 可移除。保留 `uuid`（create_session 用）。
2. 删除 `from fastapi.responses import JSONResponse, StreamingResponse` 中的 `StreamingResponse`，改 `from fastapi.responses import JSONResponse`。检查 `JSONResponse` 是否仍用——`chat` 的 slash 分支已删，其它路由是否用 JSONResponse？搜索：仅 chat 用。删后若无人用则改回不导入。保守：保留 `JSONResponse` import（truncate 等返回 dict，FastAPI 自动转 JSON；JSONResponse 可能无引用→移除）。
3. 删除 `from ..agent.turn import pre_submit, run_turns`（删 chat 后无人用）。
4. 删除函数：`chat`、`_start_sse_turn`、`stop_session`、`truncate_session`。
5. 保留：`list_sessions`、`create_session`、`get_session`、`patch_session`、`delete_session`（delete 内调 `manager.stop`/`clear_queue` 仍合理，保留）。

精确改法：打开 `sophagent/api/session_routes.py`，删除从 `@router.post("/{session_id}/stop")` 到文件末尾 `_start_sse_turn` 结束的所有内容（即 `stop_session`、`truncate_session`、`chat`、`_start_sse_turn` 四个函数）。保留 `delete_session`（在 `truncate_session` 之前）。

修改后的 import 块：

```python
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..auth import require_user
from ..models import SessionCreate, SessionOverridePatch
from ..perms import can_access_group, can_manage_group

router = APIRouter()
```

（确认 `JSONResponse` 仍被 `delete_session` 等用？`delete_session` 返回 `{"ok": True}` dict。无 JSONResponse 引用则改为不导入。运行时 import 未用不报错，保守保留 `JSONResponse` 若无引用则删。以 `grep JSONResponse sophagent/api/session_routes.py` 确认：若仅 import 行，删之。）

- [ ] **步骤 2：前端重新编辑走 `message.edit`**

`web/index.html` 中 `sendMsg` 的 `editing` 块（约 660-667 行）当前：

```javascript
  if (editing) {
    startStreamingUI();
    try {
      await api(`/api/sessions/${currentSid}/truncate`, {method: "POST", body: {message_id: editing.mid}});
    } catch (e) {
      stopStreamingUI(); alert("重新编辑失败: " + e.message); return;
    }
    editing = null;
    await openSession(currentSid);
    startStreamingUI();
  }
```

替换为（用 WS `message.edit`：截断+开轮一条龙，不再 openSession）：

```javascript
  if (editing) {
    const mid = editing.mid; editing = null;
    $("input").value = "";
    startStreamingUI();
    addBubble("user", content);
    turn = freshTurn(content, {shown: true});
    try {
      const res = await wsSend("message.edit", {session_id: currentSid, message_id: mid, content});
      // res: {deleted, started} — 流式事件陆续到达，于 turn.settled 收尾
      if (!res || !res.started) { stopStreamingUI(); turn = null; await openSession(currentSid); }
    } catch (e) {
      addBubble("err", e.message); stopStreamingUI(); turn = null; await openSession(currentSid);
    }
    return;
  }
```

- [ ] **步骤 3：前端恢复走 `session.truncate`**

`web/index.html` 中 `restoreFrom`（约 916-924 行）当前用 HTTP `/truncate`。替换为：

```javascript
async function restoreFrom(mid) {
  if (!currentSid || streaming) return;
  if (!confirm("此次对话之后的内容都将删除，确定重置？")) return;
  try {
    await wsSend("session.truncate", {session_id: currentSid, message_id: mid});
  } catch (e) { alert("恢复失败: " + e.message); return; }
  await openSession(currentSid);
}
```

- [ ] **步骤 4：删 `sse_events` helper**

`tests/conftest.py` 删除 `def sse_events(resp): ...` 函数（及末行注释）。确认无引用：`grep -rn sse_events tests/` 应为空。

- [ ] **步骤 5：删 `expectJson`（若仍在）**

`web/index.html` 中 `expectJson` 已在上一里程碑删除，跳过（`grep expectJson web/index.html` 应为空）。

- [ ] **步骤 6：确认无 SSE 残留**

运行：
```bash
grep -rn "StreamingResponse\|text/event-stream\|/chat\b\|sse_events" sophagent/ tests/ web/ | grep -v ".pyc"
```
预期：无输出（或仅注释/无关）。

- [ ] **步骤 7：跑全量**

运行：`uv run --extra dev pytest -q`
预期：PASS（全部用例迁完，无 /chat/stop/truncate 路由）。

- [ ] **步骤 8：确认删掉的路由 404**

运行：
```bash
uv run --extra dev python -c "
from fastapi.testclient import TestClient
from sophagent.main import create_app
import tempfile, os
os.environ['SOPHAGENT_DATA_DIR']=tempfile.mkdtemp()+'/data'
from sophagent.main import create_app
with TestClient(create_app()) as c:
    tok=c.post('/api/auth/login',json={'username':'admin','password':'adminpw'}).json()['token']
    h={'Authorization':f'Bearer {tok}'}
    sid=c.post('/api/sessions',json={'agent_id':1},headers=h).json().get('id','x')
    print('chat', c.post(f'/api/sessions/x/chat',headers=h).status_code)
    print('stop', c.post(f'/api/sessions/x/stop',headers=h).status_code)
    print('truncate', c.post(f'/api/sessions/x/truncate',headers=h,json={'message_id':1}).status_code)
"
```
预期：三者均 404（路由不存在）。

- [ ] **步骤 9：Commit**

```bash
git add sophagent/api/session_routes.py web/index.html tests/conftest.py
git commit -m "feat: 完全移除 SSE（删 /chat·/stop·/truncate，前端重编辑走 message.edit、恢复走 session.truncate）"
```

---

## 任务 10：端到端冒烟 + docker 重建

**文件：** 无（验证）

- [ ] **步骤 1：全量测试**

运行：`uv run --extra dev pytest -q`
预期：全绿。

- [ ] **步骤 2：docker 重建冒烟**

```bash
docker compose -p sophagent-sse-im up --build -d
for i in $(seq 1 25); do curl -sf http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break; sleep 1; done
```

WS 冒烟（参考 `tests/test_gateway_ws.py` 流程）：登录 → 建会话 → `/ws` resume + prompt.submit → 收到 `message.delta`…`turn.settled`。

- [ ] **步骤 3：Commit（若有 dockerfile/compose 改动，否则跳过）**

无代码改动则跳过。

---

## 自检

**规格覆盖度（spec §4）：**
- §4.1 删 /chat·/stop·/truncate + StreamingResponse → 任务 9 ✓
- §4.2 前端重编辑→message.edit、恢复→session.truncate → 任务 9 步骤 2/3 ✓
- §4.3 新增 session.truncate WS 方法 → 任务 2 ✓
- §4.4 测试迁移（test_api + test_overrides） → 任务 3-8 ✓

**占位符扫描：** 任务 9 步骤 1 的 import 处理有"以 grep 确认"的条件分支——这是可执行的精确指令（grep + 按结果删），非占位符。无 TODO/待定。

**类型一致性：** `ws_send/ws_response/ws_events_until` 在任务 1 定义，任务 3-8 一致使用。`m_session_truncate` 签名（params, ctx）→ dict 与其它 `m_*` 一致，注册名 `"session.truncate"` 与前端/测试调用一致。`message.edit` ack `{deleted, started}` 与前端任务 9 步骤 2 的 `res.started` 一致（任务 2 之外的 `m_message_edit` 已有该返回，未改）。

**遗漏：** spec §4.4 提到 `test_truncate_busy_returns_409` → 迁为 4009（任务 7 步骤 2 ✓）。`test_session_isolation` 的 /chat 404 断言（任务 7 步骤 4 ✓）。
