"""运行时系统设置。当前仅一项：可调的单文件大小上限 max_upload_bytes。

GET 对所有登录用户开放（前端据此提示/预校验上传）；修改权仅 admin。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_admin, require_user
from ..config import (effective_max_upload_bytes, effective_compress_threshold,
                      effective_feishu_config, effective_qq_config, effective_telegram_token,
                      effective_write_approval,
                      get_config, COMPRESS_THRESHOLD_MIN, COMPRESS_THRESHOLD_MAX,
                      DEFAULT_COMPRESS_THRESHOLD)

router = APIRouter()

MIN_UPLOAD_BYTES = 1024                      # 1 KB
MAX_UPLOAD_CEILING = 1024 * 1024 * 1024      # 1 GB 硬顶（base64 整文件入内存，防 OOM）


class MaxUploadBytesBody(BaseModel):
    max_upload_bytes: int


class CompressThresholdBody(BaseModel):
    compress_threshold: float


class WriteApprovalBody(BaseModel):
    write_approval: bool


@router.get("")
async def get_settings(request: Request, _user=Depends(require_user)):
    db = request.app.state.db
    return {
        "max_upload_bytes": await effective_max_upload_bytes(db),
        "default_max_upload_bytes": get_config().max_upload_bytes,
        "min_bytes": MIN_UPLOAD_BYTES,
        "max_bytes": MAX_UPLOAD_CEILING,
        "compress_threshold": await effective_compress_threshold(db),
        "default_compress_threshold": DEFAULT_COMPRESS_THRESHOLD,
        "compress_threshold_min": COMPRESS_THRESHOLD_MIN,
        "compress_threshold_max": COMPRESS_THRESHOLD_MAX,
        "telegram_configured": bool(await effective_telegram_token(db)),
        "qq_configured": all(await effective_qq_config(db)),
        "feishu_configured": bool((cfg := await effective_feishu_config(db)).app_id and cfg.app_secret),
        "write_approval": await effective_write_approval(db),
    }


@router.put("/write_approval")
async def set_write_approval(body: WriteApprovalBody, request: Request,
                             admin=Depends(require_admin)):
    """Toggle the global memory write-approval gate (admin policy)."""
    await request.app.state.db.set_setting(
        "write_approval", "true" if body.write_approval else "false", admin["id"]
    )
    return {"ok": True}


@router.put("/max_upload_bytes")
async def set_max_upload_bytes(body: MaxUploadBytesBody, request: Request,
                               admin=Depends(require_admin)):
    value = body.max_upload_bytes
    if value < MIN_UPLOAD_BYTES or value > MAX_UPLOAD_CEILING:
        raise HTTPException(400, f"max_upload_bytes must be between {MIN_UPLOAD_BYTES} "
                                 f"and {MAX_UPLOAD_CEILING} bytes")
    await request.app.state.db.set_setting("max_upload_bytes", str(value), admin["id"])
    return {"ok": True}


@router.put("/compress_threshold")
async def set_compress_threshold(body: CompressThresholdBody, request: Request,
                                 admin=Depends(require_admin)):
    value = body.compress_threshold
    if value < COMPRESS_THRESHOLD_MIN or value > COMPRESS_THRESHOLD_MAX:
        raise HTTPException(400, f"compress_threshold must be between "
                                 f"{COMPRESS_THRESHOLD_MIN} and {COMPRESS_THRESHOLD_MAX}")
    await request.app.state.db.set_setting("compress_threshold", str(value), admin["id"])
    return {"ok": True}


class TelegramTokenBody(BaseModel):
    token: str


@router.put("/telegram")
async def set_telegram_token(body: TelegramTokenBody, request: Request,
                             admin=Depends(require_admin)):
    token = (body.token or "").strip()
    await request.app.state.db.set_setting("telegram_bot_token", token, admin["id"])
    # 现场生效：停旧 polling、按新 token 起新（空则停）
    await request.app.state.im_controller.restart(request.app.state.db)
    return {"ok": True, "configured": bool(token)}


class QQBotBody(BaseModel):
    app_id: str = ""
    client_secret: str = ""


@router.put("/qqbot")
async def set_qqbot_config(body: QQBotBody, request: Request,
                           admin=Depends(require_admin)):
    app_id = (body.app_id or "").strip()
    client_secret = (body.client_secret or "").strip()
    await request.app.state.db.set_setting("qq_app_id", app_id, admin["id"])
    await request.app.state.db.set_setting("qq_client_secret", client_secret, admin["id"])
    await request.app.state.im_controller.restart(request.app.state.db)
    return {"ok": True, "configured": bool(app_id and client_secret)}


@router.post("/qqbot/test")
async def test_qqbot_config(body: QQBotBody, _admin=Depends(require_admin)):
    app_id = (body.app_id or "").strip()
    client_secret = (body.client_secret or "").strip()
    if not app_id or not client_secret:
        raise HTTPException(400, "app_id and client_secret are required")
    from ..im.platforms.qqbot import QQBotClient
    client = QQBotClient(app_id, client_secret)
    try:
        gateway_url = await client.gateway_url()
    except Exception as e:
        raise HTTPException(400, f"QQ Bot connection test failed: {e}") from e
    finally:
        await client.aclose()
    return {"ok": True, "gateway": bool(gateway_url)}


@router.post("/qqbot/restart")
async def restart_qqbot(request: Request, _admin=Depends(require_admin)):
    request.app.state.im_controller.current_qq_config = None
    await request.app.state.im_controller.restart(request.app.state.db)
    return {"ok": True}


class FeishuBody(BaseModel):
    app_id: str = ""
    app_secret: str = ""
    domain: str = "feishu"
    connection_mode: str = "websocket"
    verification_token: str = ""
    encrypt_key: str = ""


@router.put("/feishu")
async def set_feishu_config(body: FeishuBody, request: Request,
                            admin=Depends(require_admin)):
    app_id = (body.app_id or "").strip()
    app_secret = (body.app_secret or "").strip()
    domain = (body.domain or "feishu").strip() or "feishu"
    connection_mode = (body.connection_mode or "websocket").strip() or "websocket"
    verification_token = (body.verification_token or "").strip()
    encrypt_key = (body.encrypt_key or "").strip()
    db = request.app.state.db
    await db.set_setting("feishu_app_id", app_id, admin["id"])
    await db.set_setting("feishu_app_secret", app_secret, admin["id"])
    await db.set_setting("feishu_domain", domain, admin["id"])
    await db.set_setting("feishu_connection_mode", connection_mode, admin["id"])
    await db.set_setting("feishu_verification_token", verification_token, admin["id"])
    await db.set_setting("feishu_encrypt_key", encrypt_key, admin["id"])
    await request.app.state.im_controller.restart(db)
    return {"ok": True, "configured": bool(app_id and app_secret)}


@router.post("/feishu/test")
async def test_feishu_config(body: FeishuBody, _admin=Depends(require_admin)):
    app_id = (body.app_id or "").strip()
    app_secret = (body.app_secret or "").strip()
    domain = (body.domain or "feishu").strip() or "feishu"
    if not app_id or not app_secret:
        raise HTTPException(400, "app_id and app_secret are required")
    from ..im.platforms.feishu import FEISHU_SETUP_CHECKLIST, FeishuClient
    client = FeishuClient(app_id, app_secret, domain=domain)
    try:
        info = await client.probe()
    except Exception as e:
        raise HTTPException(400, f"Feishu connection test failed: {e}") from e
    finally:
        await client.aclose()
    bot = info.get("bot") or {}
    return {
        "ok": True,
        "bot_name": bot.get("bot_name"),
        "open_id": bot.get("open_id"),
        "setup_checklist": list(FEISHU_SETUP_CHECKLIST),
    }


@router.post("/feishu/restart")
async def restart_feishu(request: Request, _admin=Depends(require_admin)):
    request.app.state.im_controller.current_feishu_config = None
    await request.app.state.im_controller.restart(request.app.state.db)
    return {"ok": True}
