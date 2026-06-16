"""FastAPI app factory: lifespan wiring, admin bootstrap, static frontend."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from . import __version__
from .agent.manager import SessionManager
from .api import mount_routes
from .auth import hash_password
from .config import get_config
from .db import Database

log = logging.getLogger("sophclaw")

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

    from .skills.store import SkillStore
    from .tools import load_all

    load_all()
    app.state.db = db
    app.state.skill_store = SkillStore(cfg.skills_dir)
    app.state.manager = SessionManager(cfg.max_concurrent_turns)
    if not cfg.providers:
        log.warning("no model providers configured; set SOPHCLAW_PROVIDERS or providers.yaml")
    yield
    await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="sophclaw-agent", version=__version__, lifespan=lifespan)
    mount_routes(app)

    @app.get("/healthz")
    async def healthz():
        return JSONResponse({"status": "ok", "version": __version__})

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    return app


app = create_app()
