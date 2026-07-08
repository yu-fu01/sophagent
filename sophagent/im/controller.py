"""IMController: Telegram polling 的运行时生命周期。

token 现场可配（DB settings），controller 监听 token 变化：改了就停旧起新，
清空就停。lifespan 建一个 controller 实例放 app.state.im_controller；
settings 路由改 token 后调 controller.restart(db)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from ..config import (
    FeishuConfig,
    effective_dingtalk_allowed_chat_ids,
    effective_dingtalk_allowed_user_ids,
    effective_dingtalk_config,
    effective_dingtalk_dm_policy,
    effective_dingtalk_free_response_chats,
    effective_dingtalk_mention_patterns,
    effective_dingtalk_require_mention,
    effective_feishu_config,
    effective_qq_config,
    effective_telegram_token,
    effective_weixin_allowed_user_ids,
    effective_weixin_config,
    effective_weixin_dm_policy,
    get_config,
)

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


async def _default_weixin_factory(config, driver, allowed_user_ids, *, dm_policy: str = "pairing") -> None:
    from .platforms.weixin import run_weixin
    await run_weixin(
        config,
        driver,
        get_config().data_dir,
        allowed_user_ids=allowed_user_ids,
        dm_policy=dm_policy,
    )


async def _default_dingtalk_factory(
    config,
    driver,
    allowed_user_ids,
    *,
    dm_policy: str = "pairing",
    require_mention: bool = True,
) -> None:
    from .platforms.dingtalk import run_dingtalk
    config.dm_policy = dm_policy
    config.require_mention = require_mention
    await run_dingtalk(
        config,
        driver,
        get_config().data_dir,
        allowed_user_ids=allowed_user_ids,
    )


class IMController:
    def __init__(
        self,
        driver,
        allowed_user_ids: tuple[int, ...] = (),
        qq_allowed_user_ids: tuple[str, ...] = (),
        feishu_allowed_user_ids: tuple[str, ...] = (),
        weixin_allowed_user_ids: tuple[str, ...] = (),
        weixin_dm_policy: str = "pairing",
        dingtalk_allowed_user_ids: tuple[str, ...] = (),
        dingtalk_dm_policy: str = "pairing",
        dingtalk_require_mention: bool = True,
        *,
        feishu_require_mention: bool = True,
        polling_factory: Callable[..., Awaitable[None]] | None = None,
        qq_factory: Callable[..., Awaitable[None]] | None = None,
        feishu_factory: Callable[..., Awaitable[None]] | None = None,
        weixin_factory: Callable[..., Awaitable[None]] | None = None,
        dingtalk_factory: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self.driver = driver
        self.allowed_user_ids = allowed_user_ids
        self.qq_allowed_user_ids = qq_allowed_user_ids
        self.feishu_allowed_user_ids = feishu_allowed_user_ids
        self.weixin_allowed_user_ids = weixin_allowed_user_ids
        self.weixin_dm_policy = weixin_dm_policy
        self.dingtalk_allowed_user_ids = dingtalk_allowed_user_ids
        self.dingtalk_dm_policy = dingtalk_dm_policy
        self.dingtalk_require_mention = dingtalk_require_mention
        self.feishu_require_mention = feishu_require_mention
        self._factory = polling_factory or _default_factory
        self._qq_factory = qq_factory or _default_qq_factory
        self._feishu_factory = feishu_factory or _default_feishu_factory
        self._weixin_factory = weixin_factory or _default_weixin_factory
        self._dingtalk_factory = dingtalk_factory or _default_dingtalk_factory
        self.task: asyncio.Task | None = None
        self.current_token: str | None = None
        self.qq_task: asyncio.Task | None = None
        self.current_qq_config: tuple[str, str] | None = None
        self.feishu_task: asyncio.Task | None = None
        self.current_feishu_config: FeishuConfig | None = None
        self.weixin_task: asyncio.Task | None = None
        self.current_weixin_config: tuple[str, str, str, str, str] | None = None
        self.dingtalk_task: asyncio.Task | None = None
        self.current_dingtalk_config: tuple[str, ...] | None = None
        self._lock = asyncio.Lock()

    async def restart(self, db) -> None:
        """读 effective token；变了就停旧起新，空就停。"""
        async with self._lock:
            await self._restart_telegram_locked(db)
            await self._restart_qq_locked(db)
            await self._restart_feishu_locked(db)
            await self._restart_weixin_locked(db)
            await self._restart_dingtalk_locked(db)

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

    async def _restart_weixin_locked(self, db) -> None:
        config = await effective_weixin_config(db)
        allowed = await effective_weixin_allowed_user_ids(db)
        dm_policy = await effective_weixin_dm_policy(db)
        next_config = None
        if config:
            next_config = (
                config.account_id,
                config.token,
                config.base_url,
                ",".join(allowed),
                dm_policy,
            )
        if next_config == self.current_weixin_config:
            return
        await self._stop_weixin_locked()
        if next_config is None:
            self.current_weixin_config = None
            log.info("IM gateway (weixin) stopped (missing credentials)")
            return
        self.weixin_task = asyncio.create_task(
            self._weixin_factory(
                config,
                self.driver,
                allowed,
                dm_policy=dm_policy,
            )
        )
        self.current_weixin_config = next_config
        log.info("IM gateway (weixin) (re)started account=%s", config.account_id[:8])

    async def _restart_dingtalk_locked(self, db) -> None:
        config = await effective_dingtalk_config(db)
        allowed = await effective_dingtalk_allowed_user_ids(db)
        dm_policy = await effective_dingtalk_dm_policy(db)
        require_mention = await effective_dingtalk_require_mention(db)
        allowed_chat_ids = await effective_dingtalk_allowed_chat_ids(db)
        free_response_chats = await effective_dingtalk_free_response_chats(db)
        mention_patterns = await effective_dingtalk_mention_patterns(db)
        next_config = None
        if config:
            next_config = (
                config.client_id,
                config.client_secret,
                config.card_template_id,
                ",".join(allowed),
                f"{dm_policy}|{int(require_mention)}",
                ",".join(allowed_chat_ids),
                ",".join(free_response_chats),
                "|".join(mention_patterns),
            )
        if next_config == self.current_dingtalk_config:
            return
        await self._stop_dingtalk_locked()
        if next_config is None:
            self.current_dingtalk_config = None
            log.info("IM gateway (dingtalk) stopped (missing credentials)")
            return
        self.dingtalk_task = asyncio.create_task(
            self._dingtalk_factory(
                config,
                self.driver,
                allowed,
                dm_policy=dm_policy,
                require_mention=require_mention,
            )
        )
        self.current_dingtalk_config = next_config
        log.info("IM gateway (dingtalk) (re)started client_id=%s", config.client_id[:8])

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

    async def _stop_weixin_locked(self) -> None:
        if self.weixin_task is not None and not self.weixin_task.done():
            self.weixin_task.cancel()
            try:
                await self.weixin_task
            except (asyncio.CancelledError, Exception):
                pass
        self.weixin_task = None

    async def _stop_dingtalk_locked(self) -> None:
        if self.dingtalk_task is not None and not self.dingtalk_task.done():
            self.dingtalk_task.cancel()
            try:
                await self.dingtalk_task
            except (asyncio.CancelledError, Exception):
                pass
        self.dingtalk_task = None

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_telegram_locked()
            self.current_token = None
            await self._stop_qq_locked()
            self.current_qq_config = None
            await self._stop_feishu_locked()
            self.current_feishu_config = None
            await self._stop_weixin_locked()
            self.current_weixin_config = None
            await self._stop_dingtalk_locked()
            self.current_dingtalk_config = None
