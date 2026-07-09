"""IMController: token 变化驱动 polling (re)start/stop。"""
import asyncio
import pytest
from sophagent.im.controller import IMController


class FakeDB:
    def __init__(self, token="", qq_app_id="", qq_client_secret="", feishu_app_id="", feishu_app_secret="",
                 weixin_account_id="", weixin_token="", weixin_base_url="",
                 dingtalk_client_id="", dingtalk_client_secret="", dingtalk_card_template_id=""):
        self._t = token
        self._qq_app_id = qq_app_id
        self._qq_client_secret = qq_client_secret
        self._feishu_app_id = feishu_app_id
        self._feishu_app_secret = feishu_app_secret
        self._weixin_account_id = weixin_account_id
        self._weixin_token = weixin_token
        self._weixin_base_url = weixin_base_url
        self._dingtalk_client_id = dingtalk_client_id
        self._dingtalk_client_secret = dingtalk_client_secret
        self._dingtalk_card_template_id = dingtalk_card_template_id
    async def get_setting(self, key):
        if key == "telegram_bot_token":
            return self._t
        if key == "qq_app_id":
            return self._qq_app_id
        if key == "qq_client_secret":
            return self._qq_client_secret
        if key == "feishu_app_id":
            return self._feishu_app_id
        if key == "feishu_app_secret":
            return self._feishu_app_secret
        if key == "weixin_account_id":
            return self._weixin_account_id
        if key == "weixin_token":
            return self._weixin_token
        if key == "weixin_base_url":
            return self._weixin_base_url
        if key == "dingtalk_client_id":
            return self._dingtalk_client_id
        if key == "dingtalk_client_secret":
            return self._dingtalk_client_secret
        if key == "dingtalk_card_template_id":
            return self._dingtalk_card_template_id
        if key in {
            "dingtalk_robot_code", "dingtalk_allowed_user_ids", "dingtalk_dm_policy",
            "dingtalk_require_mention", "weixin_allowed_user_ids", "weixin_dm_policy",
            "feishu_domain", "feishu_connection_mode", "feishu_verification_token",
            "feishu_encrypt_key", "weixin_cdn_base_url", "weixin_split_multiline",
        }:
            return None
        return None


@pytest.mark.asyncio
async def test_restart_starts_polling_when_token_set():
    started = []
    async def factory(token, driver, allowed):
        started.append(token)
    c = IMController(driver=None, polling_factory=factory)
    await c.restart(FakeDB("AAA"))
    await asyncio.sleep(0)   # 让 created task 跑到 factory 体
    assert started == ["AAA"]
    assert c.current_token == "AAA"
    await c.stop()


@pytest.mark.asyncio
async def test_restart_noop_when_unchanged():
    n = 0
    async def factory(token, driver, allowed):
        nonlocal n; n += 1
    c = IMController(driver=None, polling_factory=factory)
    await c.restart(FakeDB("AAA"))
    await asyncio.sleep(0)
    await c.restart(FakeDB("AAA"))   # 同 token，不应再起
    await asyncio.sleep(0)
    assert n == 1
    await c.stop()


@pytest.mark.asyncio
async def test_restart_stops_when_token_cleared():
    stopped = []
    async def factory(token, driver, allowed):
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            stopped.append(token); raise
    c = IMController(driver=None, polling_factory=factory)
    await c.restart(FakeDB("AAA"))
    await asyncio.sleep(0)   # 让 AAA task 进 sleep
    await c.restart(FakeDB(""))      # 清空 → 停
    assert stopped == ["AAA"]
    assert c.current_token is None and c.task is None


@pytest.mark.asyncio
async def test_restart_swaps_on_token_change():
    calls = []
    async def factory(token, driver, allowed):
        calls.append(token)
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            raise
    c = IMController(driver=None, polling_factory=factory)
    await c.restart(FakeDB("AAA"))
    await asyncio.sleep(0)
    await c.restart(FakeDB("BBB"))   # 换 token → 停旧起新
    await asyncio.sleep(0)
    assert calls == ["AAA", "BBB"]
    await c.stop()


@pytest.mark.asyncio
async def test_restart_starts_qq_when_configured():
    started = []
    async def qq_factory(app_id, client_secret, driver, allowed):
        started.append((app_id, client_secret))
    c = IMController(driver=None, qq_factory=qq_factory)
    await c.restart(FakeDB(qq_app_id="APPID", qq_client_secret="SECRET"))
    await asyncio.sleep(0)
    assert started == [("APPID", "SECRET")]
    assert c.current_qq_config == ("APPID", "SECRET")
    await c.stop()


@pytest.mark.asyncio
async def test_restart_starts_feishu_when_configured():
    started = []
    async def feishu_factory(config, driver, allowed, *, require_mention_in_group=True):
        started.append((config.app_id, config.app_secret, config.connection_mode))
    c = IMController(driver=None, feishu_factory=feishu_factory)
    await c.restart(FakeDB(feishu_app_id="CLI", feishu_app_secret="SEC"))
    await asyncio.sleep(0)
    assert started == [("CLI", "SEC", "websocket")]
    assert c.current_feishu_config.app_id == "CLI"
    await c.stop()


@pytest.mark.asyncio
async def test_restart_starts_weixin_when_configured():
    started = []
    async def weixin_factory(config, driver, allowed, *, dm_policy="pairing"):
        started.append((config.account_id, config.token))
    c = IMController(driver=None, weixin_factory=weixin_factory)
    await c.restart(FakeDB(weixin_account_id="WXID", weixin_token="TOKEN"))
    await asyncio.sleep(0)
    assert started == [("WXID", "TOKEN")]
    assert c.current_weixin_config[0] == "WXID"
    assert c.current_weixin_config[1] == "TOKEN"
    await c.stop()


@pytest.mark.asyncio
async def test_restart_starts_dingtalk_when_configured():
    started = []
    async def dingtalk_factory(config, driver, data_dir, *, dm_policy="pairing", require_mention=True):
        started.append((config.client_id, config.card_template_id, dm_policy))
    c = IMController(driver=None, dingtalk_factory=dingtalk_factory)
    await c.restart(FakeDB(
        dingtalk_client_id="APPKEY",
        dingtalk_client_secret="SECRET",
    ))
    await asyncio.sleep(0)
    assert started == [("APPKEY", "", "pairing")]
    assert c.current_dingtalk_config[0] == "APPKEY"
    await c.stop()
