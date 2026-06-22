from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import create_token, hash_password, require_user, verify_password
from ..config import get_config
from ..models import LoginRequest, PasswordChange

router = APIRouter()


@router.get("/auto")
async def auto_login(request: Request):
    """No-login mode: return a token for the default admin without a password."""
    cfg = get_config()
    if not cfg.no_login:
        raise HTTPException(404, "auto login disabled")
    user = await request.app.state.db.get_user_by_username(cfg.admin_username)
    if user is None:
        raise HTTPException(503, "default user not ready")
    is_admin = await request.app.state.db.is_admin(user["id"])
    return {
        "token": create_token(user["id"], user["role"]),
        "role": user["role"],
        "username": user["username"],
        "is_admin": is_admin,
        "no_login": True,
    }


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    user = await request.app.state.db.get_user_by_username(req.username)
    if user is None or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "invalid username or password")
    return {"token": create_token(user["id"], user["role"]), "role": user["role"], "username": user["username"]}


@router.get("/me")
async def me(request: Request, user=Depends(require_user)):
    is_admin = await request.app.state.db.is_admin(user["id"])
    return {"id": user["id"], "username": user["username"], "role": user["role"], "is_admin": is_admin}


@router.post("/password")
async def change_password(req: PasswordChange, request: Request, user=Depends(require_user)):
    if not verify_password(req.old_password, user["password_hash"]):
        raise HTTPException(403, "old password incorrect")
    await request.app.state.db.update_user(user["id"], password_hash=hash_password(req.new_password))
    return {"ok": True}
