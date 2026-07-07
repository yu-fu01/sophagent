"""IMController: token 变化驱动 polling (re)start/stop。"""
import asyncio
import pytest
from sophagent.im.controller import IMController


class FakeDB:
    def __init__(self, token="", qq_app_id="", qq_client_secret="", feishu_app_id="", feishu_app_secret=""):
        self._t = token
        self._qq_app_id = qq_app_id
        self._qq_client_secret = qq_client_secret
        self._feishu_app_id = feishu_app_id
        self._feishu_app_secret = feishu_app_secret
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
