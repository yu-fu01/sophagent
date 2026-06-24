"""End-to-end API tests with a scripted fake provider (no real model calls)."""

import asyncio
import threading

from sophclaw.models import AssistantTurn, StreamEvent, ToolCall

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
    first_response = {}

    def post_first_turn():
        first_response["resp"] = client.post(
            f"/api/sessions/{sid}/chat", json={"content": "first"}, headers=bob,
        )

    thread = threading.Thread(target=post_first_turn)
    thread.start()
    assert started.wait(2)

    for position, content in enumerate(["second", "third", "fourth"], start=1):
        queued = client.post(f"/api/sessions/{sid}/chat", json={"content": content}, headers=bob)
        assert queued.status_code == 200
        assert queued.json() == {"queued": True, "position": position}
    overflow = client.post(f"/api/sessions/{sid}/chat", json={"content": "fifth"}, headers=bob)
    assert overflow.status_code == 429

    release.set()
    thread.join(5)
    assert not thread.is_alive()
    events = sse_events(first_response["resp"])
    assert {"type": "queued_next", "content": "second"} in events
    assert {"type": "queued_next", "content": "third"} in events
    assert {"type": "queued_next", "content": "fourth"} in events

    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    assert [(m["role"], m["content"]) for m in detail["messages"]] == [
        ("user", "first"),
        ("assistant", "echo: first"),
        ("user", "second"),
        ("assistant", "echo: second"),
        ("user", "third"),
        ("assistant", "echo: third"),
        ("user", "fourth"),
        ("assistant", "echo: fourth"),
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
    first_response = {}

    def post_first_turn():
        first_response["resp"] = client.post(
            f"/api/sessions/{sid}/chat", json={"content": "first"}, headers=bob,
        )

    thread = threading.Thread(target=post_first_turn)
    thread.start()
    assert started.wait(2)

    queued = client.post(f"/api/sessions/{sid}/chat", json={"content": "second"}, headers=bob)
    assert queued.status_code == 200
    assert queued.json() == {"queued": True, "position": 1}

    stopped = client.post(f"/api/sessions/{sid}/stop", headers=bob)
    assert stopped.status_code == 200
    assert stopped.json() == {"stopped": True}
    release.set()
    thread.join(5)
    assert not thread.is_alive()

    events = sse_events(first_response["resp"])
    assert {"type": "error", "message": "stopped by user"} in events
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
    assert client.post(f"/api/sessions/{sid}/chat", json={"content": "x"}, headers=bob).status_code == 404
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
    client.post(f"/api/sessions/{sid}/chat", json={"content": "question"}, headers=bob)

    resp = client.post(f"/api/sessions/{sid}/chat", json={"content": "/retry"}, headers=bob)
    events = sse_events(resp)
    assert any(e["type"] == "text_delta" and e["text"] == "new answer" for e in events)

    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    assert [(m["role"], m["content"]) for m in detail["messages"]] == [
        ("user", "question"),
        ("assistant", "new answer"),
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
    client.post(f"/api/sessions/{sid}/chat", json={"content": "first"}, headers=bob)
    client.post(f"/api/sessions/{sid}/chat", json={"content": "second"}, headers=bob)
    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
    # id of the 2nd user message (the "second" turn)
    second_user_id = detail["messages"][2]["id"]
    resp = client.post(f"/api/sessions/{sid}/truncate", json={"message_id": second_user_id}, headers=bob)
    assert resp.status_code == 200
    assert resp.json()["deleted"] >= 2  # that user msg + its assistant reply
    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "first"


def test_truncate_busy_returns_409(client, bob, agent_id):
    """Cannot truncate while a turn is running (is_busy guard fires)."""
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    client.post(f"/api/sessions/{sid}/chat", json={"content": "x"}, headers=bob)
    # simulate an in-flight turn: force the manager to report busy
    client.app.state.manager.is_busy = lambda session_id: True
    try:
        resp = client.post(f"/api/sessions/{sid}/truncate", json={"message_id": 1}, headers=bob)
        assert resp.status_code == 409
    finally:
        client.app.state.manager.is_busy = lambda session_id: False


def test_truncate_isolation(client, admin, bob, agent_id):
    """Another user's session cannot be truncated (404)."""
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    client.post(f"/api/sessions/{sid}/chat", json={"content": "mine"}, headers=bob)
    # carol: unrelated user
    client.post("/api/users", json={"username": "carol", "password": "carolpw1"}, headers=admin)
    carol = {"Authorization": f"Bearer {client.post('/api/auth/login', json={'username': 'carol', 'password': 'carolpw1'}).json()['token']}"}
    resp = client.post(f"/api/sessions/{sid}/truncate", json={"message_id": 1}, headers=carol)
    assert resp.status_code == 404


def test_user_deletion_cascades(client, admin, bob, agent_id):
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    bob_id = next(u["id"] for u in client.get("/api/users", headers=admin).json() if u["username"] == "bob")
    assert client.delete(f"/api/users/{bob_id}", headers=admin).status_code == 200
    # bob's token no longer works; his session is gone
    assert client.get("/api/sessions", headers=bob).status_code == 401
    assert client.get(f"/api/sessions/{sid}", headers=admin).status_code == 404
