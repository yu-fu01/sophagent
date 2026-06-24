"""IM pair-code REST route."""
import asyncio


def test_issue_pair_code(client, bob, agent_id):
    r = client.post("/api/im/pair-code", json={"agent_id": agent_id}, headers=bob)
    assert r.status_code == 200
    code = r.json()["code"]
    assert len(code) == 8


def test_issue_pair_code_unknown_agent(client, bob):
    r = client.post("/api/im/pair-code", json={"agent_id": 99999}, headers=bob)
    assert r.status_code == 404


def test_set_telegram_token_routes(monkeypatch, client, admin):
    started = []
    async def fake_factory(token, driver, allowed):
        started.append(token)
        try: await asyncio.sleep(100)
        except asyncio.CancelledError: raise
    client.app.state.im_controller._factory = fake_factory
    r = client.put("/api/settings/telegram", json={"token": "FAKE_TOKEN"}, headers=admin)
    assert r.status_code == 200 and r.json()["configured"] is True
    assert "FAKE_TOKEN" not in client.get("/api/settings", headers=admin).text
    r = client.put("/api/settings/telegram", json={"token": ""}, headers=admin)
    assert r.json()["configured"] is False
    assert started == ["FAKE_TOKEN"]
