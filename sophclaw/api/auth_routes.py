from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import create_token, hash_password, require_user, verify_password
from ..models import LoginRequest, PasswordChange

router = APIRouter()


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    user = await request.app.state.db.get_user_by_username(req.username)
    if user is None or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "invalid username or password")
    return {"token": create_token(user["id"], user["role"]), "role": user["role"], "username": user["username"]}


@router.get("/me")
async def me(user=Depends(require_user)):
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


@router.post("/password")
async def change_password(req: PasswordChange, request: Request, user=Depends(require_user)):
    if not verify_password(req.old_password, user["password_hash"]):
        raise HTTPException(403, "old password incorrect")
    await request.app.state.db.update_user(user["id"], password_hash=hash_password(req.new_password))
    return {"ok": True}
