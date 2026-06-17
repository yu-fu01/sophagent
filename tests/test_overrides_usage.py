"""会话覆盖字段贯通 runner 的集成测试。"""

import pytest


def test_session_override_thinking_passed_to_provider(client, bob, agent_id):
    s = client.post("/api/sessions", headers=bob, json={"agent_id": agent_id}).json()
    client.patch(f"/api/sessions/{s['id']}", headers=bob,
                 json={"thinking_mode": "thinking", "override_model": "big-model"})
    r = client.post(f"/api/sessions/{s['id']}/chat", headers=bob, json={"content": "hi"})
    assert r.status_code == 200
    assert client.provider.last_kwargs["thinking"] == "thinking"
    assert client.provider.last_kwargs["model"] == "big-model"
