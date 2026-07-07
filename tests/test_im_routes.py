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


def test_set_qqbot_config_routes(monkeypatch, client, admin):
    started = []
    async def fake_qq_factory(app_id, client_secret, driver, allowed):
        started.append((app_id, client_secret))
        try: await asyncio.sleep(100)
        except asyncio.CancelledError: raise
    client.app.state.im_controller._qq_factory = fake_qq_factory
    r = client.put(
        "/api/settings/qqbot",
        json={"app_id": "APPID", "client_secret": "SECRET"},
        headers=admin,
    )
    assert r.status_code == 200 and r.json()["configured"] is True
    assert "SECRET" not in client.get("/api/settings", headers=admin).text
    r = client.put(
        "/api/settings/qqbot",
        json={"app_id": "", "client_secret": ""},
        headers=admin,
    )
    assert r.json()["configured"] is False
    assert started == [("APPID", "SECRET")]


def test_restart_qqbot_route(client, admin):
    client.app.state.im_controller.current_qq_config = ("old", "secret")
    r = client.post("/api/settings/qqbot/restart", headers=admin)
    assert r.status_code == 200 and r.json()["ok"] is True


def test_test_qqbot_config_route(monkeypatch, client, admin):
    class FakeQQBotClient:
        def __init__(self, app_id, client_secret):
            self.app_id = app_id
            self.client_secret = client_secret
        async def gateway_url(self):
            return "wss://example.test/gateway"
        async def aclose(self):
            pass
    import sophagent.im.platforms.qqbot as qqbot
    monkeypatch.setattr(qqbot, "QQBotClient", FakeQQBotClient)
    r = client.post(
        "/api/settings/qqbot/test",
        json={"app_id": "APPID", "client_secret": "SECRET"},
        headers=admin,
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "gateway": True}


def test_set_feishu_config_routes(monkeypatch, client, admin):
    started = []
    async def fake_feishu_factory(config, driver, allowed, *, require_mention_in_group=True):
        started.append((config.app_id, config.app_secret))
        try: await asyncio.sleep(100)
        except asyncio.CancelledError: raise
    client.app.state.im_controller._feishu_factory = fake_feishu_factory
    r = client.put(
        "/api/settings/feishu",
        json={"app_id": "FEISHU_APP", "app_secret": "FEISHU_SECRET"},
        headers=admin,
    )
    assert r.status_code == 200 and r.json()["configured"] is True
    assert "FEISHU_SECRET" not in client.get("/api/settings", headers=admin).text
    r = client.put(
        "/api/settings/feishu",
        json={"app_id": "", "app_secret": ""},
        headers=admin,
    )
    assert r.json()["configured"] is False
    assert started == [("FEISHU_APP", "FEISHU_SECRET")]


def test_restart_feishu_route(client, admin):
    from sophagent.config import FeishuConfig
    client.app.state.im_controller.current_feishu_config = FeishuConfig("old", "secret")
    r = client.post("/api/settings/feishu/restart", headers=admin)
    assert r.status_code == 200 and r.json()["ok"] is True


def test_test_feishu_config_route(monkeypatch, client, admin):
    class FakeFeishuClient:
        def __init__(self, app_id, app_secret, domain="feishu"):
            self.app_id = app_id
            self.app_secret = app_secret
        async def probe(self):
            return {"bot": {"bot_name": "SophBot", "open_id": "ou_bot"}}
        async def aclose(self):
            pass
    import sophagent.im.platforms.feishu as feishu
    monkeypatch.setattr(feishu, "FeishuClient", FakeFeishuClient)
    r = client.post(
        "/api/settings/feishu/test",
        json={"app_id": "APPID", "app_secret": "SECRET"},
        headers=admin,
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["bot_name"] == "SophBot"
