"""FastAPI app factory: lifespan wiring, admin bootstrap, static frontend."""

from __future__ import annotations

import logging
import mimetypes
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .agent.manager import SessionManager
from .api import mount_routes
from .auth import hash_password
from .config import get_config
from .db import Database

log = logging.getLogger("sophagent")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


async def _bootstrap_admin(db: Database) -> None:
    cfg = get_config()
    if await db.count_users() > 0:
        return
    password = cfg.admin_password or secrets.token_urlsafe(12)
    await db.create_user(cfg.admin_username, hash_password(password), "admin")
    if cfg.admin_password:
        log.info("bootstrap: created admin user %r", cfg.admin_username)
    else:
        # printed once; user must note it down or set ADMIN_PASSWORD
        log.warning("bootstrap: created admin user %r with generated password: %s",
                    cfg.admin_username, password)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = get_config()
    db = Database(cfg.db_path)
    await db.connect()
    await _bootstrap_admin(db)
    await db.ensure_groups()  # admin group + personal groups; idempotent (also migrates old DBs)

    from .skills.seed import seed_builtin_skills
    from .skills.store import SkillStore
    from .tools import load_all

    load_all()
    from .tools.sandbox import sandbox_status
    status = sandbox_status()
    (log.info if "active" in status else log.warning)(status)
    app.state.db = db
    seeded = seed_builtin_skills(cfg.skills_dir)  # populate missing built-in skills
    if seeded:
        log.info("seeded %d built-in skills into %s", seeded, cfg.skills_dir)
    app.state.skill_store = SkillStore(cfg.skills_dir)
    app.state.manager = SessionManager(cfg.max_concurrent_turns)
    from .gateway.ws import build_registry
    app.state.gateway_registry = build_registry()
    from .providers.registry import init_registry
    registry = init_registry(db, cfg)
    await registry.refresh()
    if not registry.names():
        log.warning("no model providers configured; set SOPHAGENT_PROVIDERS or providers.yaml")

    # IM 网关（Telegram）：token 现场可配（DB settings），controller 管 polling 生命周期
    from .im.controller import IMController
    from .im.driver import IMDriver
    im_driver = IMDriver(db=db, manager=app.state.manager, skill_store=app.state.skill_store)
    app.state.im_controller = IMController(im_driver, allowed_user_ids=cfg.telegram_allowed_user_ids)
    await app.state.im_controller.restart(db)

    # 定时任务（cron）：daemon tick 循环，到期在来源 session 产出 output
    from .cron import CronScheduler
    app.state.cron_scheduler = CronScheduler(
        db, app.state.manager, app.state.skill_store, app.state.gateway_registry,
    )
    if cfg.cron_enabled:
        await app.state.cron_scheduler.start()

    yield
    if cfg.cron_enabled:
        await app.state.cron_scheduler.stop()
    await app.state.im_controller.stop()
    await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="sophagent", version=__version__, lifespan=lifespan)
    mount_routes(app)

    from .gateway.ws import handle_ws

    @app.websocket("/ws")
    async def _ws(ws: WebSocket):
        await handle_ws(ws)

    @app.get("/healthz")
    async def healthz():
        return JSONResponse({"status": "ok", "version": __version__})

    # 前端外壳文件（HTML / 应用 JS）无版本指纹，文件名不随内容变化。
    # 设 no-cache 强制浏览器每次带 etag 回源校验：未变返 304（廉价），
    # 重新部署后改动立即生效，避免「改了前端却被旧缓存覆盖」（需硬刷新）的坑。
    # 第三方资源（/vendor 下 KaTeX 等）极少变动，仍由 StaticFiles 默认缓存。
    NO_CACHE = {"Cache-Control": "no-cache"}

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB_DIR / "index.html", headers=NO_CACHE)

    @app.get("/worklog.js", include_in_schema=False)
    async def worklog_js():
        return FileResponse(WEB_DIR / "worklog.js", media_type="application/javascript", headers=NO_CACHE)

    @app.get("/markdown.js", include_in_schema=False)
    async def markdown_js():
        return FileResponse(WEB_DIR / "markdown.js", media_type="application/javascript", headers=NO_CACHE)

    # 前端第三方资源（KaTeX 等）。woff2 需显式注册 mime，否则浏览器拒绝加载。
    mimetypes.add_type("font/woff2", ".woff2")
    vendor_dir = WEB_DIR / "vendor"
    if vendor_dir.is_dir():
        app.mount("/vendor", StaticFiles(directory=vendor_dir), name="vendor")

    return app


app = create_app()
