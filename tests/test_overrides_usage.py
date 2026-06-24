"""会话覆盖字段贯通 runner 的集成测试。"""

from sophclaw.usage import cache_hit_percent
from conftest import ws_token, ws_send, ws_recv_frame, ws_response, ws_events_until


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


def test_no_override_falls_back_to_agent(client, bob, agent_id):
    s = client.post("/api/sessions", headers=bob, json={"agent_id": agent_id}).json()
    # 不设任何覆盖，直接 chat
    with client.websocket_connect(f"/ws?token={ws_token(bob)}") as ws:
        ws_recv_frame(ws)
        ws_send(ws, "session.resume", 1, session_id=s["id"]); ws_response(ws, 1)
        ws_send(ws, "prompt.submit", 2, session_id=s["id"], content="hi")
        ws_response(ws, 2)
        ws_events_until(ws, "turn.settled")
    assert client.provider.last_kwargs["model"] == "test-model"   # agent 原 model
    assert client.provider.last_kwargs["thinking"] is None


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


def test_cache_hit_percent():
    assert cache_hit_percent(0, 100) is None
    assert cache_hit_percent(50, 0) is None
    assert cache_hit_percent(30, 100) == 30
    assert cache_hit_percent(200, 100) == 100   # 截断


def test_turn_usage_and_done_usage(client, bob, agent_id):
    from sophclaw.models import AssistantTurn
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
