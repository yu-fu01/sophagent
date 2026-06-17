"""文件管理 REST API，作用域恒为调用者的每用户工作目录。

REQ2.1（目录树/查看）、REQ2.2（上传→引用进对话）、REQ2.3（所有登录用户可用，
细化权限后期再做）。对标 hermes-agent 的 /api/files/*。所有路径经
tools.files.safe_path() 收敛在工作目录内。"""

from __future__ import annotations

import base64
import binascii
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_user
from ..config import get_config
from ..tools.files import MAX_READ_BYTES, safe_path

router = APIRouter()


def _root(user) -> Path:
    return get_config().workspace_for(user["id"]).resolve()


def _safe(root: Path, p: str) -> Path:
    """safe_path 但把越界的 ValueError 转成 400。"""
    try:
        return safe_path(root, p)
    except ValueError:
        raise HTTPException(400, "invalid path")


def _entry(root: Path, p: Path) -> dict:
    st = p.stat()
    is_dir = p.is_dir()
    return {
        "name": p.name,
        "path": str(p.relative_to(root)),
        "is_dir": is_dir,
        "size": None if is_dir else st.st_size,
        "mtime": st.st_mtime,
    }


class UploadBody(BaseModel):
    path: str
    data_url: str
    overwrite: bool = False


def _decode_data_url(data_url: str, limit: int) -> bytes:
    text = (data_url or "").strip()
    if not text.startswith("data:") or "," not in text:
        raise HTTPException(400, "payload must be a data URL")
    header, encoded = text.split(",", 1)
    if ";base64" not in header:
        raise HTTPException(400, "payload must be base64 encoded")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "payload is not valid base64")
    if len(data) > limit:
        raise HTTPException(413, "file too large")
    return data


def _dedupe(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    i = 1
    while True:
        cand = target.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
        i += 1


@router.get("")
async def list_files(request: Request, path: str = "", user=Depends(require_user)):
    root = _root(user)
    target = _safe(root, path or ".")
    if not target.exists():
        raise HTTPException(404, "path not found")
    if not target.is_dir():
        raise HTTPException(400, "path is not a directory")
    entries = sorted(
        (_entry(root, c) for c in target.iterdir()),
        key=lambda e: (not e["is_dir"], e["name"].lower()),
    )
    rel = "" if target == root else str(target.relative_to(root))
    parent = None if target == root else str(target.parent.relative_to(root))
    return {"path": rel, "parent": parent, "entries": entries}


@router.get("/read")
async def read_file_api(request: Request, path: str, user=Depends(require_user)):
    root = _root(user)
    target = _safe(root, path)
    if not target.is_file():
        raise HTTPException(404, "file not found")
    cfg = get_config()
    size = target.stat().st_size
    if size > cfg.max_upload_bytes:
        raise HTTPException(413, "file too large")
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    data = target.read_bytes()
    if size <= MAX_READ_BYTES:
        try:
            return {"name": target.name, "path": path, "size": size, "mime": mime,
                    "content": data.decode("utf-8")}
        except UnicodeDecodeError:
            pass
    encoded = base64.b64encode(data).decode("ascii")
    return {"name": target.name, "path": path, "size": size, "mime": mime,
            "data_url": f"data:{mime};base64,{encoded}"}


@router.post("/upload")
async def upload_file_api(body: UploadBody, request: Request, user=Depends(require_user)):
    root = _root(user)
    cfg = get_config()
    data = _decode_data_url(body.data_url, cfg.max_upload_bytes)
    name = Path(body.path).name  # 只取文件名：上传一律落工作目录根
    if not name:
        raise HTTPException(400, "invalid filename")
    target = _safe(root, name)
    if target.exists() and target.is_dir():
        raise HTTPException(409, "a directory already exists at that path")
    if target.exists() and not body.overwrite:
        target = _dedupe(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"ok": True, "entry": _entry(root, target)}
