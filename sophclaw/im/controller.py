"""IMController: Telegram polling 的运行时生命周期。

token 现场可配（DB settings），controller 监听 token 变化：改了就停旧起新，
清空就停。lifespan 建一个 controller 实例放 app.state.im_controller；
settings 路由改 token 后调 controller.restart(db)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from ..config import effective_telegram_token

log = logging.getLogger(__name__)


async def _default_factory(token: str, driver, allowed_user_ids) -> None:
    from .adapter import TelegramClient, run_polling
    await run_polling(TelegramClient(token), driver, allowed_user_ids=allowed_user_ids)


class IMController:
    def __init__(self, driver, allowed_user_ids: tuple[int, ...] = (),
                 *, polling_factory: Callable[..., Awaitable[None]] | None = None) -> None:
        self.driver = driver
        self.allowed_user_ids = allowed_user_ids
        self._factory = polling_factory or _default_factory
        self.task: asyncio.Task | None = None
        self.current_token: str | None = None
        self._lock = asyncio.Lock()

    async def restart(self, db) -> None:
        """读 effective token；变了就停旧起新，空就停。"""
        async with self._lock:
            token = await effective_telegram_token(db)
            if token == self.current_token:
                return
            await self._stop_locked()
            if not token:
                self.current_token = None
                log.info("IM gateway (telegram) polling stopped (no token)")
                return
            self.task = asyncio.create_task(
                self._factory(token, self.driver, self.allowed_user_ids))
            self.current_token = token
            log.info("IM gateway (telegram) polling (re)started")

    async def _stop_locked(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        self.task = None

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()
            self.current_token = None
