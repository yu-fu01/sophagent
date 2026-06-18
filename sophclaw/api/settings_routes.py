"""运行时系统设置。当前仅一项：可调的单文件大小上限 max_upload_bytes。

GET 对所有登录用户开放（前端据此提示/预校验上传）；修改权仅 admin。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_admin, require_user
from ..config import effective_max_upload_bytes, get_config

router = APIRouter()

MIN_UPLOAD_BYTES = 1024                      # 1 KB
MAX_UPLOAD_CEILING = 1024 * 1024 * 1024      # 1 GB 硬顶（base64 整文件入内存，防 OOM）


class MaxUploadBytesBody(BaseModel):
    max_upload_bytes: int


@router.get("")
async def get_settings(request: Request, _user=Depends(require_user)):
    db = request.app.state.db
    return {
        "max_upload_bytes": await effective_max_upload_bytes(db),
        "default_max_upload_bytes": get_config().max_upload_bytes,
        "min_bytes": MIN_UPLOAD_BYTES,
        "max_bytes": MAX_UPLOAD_CEILING,
    }


@router.put("/max_upload_bytes")
async def set_max_upload_bytes(body: MaxUploadBytesBody, request: Request,
                               admin=Depends(require_admin)):
    value = body.max_upload_bytes
    if value < MIN_UPLOAD_BYTES or value > MAX_UPLOAD_CEILING:
        raise HTTPException(400, f"max_upload_bytes must be between {MIN_UPLOAD_BYTES} "
                                 f"and {MAX_UPLOAD_CEILING} bytes")
    await request.app.state.db.set_setting("max_upload_bytes", str(value), admin["id"])
    return {"ok": True}
