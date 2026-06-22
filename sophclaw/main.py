"""FastAPI app factory: lifespan wiring, admin bootstrap, static frontend."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .agent.manager import SessionManager
from .api import mount_routes
from .auth import hash_password
from .config import get_config
from .db import Database

log = logging.getLogger("sophclaw")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
WEB_DIST = WEB_DIR / "dist"


def _using_dist() -> bool:
    return (WEB_DIST / "index.html").is_file()


async def _bootstrap_admin(db: Database) -> None:
    cfg = get_config()
    if await db.count_users() > 0:
        return
    password = cfg.admin_password or secrets.token_urlsafe(12)
    await db.create_user(cfg.admin_username, hash_password(password), "admin")
    if cfg.admin_password:
        log.info("bootstrap: created admin user %r", cfg.admin_username)
    else:
        log.warning("bootstrap: created admin user %r with generated password: %s",
                    cfg.admin_username, password)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = get_config()
    if cfg.no_login:
        log.info("no-login mode enabled — API accepts requests without JWT")
    db = Database(cfg.db_path)
    await db.connect()
    await _bootstrap_admin(db)
    await db.ensure_groups()

    from .skills.seed import seed_builtin_skills
    from .skills.store import SkillStore
    from .tools import load_all

    load_all()
    app.state.db = db
    seeded = seed_builtin_skills(cfg.skills_dir)
    if seeded:
        log.info("seeded %d built-in skills into %s", seeded, cfg.skills_dir)
    app.state.skill_store = SkillStore(cfg.skills_dir)
    app.state.manager = SessionManager(cfg.max_concurrent_turns)
    from .providers.registry import init_registry
    registry = init_registry(db, cfg)
    await registry.refresh()
    if not registry.names():
        log.warning("no model providers configured; set SOPHCLAW_PROVIDERS or providers.yaml")
    yield
    await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="sophclaw-agent", version=__version__, lifespan=lifespan)
    mount_routes(app)

    @app.get("/healthz")
    async def healthz():
        return JSONResponse({"status": "ok", "version": __version__})

    if _using_dist():
        # Vue SPA (built by frontend/ → web/dist). html=True enables client-side routing fallback.
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="frontend")
    else:
        from fastapi.responses import FileResponse
        import mimetypes

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

        mimetypes.add_type("font/woff2", ".woff2")
        vendor_dir = WEB_DIR / "vendor"
        if vendor_dir.is_dir():
            app.mount("/vendor", StaticFiles(directory=vendor_dir), name="vendor")

    return app


app = create_app()
