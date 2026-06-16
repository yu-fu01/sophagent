import shutil

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import hash_password, require_admin
from ..config import get_config
from ..models import UserCreate, UserPatch

router = APIRouter()


@router.get("")
async def list_users(request: Request, _admin=Depends(require_admin)):
    rows = await request.app.state.db.list_users()
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_user(req: UserCreate, request: Request, _admin=Depends(require_admin)):
    db = request.app.state.db
    try:
        user_id = await db.create_user(req.username, hash_password(req.password), "user")
    except aiosqlite.IntegrityError:
        raise HTTPException(409, "username already exists")
    await db.create_personal_group(user_id, req.username)  # REQ1.7: own group, owner
    return {"id": user_id, "username": req.username, "role": "user"}


@router.patch("/{user_id}")
async def patch_user(user_id: int, req: UserPatch, request: Request, admin=Depends(require_admin)):
    db = request.app.state.db
    if await db.get_user(user_id) is None:
        raise HTTPException(404, "user not found")
    if req.role == "user" and user_id == admin["id"] and await db.count_admins() == 1:
        raise HTTPException(400, "cannot demote the last admin")
    await db.update_user(
        user_id,
        password_hash=hash_password(req.password) if req.password else None,
        role=req.role,
    )
    return {"ok": True}


@router.delete("/{user_id}")
async def delete_user(user_id: int, request: Request, admin=Depends(require_admin)):
    db = request.app.state.db
    target = await db.get_user(user_id)
    if target is None:
        raise HTTPException(404, "user not found")
    if target["role"] == "admin" and await db.count_admins() == 1:
        raise HTTPException(400, "cannot delete the last admin")
    await db.delete_user(user_id)  # sessions/messages/memory cascade
    shutil.rmtree(get_config().workspaces_dir / str(user_id), ignore_errors=True)
    return {"ok": True}
