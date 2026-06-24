"""运行时系统设置。当前仅一项：可调的单文件大小上限 max_upload_bytes。

GET 对所有登录用户开放（前端据此提示/预校验上传）；修改权仅 admin。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_admin, require_user
from ..config import (effective_max_upload_bytes, effective_compress_threshold,
                      effective_telegram_token, effective_write_approval,
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
