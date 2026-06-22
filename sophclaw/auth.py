"""Password hashing (bcrypt), JWT tokens, FastAPI auth dependencies."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_config

_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def create_token(user_id: int, role: str) -> str:
    cfg = get_config()
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=cfg.token_ttl_hours),
    }
    return jwt.encode(payload, cfg.secret, algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, get_config().secret, algorithms=["HS256"])


async def _default_user(request: Request) -> Any | None:
    """In no-login mode, use the bootstrap admin as the implicit identity."""
    cfg = get_config()
    if not cfg.no_login:
        return None
    return await request.app.state.db.get_user_by_username(cfg.admin_username)


async def require_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Any:
    """Resolve the JWT to a live user row (revoked/deleted users fail here)."""
    if creds is None:
        user = await _default_user(request)
        if user is not None:
            return user
        raise HTTPException(401, "missing bearer token")
    try:
        payload = decode_token(creds.credentials)
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid or expired token")
    user = await request.app.state.db.get_user(int(payload["sub"]))
    if user is None:
        raise HTTPException(401, "user no longer exists")
    return user


async def require_admin(
    request: Request,
    user: Any = Depends(require_user),
) -> Any:
    """Admin privilege is derived from admin-group membership (REQ1.8)."""
    if not await request.app.state.db.is_admin(user["id"]):
        raise HTTPException(403, "admin privileges required")
    return user
