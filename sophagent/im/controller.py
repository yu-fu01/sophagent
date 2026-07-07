"""IMController: Telegram polling 的运行时生命周期。

token 现场可配（DB settings），controller 监听 token 变化：改了就停旧起新，
清空就停。lifespan 建一个 controller 实例放 app.state.im_controller；
settings 路由改 token 后调 controller.restart(db)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from ..config import FeishuConfig, effective_feishu_config, effective_qq_config, effective_telegram_token

log = logging.getLogger(__name__)


async def _default_factory(token: str, driver, allowed_user_ids) -> None:
    from .adapter import TelegramClient, run_polling
    await run_polling(TelegramClient(token), driver, allowed_user_ids=allowed_user_ids)


async def _default_qq_factory(app_id: str, client_secret: str, driver, allowed_user_ids) -> None:
    from .platforms.qqbot import QQBotClient, run_qqbot
    await run_qqbot(
        QQBotClient(app_id, client_secret),
        driver,
        allowed_user_ids=allowed_user_ids,
    )


async def _default_feishu_factory(
    config: FeishuConfig,
    driver,
    allowed_user_ids,
    *,
    require_mention_in_group: bool = True,
) -> None:
    from .platforms.feishu import FeishuClient, run_feishu
    client = FeishuClient(config.app_id, config.app_secret, domain=config.domain)
    await run_feishu(
        client,
        driver,
        allowed_user_ids=allowed_user_ids,
        require_mention_in_group=require_mention_in_group,
        connection_mode=config.connection_mode,
        verification_token=config.verification_token,
        encrypt_key=config.encrypt_key,
    )


class IMController:
    def __init__(
        self,
        driver,
        allowed_user_ids: tuple[int, ...] = (),
        qq_allowed_user_ids: tuple[str, ...] = (),
        feishu_allowed_user_ids: tuple[str, ...] = (),
        *,
        feishu_require_mention: bool = True,
        polling_factory: Callable[..., Awaitable[None]] | None = None,
        qq_factory: Callable[..., Awaitable[None]] | None = None,
        feishu_factory: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self.driver = driver
        self.allowed_user_ids = allowed_user_ids
        self.qq_allowed_user_ids = qq_allowed_user_ids
        self.feishu_allowed_user_ids = feishu_allowed_user_ids
        self.feishu_require_mention = feishu_require_mention
        self._factory = polling_factory or _default_factory
        self._qq_factory = qq_factory or _default_qq_factory
        self._feishu_factory = feishu_factory or _default_feishu_factory
        self.task: asyncio.Task | None = None
        self.current_token: str | None = None
        self.qq_task: asyncio.Task | None = None
        self.current_qq_config: tuple[str, str] | None = None
        self.feishu_task: asyncio.Task | None = None
        self.current_feishu_config: FeishuConfig | None = None
        self._lock = asyncio.Lock()

    async def restart(self, db) -> None:
        """读 effective token；变了就停旧起新，空就停。"""
        async with self._lock:
            await self._restart_telegram_locked(db)
            await self._restart_qq_locked(db)
            await self._restart_feishu_locked(db)

    async def _restart_telegram_locked(self, db) -> None:
        token = await effective_telegram_token(db)
        if token == self.current_token:
            return
        await self._stop_telegram_locked()
        if not token:
            self.current_token = None
            log.info("IM gateway (telegram) polling stopped (no token)")
            return
        self.task = asyncio.create_task(
            self._factory(token, self.driver, self.allowed_user_ids))
        self.current_token = token
        log.info("IM gateway (telegram) polling (re)started")

    async def _restart_qq_locked(self, db) -> None:
        app_id, client_secret = await effective_qq_config(db)
        next_config = (app_id, client_secret) if app_id and client_secret else None
        if next_config == self.current_qq_config:
            return
        await self._stop_qq_locked()
        if next_config is None:
            self.current_qq_config = None
            log.info("IM gateway (qqbot) stopped (missing credentials)")
            return
        self.qq_task = asyncio.create_task(
            self._qq_factory(app_id, client_secret, self.driver, self.qq_allowed_user_ids)
        )
        self.current_qq_config = next_config
        log.info("IM gateway (qqbot) (re)started")

    async def _restart_feishu_locked(self, db) -> None:
        config = await effective_feishu_config(db)
        next_config = config if config.app_id and config.app_secret else None
        if next_config == self.current_feishu_config:
            return
        await self._stop_feishu_locked()
        if next_config is None:
            self.current_feishu_config = None
            log.info("IM gateway (feishu) stopped (missing credentials)")
            return
        self.feishu_task = asyncio.create_task(
            self._feishu_factory(
                next_config,
                self.driver,
                self.feishu_allowed_user_ids,
                require_mention_in_group=self.feishu_require_mention,
            )
        )
        self.current_feishu_config = next_config
        log.info("IM gateway (feishu) (re)started mode=%s", next_config.connection_mode)

    async def _stop_telegram_locked(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        self.task = None

    async def _stop_qq_locked(self) -> None:
        if self.qq_task is not None and not self.qq_task.done():
            self.qq_task.cancel()
            try:
                await self.qq_task
            except (asyncio.CancelledError, Exception):
                pass
        self.qq_task = None

    async def _stop_feishu_locked(self) -> None:
        if self.feishu_task is not None and not self.feishu_task.done():
            self.feishu_task.cancel()
            try:
                await self.feishu_task
            except (asyncio.CancelledError, Exception):
                pass
        self.feishu_task = None

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_telegram_locked()
            self.current_token = None
            await self._stop_qq_locked()
            self.current_qq_config = None
            await self._stop_feishu_locked()
            self.current_feishu_config = None
