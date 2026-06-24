"""WebSocket gateway integration tests (JSON-RPC over /ws).

Covers: auth failure (1008), submit streaming, interrupt, unknown method
(-32601), and the detach→reconnect replay of an in-flight turn. Uses the
shared ``client`` harness so the same turn core as the REST SSE path drives
these turns.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from sophclaw.config import ProviderConfig
import sophclaw.providers as providers_mod
from sophclaw.models import AssistantTurn, StreamEvent


def _ws_url(client, token: str, **extra) -> str:
    q = f"token={token}"
    for k, v in extra.items():
        q += f"&{k}={v}"
    return f"/ws?{q}"


def _send(ws, method: str, req_id: str | int, **params) -> None:
    frame = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    ws.send_text(json.dumps(frame))


def _recv(ws) -> dict:
    return json.loads(ws.receive_text())


def _recv_response(ws, req_id) -> dict:
    """Read frames until the response matching ``req_id`` arrives."""
    while True:
        frame = _recv(ws)
        if frame.get("id") == req_id:
            return frame


def _collect_events_until(ws, wire_type: str, timeout_events: int = 200) -> list[dict]:
    """Collect event frames until one of ``wire_type`` is seen."""
    seen = []
    for _ in range(timeout_events):
        frame = _recv(ws)
        if frame.get("method") != "event":
            continue
        params = frame["params"]
        seen.append(params)
        if params["type"] == wire_type:
            return seen
    raise AssertionError(f"never saw {wire_type}; got {seen}")


def test_ready_and_submit_streams(client, bob, agent_id):
    tok = bob["Authorization"].split(" ", 1)[1]
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]

    with client.websocket_connect(_ws_url(client, tok)) as ws:
        ready = _recv(ws)
        assert ready["method"] == "event"
        assert ready["params"]["type"] == "gateway.ready"

        _send(ws, "session.resume", 1, session_id=sid)
        assert _recv_response(ws, 1)["id"] == 1

        _send(ws, "prompt.submit", 2, session_id=sid, content="hello")
        ack = _recv_response(ws, 2)
        assert ack["result"]["kind"] == "start"

        events = _collect_events_until(ws, "session.info")
        types = [e["type"] for e in events]
        assert "message.delta" in types
        assert "message.complete" in types
        # the streamed text carries the echo
        delta = next(e for e in events if e["type"] == "message.delta")
        assert "echo: hello" in delta["payload"]["text"]

    # persistence worked (shared turn core)
    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_auth_missing_token_rejected(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws"):
            pass
    assert exc.value.code == 1008


def test_auth_bad_token_rejected(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws?token=not.a.jwt"):
            pass
    assert exc.value.code == 1008


def test_session_not_owned_forbidden(client, bob, agent_id, admin):
    """Resuming a session owned by another user → 4401."""
    # bob owns a session
    bob_tok = bob["Authorization"].split(" ", 1)[1]
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    # admin (different user) tries to resume it
    admin_tok = admin["Authorization"].split(" ", 1)[1]
    with client.websocket_connect(_ws_url(client, admin_tok)) as ws:
        _recv(ws)  # ready
        _send(ws, "session.resume", 1, session_id=sid)
        resp = _recv_response(ws, 1)
        assert resp["error"]["code"] == 4401


def test_unknown_method(client, bob):
    tok = bob["Authorization"].split(" ", 1)[1]
    with client.websocket_connect(_ws_url(client, tok)) as ws:
        _recv(ws)  # ready
        _send(ws, "bogus.method", 9)
        resp = _recv_response(ws, 9)
        assert resp["error"]["code"] == -32601


def test_interrupt_stops_turn(client, bob, agent_id, monkeypatch):
    """session.interrupt cancels the running turn; client sees an error event."""
    gate = {"proceed": asyncio.Event()}

    async def slow_chat(self, *, model, system, messages, tools=None,
                        temperature=None, max_tokens=None, thinking=None):
        yield StreamEvent("text_delta", text="first chunk")
        await gate["proceed"].wait()       # block until we interrupt
        yield StreamEvent("turn_done", turn=AssistantTurn(
            content="first chunk", stop_reason="stop", input_tokens=1, output_tokens=1))

    prov = client.provider
    monkeypatch.setattr(prov, "chat", slow_chat.__get__(prov))

    tok = bob["Authorization"].split(" ", 1)[1]
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]

    with client.websocket_connect(_ws_url(client, tok)) as ws:
        _recv(ws)
        _send(ws, "session.resume", 1, session_id=sid)
        _recv_response(ws, 1)
        _send(ws, "prompt.submit", 2, session_id=sid, content="go")
        _recv_response(ws, 2)             # ack
        # first delta streams
        delta = _recv(ws)
        assert delta["params"]["type"] == "message.delta"

        _send(ws, "session.interrupt", 3, session_id=sid)
        _recv_response(ws, 3)
        # the turn emits an error event ("stopped by user")
        events = _collect_events_until(ws, "error", timeout_events=20)
        assert any(e["type"] == "error" for e in events)

    # session is no longer busy
    assert not client.app.state.manager.is_busy(sid)


class _GatedProvider:
    """Echo provider that pauses mid-turn on a shared gate, so a test can
    disconnect mid-turn, then release the turn and reconnect to observe replay."""

    def __init__(self, gate: asyncio.Event):
        self.gate = gate
        self.history: list[str] = []

    async def chat(self, *, model, system, messages, tools=None,
                   temperature=None, max_tokens=None, thinking=None):
        last = next((m.content for m in reversed(messages) if m.role == "user"), "")
        self.history.append(last)
        yield StreamEvent("text_delta", text=f"chunk1:{last}")
        await self.gate.wait()             # mid-turn: client disconnects here
        yield StreamEvent("text_delta", text="chunk2")
        yield StreamEvent("turn_done", turn=AssistantTurn(
            content=f"chunk1:{last}chunk2", stop_reason="stop",
            input_tokens=2, output_tokens=2))


def test_disconnect_then_reconnect_replays_inflight(client, bob, agent_id, monkeypatch):
    """The signature feature: a client that disconnects mid-turn, then reconnects
    within the grace window, sees the events it missed (not lost to the detach)."""
    # shrink the grace window so the test can't accidentally time out waiting
    client.app.state.gateway_registry.grace_seconds = 30.0

    gate = asyncio.Event()
    gated = _GatedProvider(gate)
    monkeypatch.setitem(providers_mod._cache, "test", gated)

    tok = bob["Authorization"].split(" ", 1)[1]
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]

    # ── first connection: resume, submit, receive chunk1, then drop ──────────
    with client.websocket_connect(_ws_url(client, tok)) as ws:
        _recv(ws)
        _send(ws, "session.resume", 1, session_id=sid)
        _recv_response(ws, 1)
        _send(ws, "prompt.submit", 2, session_id=sid, content="hi")
        _recv_response(ws, 2)
        chunk1 = _recv(ws)
        assert chunk1["params"]["payload"]["text"] == "chunk1:hi"
    # connection closed mid-turn; the turn task keeps running in the background,
    # suspended on the gate (no `done` yet → buffer still holds chunk1)

    # ── reconnect WHILE the turn is still in flight (gate not released) ──────
    with client.websocket_connect(_ws_url(client, tok)) as ws:
        _recv(ws)  # ready
        _send(ws, "session.resume", 3, session_id=sid)
        # resume replays buffered in-flight events BEFORE the ack arrives
        replayed = []
        while True:
            frame = _recv(ws)
            if frame.get("id") == 3:           # the resume ack — replay finished
                assert frame["result"]["running"] is True
                break
            assert frame["method"] == "event"
            replayed.append(frame["params"])
        # chunk1 was replayed from the detach buffer — the crux of the feature
        assert any(e["type"] == "message.delta" and e["payload"]["text"] == "chunk1:hi"
                    for e in replayed), replayed

        # now release the turn: chunk2 + complete stream LIVE onto this client
        gate.set()
        events = _collect_events_until(ws, "session.info")
        texts = [e["payload"]["text"] for e in events if e["type"] == "message.delta"]
        assert texts == ["chunk2"]              # chunk1 not re-sent live (already replayed)
        assert any(e["type"] == "message.complete" for e in events)

    detail = client.get(f"/api/sessions/{sid}", headers=bob).json()
    contents = [m["content"] for m in detail["messages"] if m["role"] == "assistant"]
    assert any("chunk1:hi" in c and "chunk2" in c for c in contents), contents


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
