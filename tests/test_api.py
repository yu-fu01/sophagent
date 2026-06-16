"""End-to-end API tests with a scripted fake provider (no real model calls)."""

from sophclaw.models import AssistantTurn, ToolCall

from conftest import sse_events  # noqa: F401  (shared harness helper)

SKILL_MD = """---
name: greet
description: "How to greet users"
---

# Greet
Say hello warmly.
"""


def test_healthz(client):
    assert client.get("/healthz").json()["status"] == "ok"


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
    session = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()
    sid = session["id"]
    resp = client.post(f"/api/sessions/{sid}/chat", json={"content": "hello there"}, headers=bob)
    assert resp.status_code == 200
    events = sse_events(resp)
    assert any(e["type"] == "text_delta" and "hello there" in e["text"] for e in events)
    assert events[-1]["type"] == "done"
    # history persisted
    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]
    assert detail["title"] == "hello there"
    # second turn sees prior history
    client.post(f"/api/sessions/{sid}/chat", json={"content": "again"}, headers=bob)
    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    assert len(detail["messages"]) == 4


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
    resp = client.post(f"/api/sessions/{sid}/chat", json={"content": "save a note"}, headers=bob)
    types = [e["type"] for e in sse_events(resp)]
    assert types == ["tool_call", "tool_result", "text_delta", "done"]


def test_skill_self_evolution_via_chat(client, admin, bob, agent_id):
    client.provider.script = [
        AssistantTurn(tool_calls=[ToolCall(id="c1", name="skill_manage",
                                           arguments={"action": "create", "name": "greet", "content": SKILL_MD})]),
        AssistantTurn(content="skill created", stop_reason="stop"),
    ]
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    resp = client.post(f"/api/sessions/{sid}/chat", json={"content": "learn to greet"}, headers=bob)
    assert any(e["type"] == "tool_result" and '"ok": true' in e["preview"] for e in sse_events(resp))
    # skill is now visible via the API to any user, and admin can delete it
    assert client.get("/api/skills", headers=bob).json()[0]["name"] == "greet"
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
    client.post(f"/api/sessions/{sid}/chat", json={"content": "remember I like tea"}, headers=bob)
    # memory should be injected into the next turn's run (frozen snapshot)
    sid2 = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    resp = client.post(f"/api/sessions/{sid2}/chat", json={"content": "what do I like?"}, headers=bob)
    assert sse_events(resp)[-1]["type"] == "done"


def test_user_deletion_cascades(client, admin, bob, agent_id):
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    bob_id = next(u["id"] for u in client.get("/api/users", headers=admin).json() if u["username"] == "bob")
    assert client.delete(f"/api/users/{bob_id}", headers=admin).status_code == 200
    # bob's token no longer works; his session is gone
    assert client.get("/api/sessions", headers=bob).status_code == 401
    assert client.get(f"/api/sessions/{sid}", headers=admin).status_code == 404
