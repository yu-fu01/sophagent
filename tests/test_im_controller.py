"""IMController: token 变化驱动 polling (re)start/stop。"""
import asyncio
import pytest
from sophclaw.im.controller import IMController


class FakeDB:
    def __init__(self, token=""): self._t = token
    async def get_setting(self, key): return self._t if key == "telegram_bot_token" else None


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
