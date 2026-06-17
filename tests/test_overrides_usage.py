"""会话覆盖字段贯通 runner 的集成测试。"""

from sophclaw.usage import cache_hit_percent


def test_session_override_thinking_passed_to_provider(client, bob, agent_id):
    s = client.post("/api/sessions", headers=bob, json={"agent_id": agent_id}).json()
    client.patch(f"/api/sessions/{s['id']}", headers=bob,
                 json={"thinking_mode": "thinking", "override_model": "big-model"})
    r = client.post(f"/api/sessions/{s['id']}/chat", headers=bob, json={"content": "hi"})
    assert r.status_code == 200
    assert client.provider.last_kwargs["thinking"] == "thinking"
    assert client.provider.last_kwargs["model"] == "big-model"


def test_no_override_falls_back_to_agent(client, bob, agent_id):
    s = client.post("/api/sessions", headers=bob, json={"agent_id": agent_id}).json()
    # 不设任何覆盖，直接 chat
    r = client.post(f"/api/sessions/{s['id']}/chat", headers=bob, json={"content": "hi"})
    assert r.status_code == 200
    assert client.provider.last_kwargs["model"] == "test-model"   # agent 原 model
    assert client.provider.last_kwargs["thinking"] is None


def test_thinking_default_folds_to_none(client, bob, agent_id):
    s = client.post("/api/sessions", headers=bob, json={"agent_id": agent_id}).json()
    client.patch(f"/api/sessions/{s['id']}", headers=bob, json={"thinking_mode": "default"})
    r = client.post(f"/api/sessions/{s['id']}/chat", headers=bob, json={"content": "hi"})
    assert r.status_code == 200
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
    r = client.post(f"/api/sessions/{s['id']}/chat", headers=bob, json={"content": "yo"})
    import json
    payloads = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
    turn_usage = next(p for p in payloads if p["type"] == "turn_usage")
    assert turn_usage["cache_read_tokens"] == 40 and turn_usage["cache_hit"] == 29  # 40/140
    done = next(p for p in payloads if p["type"] == "done")
    assert done["usage"]["input_tokens"] == 100
    assert "context_length" in done and "context_limit" in done
