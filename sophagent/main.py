"""FastAPI app factory: lifespan wiring, admin bootstrap, static frontend."""

from __future__ import annotations

import logging
import mimetypes
import os
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


def _harden_data_dir(cfg) -> None:
    """root 容器内收紧 data 目录权限：目录 0711(可穿行不可列举)，密钥文件
    0600，skills 目录 0755(脚本需可读)，使降权后的 exec 子进程(sandbox 用户)
    读不到密钥但能进入自己的 workspace。非 root 环境直接跳过(本地开发无降权)。"""
    if os.geteuid() != 0:
        return
    for d in (cfg.data_dir, cfg.workspaces_dir):
        try:
            os.chmod(d, 0o711)
        except OSError:
            pass
    # skill 脚本需对降权后的 sandbox 子进程可读：递归授目录 o+x、文件 o+r
    if cfg.skills_dir.exists():
        for root, dirs, files in os.walk(cfg.skills_dir):
            try:
                os.chmod(root, 0o755)
            except OSError:
                pass
            for name in files:
                try:
                    p = os.path.join(root, name)
                    os.chmod(p, os.stat(p).st_mode | 0o044)  # 加 group/other 读
                except OSError:
                    pass
    secret_files = [cfg.data_dir / ".secret", cfg.data_dir / "providers.yaml"]
    secret_files += sorted(cfg.data_dir.glob("sophagent.db*"))  # db + WAL/SHM 旁文件
    for f in secret_files:
        try:
            if f.exists():
                os.chmod(f, 0o600)
        except OSError:
            pass


async def _run_exec_selftest(cfg) -> None:
    """启动期功能自检：真的走启动器跑一条命令，验证 exec 能执行、且(隔离应生效时)
    读不到沙箱外的文件。这在生产事件循环(uvloop)下运行，能抓住 sandbox_status
    仍报 active 却实际全崩/隔离失效 的静默退化。

    隔离本应生效却自检失败时，默认 **拒绝启动**（把回归挡在上线前）；
    设 ``SOPHAGENT_SANDBOX_SELFTEST=warn``（或 off）可降级为仅告警。"""
    from .tools.sandbox import exec_credentials, landlock_available
    from .tools.terminal import selftest_exec

    ok, msg = await selftest_exec(cfg)
    if ok:
        log.info("exec sandbox self-test: %s", msg)
        return
    log.error("exec sandbox self-test: %s", msg)
    isolation_expected = exec_credentials() is not None or landlock_available()
    mode = os.environ.get("SOPHAGENT_SANDBOX_SELFTEST", "on").lower()
    if isolation_expected and mode not in {"off", "warn"}:
        raise RuntimeError(
            "exec 沙箱启动自检失败，且当前环境本应启用隔离——拒绝启动。"
            "设 SOPHAGENT_SANDBOX_SELFTEST=warn 可降级为告警。详情：" + msg
        )


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
    _harden_data_dir(cfg)
    from .tools.sandbox import exec_credentials, sandbox_status
    status = sandbox_status()
    # 容器内(root)却拿不到降权凭据 = sandbox 用户缺失，属配置异常 → 告警；
    # 本地非 root 无降权属预期，按 landlock 是否降级决定级别。
    misconfig = os.geteuid() == 0 and exec_credentials() is None
    (log.warning if ("degraded" in status or misconfig) else log.info)(status)
    await _run_exec_selftest(cfg)
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

    # IM 网关：凭据现场可配（DB settings），controller 管各平台生命周期
    from .im.controller import IMController
    from .im.driver import IMDriver
    from .im.transport import BufferedSendTransport, TelegramTransport

    def _im_transport_factory(ev, client):
        if ev.platform == "weixin":
            from .im.platforms.weixin import WeixinBufferedTransport
            return WeixinBufferedTransport(ev.chat_id, client)
        if ev.platform in {"qqbot", "feishu"}:
            return BufferedSendTransport(ev.chat_id, client)
        if ev.platform == "dingtalk":
            from .im.platforms.dingtalk.transport import DingTalkBufferedTransport
            on_complete = getattr(client, "fire_turn_complete", None)
            return DingTalkBufferedTransport(ev.chat_id, client, on_turn_complete=on_complete)
        return TelegramTransport(ev.chat_id, client)

    im_driver = IMDriver(
        db=db,
        manager=app.state.manager,
        skill_store=app.state.skill_store,
        transport_factory=_im_transport_factory,
    )
    app.state.im_controller = IMController(
        im_driver,
        allowed_user_ids=cfg.telegram_allowed_user_ids,
        qq_allowed_user_ids=cfg.qq_allowed_user_ids,
        feishu_allowed_user_ids=cfg.feishu_allowed_user_ids,
        weixin_allowed_user_ids=cfg.weixin_allowed_user_ids,
        weixin_dm_policy=cfg.weixin_dm_policy,
        dingtalk_allowed_user_ids=cfg.dingtalk_allowed_user_ids,
        dingtalk_dm_policy=cfg.dingtalk_dm_policy,
        dingtalk_require_mention=cfg.dingtalk_require_mention,
        feishu_require_mention=cfg.feishu_require_mention,
    )
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
