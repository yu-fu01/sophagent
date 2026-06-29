"""End-to-end API tests with a scripted fake provider (no real model calls)."""

import asyncio
import threading

from sophagent.models import AssistantTurn, StreamEvent, ToolCall

from conftest import ws_token, ws_send, ws_recv_frame, ws_response, ws_events_until  # noqa: F401

SKILL_MD = """---
name: greet
description: "How to greet users"
---

# Greet
Say hello warmly.
"""


def test_healthz(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_frontend_shell_served_with_no_cache(client):
    # 外壳文件无版本指纹：必须带 no-cache，避免重新部署后被浏览器旧缓存覆盖。
    for path in ("/", "/markdown.js", "/worklog.js"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers.get("cache-control") == "no-cache", path
    # markdown.js 以 JS MIME 返回，浏览器才肯当脚本执行
    assert "javascript" in client.get("/markdown.js").headers["content-type"]


def test_login_failures(client):
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_admin_only_user_management(client, admin, bob):
    assert client.get("/api/users", headers=bob).status_code == 403
    assert client.get("/api/users", headers=admin).status_code == 200
    # duplicate username
    resp = client.post("/api/users", json={"username": "bob", "password": "xxxxxx"}, headers=admin)
    assert resp.status_code == 409
    # cannot delete last admin
    admin_id = client.get("/api/auth/me", headers=admin).json()["id"]
    assert client.delete(f"/api/users/{admin_id}", headers=admin).status_code == 400


def test_agent_validation(client, admin):
    resp = client.post("/api/agents", json={
        "name": "x", "system_prompt": "p", "provider": "nope", "model": "m", "tools": [],
    }, headers=admin)
    assert resp.status_code == 400 and "unknown provider" in resp.text
    resp = client.post("/api/agents", json={
        "name": "x", "system_prompt": "p", "provider": "test", "model": "m", "tools": ["bogus"],
    }, headers=admin)
    assert resp.status_code == 400 and "unknown tools" in resp.text


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


def test_session_isolation(client, admin, bob, agent_id):
    # carol is in her own group, unrelated to bob (neither manages the other)
    resp = client.post("/api/users", json={"username": "carol", "password": "carolpw1"}, headers=admin)
    assert resp.status_code == 201
    carol = {"Authorization": f"Bearer {client.post('/api/auth/login', json={'username': 'carol', 'password': 'carolpw1'}).json()['token']}"}
    caid = client.post("/api/agents", json={
        "name": "c1", "system_prompt": "p", "provider": "test", "model": "m", "tools": [],
    }, headers=carol).json()["id"]
    sid = client.post("/api/sessions", json={"agent_id": caid}, headers=carol).json()["id"]
    # bob cannot see, chat into, or delete carol's session, nor list it
    assert client.get(f"/api/sessions/{sid}", headers=bob).status_code == 404
    # bob cannot resume carol's session via WS (4401)
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=sid)
        assert ws_response(ws, 1)["error"]["code"] == 4401
    assert client.delete(f"/api/sessions/{sid}", headers=bob).status_code in (403, 404)
    assert [s["id"] for s in client.get("/api/sessions", headers=bob).json()] == []


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


def test_openai_compat(client, bob, agent_id):
    models = client.get("/v1/models", headers=bob).json()
    assert models["data"][0]["id"] == "helper"
    resp = client.post("/v1/chat/completions", headers=bob, json={
        "model": "helper",
        "messages": [{"role": "user", "content": "ping"}],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "echo: ping"
    assert body["usage"]["total_tokens"] == 15
    # streaming variant
    resp = client.post("/v1/chat/completions", headers=bob, json={
        "model": "helper", "stream": True,
        "messages": [{"role": "user", "content": "ping"}],
    })
    assert "chat.completion.chunk" in resp.text and "data: [DONE]" in resp.text


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


def test_truncate_restores_to_user_message(client, bob, agent_id):
    """Truncating at a user message deletes that message and everything after it."""
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


def test_truncate_busy_returns_4009(client, bob, agent_id):
    """Cannot truncate while a turn is running (is_busy guard fires)."""
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


def test_truncate_isolation(client, admin, bob, agent_id):
    """Another user's session cannot be truncated (4401)."""
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


def test_user_deletion_cascades(client, admin, bob, agent_id):
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    bob_id = next(u["id"] for u in client.get("/api/users", headers=admin).json() if u["username"] == "bob")
    assert client.delete(f"/api/users/{bob_id}", headers=admin).status_code == 200
    # bob's token no longer works; his session is gone
    assert client.get("/api/sessions", headers=bob).status_code == 401
    assert client.get(f"/api/sessions/{sid}", headers=admin).status_code == 404


def test_patch_session_title(client, bob, agent_id):
    s = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()
    client.patch(f"/api/sessions/{s['id']}", headers=bob, json={"thinking_mode": "fast"})
    # 仅改 title 不应清掉已有 override（exclude_unset）
    patched = client.patch(f"/api/sessions/{s['id']}", headers=bob, json={"title": "Renamed"}).json()
    assert patched["title"] == "Renamed"
    assert patched["thinking_mode"] == "fast"
    detail = client.get(f"/api/sessions/{s['id']}", headers=bob).json()
    assert detail["title"] == "Renamed"
    assert detail["thinking_mode"] == "fast"


def test_list_sessions_includes_running(client, bob, agent_id):
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    row = next(r for r in client.get("/api/sessions", headers=bob).json() if r["id"] == sid)
    assert row["running"] is False
    client.app.state.manager.is_busy = lambda session_id: session_id == sid
    try:
        row = next(r for r in client.get("/api/sessions", headers=bob).json() if r["id"] == sid)
        assert row["running"] is True
        assert client.get(f"/api/sessions/{sid}", headers=bob).json()["running"] is True
    finally:
        client.app.state.manager.is_busy = lambda session_id: False


def test_queue_remove(client, bob, agent_id):
    started = threading.Event()
    release = threading.Event()

    async def blocking_chat(*, model, system, messages, tools=None,
                            temperature=None, max_tokens=None, thinking=None):
        started.set()
        await asyncio.to_thread(release.wait, 5)
        turn = AssistantTurn(content="done", stop_reason="stop", input_tokens=1, output_tokens=1)
        yield StreamEvent("text_delta", text=turn.content)
        yield StreamEvent("turn_done", turn=turn)

    client.provider.chat = blocking_chat
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)  # ready
        ws_send(ws, "session.resume", 1, session_id=sid); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=sid, content="first")
        ws_response(ws, 2)
        assert started.wait(2)
        ws_send(ws, "prompt.submit", 3, session_id=sid, content="second")
        assert ws_response(ws, 3)["result"] == {"kind": "queued", "position": 1}
        ws_send(ws, "prompt.submit", 4, session_id=sid, content="third")
        assert ws_response(ws, 4)["result"] == {"kind": "queued", "position": 2}
        ws_send(ws, "queue.remove", 5, session_id=sid, index=0)
        assert ws_response(ws, 5)["result"] == {"removed": True, "remaining": 1}
        release.set()
        events = ws_events_until(ws, "turn.settled")
    qnext = [e["payload"]["content"] for e in events if e["type"] == "queued_next"]
    assert qnext == ["third"]
