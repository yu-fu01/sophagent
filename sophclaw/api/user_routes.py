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


async def _protect_root_admin(db, target_id: int, actor) -> None:
    """No one but the root admin may modify the root admin (REQ1.1 addendum)."""
    root_id = await db.get_root_admin_id()
    if target_id == root_id and actor["id"] != root_id:
        raise HTTPException(403, "only the root admin can modify the root admin")


@router.patch("/{user_id}")
async def patch_user(user_id: int, req: UserPatch, request: Request, admin=Depends(require_admin)):
    db = request.app.state.db
    if await db.get_user(user_id) is None:
        raise HTTPException(404, "user not found")
    await _protect_root_admin(db, user_id, admin)
    if req.password:
        await db.update_user(user_id, password_hash=hash_password(req.password))
    return {"ok": True}


@router.delete("/{user_id}")
async def delete_user(user_id: int, request: Request, admin=Depends(require_admin)):
    db = request.app.state.db
    target = await db.get_user(user_id)
    if target is None:
        raise HTTPException(404, "user not found")
    await _protect_root_admin(db, user_id, admin)  # non-root actor -> 403 before anything else
    if user_id == await db.get_root_admin_id():
        raise HTTPException(400, "cannot delete the root admin")
    await db.delete_user(user_id)  # sessions/messages/memory/owned groups cascade
    shutil.rmtree(get_config().workspaces_dir / str(user_id), ignore_errors=True)
    return {"ok": True}
