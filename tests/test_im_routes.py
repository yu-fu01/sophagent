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


def test_set_weixin_config_routes(monkeypatch, client, admin):
    started = []
    async def fake_weixin_factory(config, driver, allowed, *, dm_policy="pairing"):
        started.append((config.account_id, config.token, dm_policy))
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            raise
    client.app.state.im_controller._weixin_factory = fake_weixin_factory
    r = client.put(
        "/api/settings/weixin",
        json={
            "account_id": "WX_ACCT",
            "token": "WX_TOKEN",
            "base_url": "https://ilinkai.weixin.qq.com",
            "dm_policy": "pairing",
            "allowed_user_ids": "user1",
        },
        headers=admin,
    )
    assert r.status_code == 200 and r.json()["configured"] is True
    assert "WX_TOKEN" not in client.get("/api/settings", headers=admin).text
    r = client.put(
        "/api/settings/weixin",
        json={
            "account_id": "",
            "token": "",
            "base_url": "https://ilinkai.weixin.qq.com",
            "dm_policy": "pairing",
            "allowed_user_ids": "",
        },
        headers=admin,
    )
    assert r.json()["configured"] is False
    assert started == [("WX_ACCT", "WX_TOKEN", "pairing")]


def test_restart_weixin_route(client, admin):
    client.app.state.im_controller.current_weixin_config = ("old", "tok", "https://ilinkai.weixin.qq.com", "", "pairing")
    r = client.post("/api/settings/weixin/restart", headers=admin)
    assert r.status_code == 200 and r.json()["ok"] is True


def test_weixin_qr_routes(monkeypatch, client, admin):
    import sophagent.im.platforms.weixin.auth as wx_auth

    async def fake_start():
        return {
            "session_id": "sess-1",
            "status": "wait",
            "qrcode_url": "https://example.com/qr",
            "qr_svg": "<svg></svg>",
            "message": "请扫码",
            "expires_in": 600,
        }

    async def fake_poll(session_id):
        assert session_id == "sess-1"
        return {
            "session_id": session_id,
            "status": "confirmed",
            "message": "ok",
            "account_id": "acct-1",
            "token": "tok-1",
            "base_url": "https://ilinkai.weixin.qq.com",
            "user_id": "u1",
        }

    monkeypatch.setattr(wx_auth, "start_qr_session", fake_start)
    monkeypatch.setattr(wx_auth, "poll_qr_session", fake_poll)
    monkeypatch.setattr(wx_auth, "persist_qr_credentials", lambda *a, **k: None)

    r = client.post("/api/settings/weixin/qr", headers=admin)
    assert r.status_code == 200
    assert r.json()["session_id"] == "sess-1"

    r = client.get("/api/settings/weixin/qr/sess-1", headers=admin)
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    assert r.json()["account_id"] == "acct-1"
    assert "tok-1" not in r.text


def test_set_dingtalk_config_routes(monkeypatch, client, admin):
    started = []
    async def fake_dingtalk_factory(config, driver, data_dir, *, dm_policy="pairing", require_mention=True):
        started.append((config.client_id, config.client_secret, config.card_template_id, dm_policy))
    client.app.state.im_controller._dingtalk_factory = fake_dingtalk_factory
    r = client.put(
        "/api/settings/dingtalk",
        json={
            "client_id": "DT_APP",
            "client_secret": "DT_SECRET",
            "card_template_id": "CARD_TMPL",
            "dm_policy": "pairing",
            "require_mention": True,
            "allowed_user_ids": "staff1",
        },
        headers=admin,
    )
    assert r.status_code == 200 and r.json()["configured"] is True
    assert "DT_SECRET" not in client.get("/api/settings", headers=admin).text
    r = client.put(
        "/api/settings/dingtalk",
        json={
            "client_id": "",
            "client_secret": "",
            "card_template_id": "",
            "dm_policy": "pairing",
            "require_mention": True,
            "allowed_user_ids": "",
        },
        headers=admin,
    )
    assert r.json()["configured"] is False
    assert started == [("DT_APP", "DT_SECRET", "CARD_TMPL", "pairing")]


def test_dingtalk_without_card_template_configures(monkeypatch, client, admin):
    started = []
    async def fake_dingtalk_factory(config, driver, data_dir, *, dm_policy="pairing", require_mention=True):
        started.append((config.client_id, config.card_template_id))
    client.app.state.im_controller._dingtalk_factory = fake_dingtalk_factory
    r = client.put(
        "/api/settings/dingtalk",
        json={
            "client_id": "DT_APP",
            "client_secret": "DT_SECRET",
            "card_template_id": "",
            "dm_policy": "pairing",
        },
        headers=admin,
    )
    assert r.status_code == 200 and r.json()["configured"] is True
    assert started and started[0] == ("DT_APP", "")


def test_dingtalk_qr_routes(monkeypatch, client, admin):
    async def fake_start():
        return {
            "session_id": "dt-sess-1",
            "status": "waiting",
            "qr_svg": "<svg></svg>",
            "verification_uri": "https://example.com/qr",
            "message": "scan me",
        }

    async def fake_poll(session_id):
        assert session_id == "dt-sess-1"
        return {
            "status": "confirmed",
            "message": "ok",
            "client_id": "APPKEY",
            "client_secret": "APPSECRET",
        }

    import sophagent.im.platforms.dingtalk.auth as dt_auth
    monkeypatch.setattr(dt_auth, "start_qr_session", fake_start)
    monkeypatch.setattr(dt_auth, "poll_qr_session", fake_poll)

    r = client.post("/api/settings/dingtalk/qr", headers=admin)
    assert r.status_code == 200
    assert r.json()["session_id"] == "dt-sess-1"

    r = client.get("/api/settings/dingtalk/qr/dt-sess-1", headers=admin)
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    assert r.json()["client_id"] == "APPKEY"
    assert "APPSECRET" not in r.text

