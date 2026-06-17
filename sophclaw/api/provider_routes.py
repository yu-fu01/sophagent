from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_user
from ..config import get_config
from ..crypto import encrypt, mask_key
from ..models import ProviderCreate, ProviderPatch
from ..providers.registry import get_registry

router = APIRouter()


async def _require_admin(request: Request, user) -> None:
    if not await request.app.state.db.is_admin(user["id"]):
        raise HTTPException(403, "admin only")


@router.get("")
async def list_providers(request: Request, user=Depends(require_user)):
    await _require_admin(request, user)
    reg = get_registry()
    out = []
    for name in reg.names():
        r = reg.resolve(name)
        out.append({"name": r.name, "api_mode": r.api_mode, "base_url": r.base_url,
                    "api_key": mask_key(r.api_key), "context_limit": r.context_limit,
                    "source": r.source})
    return out


@router.post("", status_code=201)
async def create_provider(req: ProviderCreate, request: Request, user=Depends(require_user)):
    await _require_admin(request, user)
    db = request.app.state.db
    if await db.get_provider(req.name) is not None:
        raise HTTPException(409, "provider name already exists")
    enc = encrypt(req.api_key, get_config().secret) if req.api_key else None
    await db.create_provider({"name": req.name, "api_mode": req.api_mode,
        "base_url": req.base_url, "api_key_enc": enc, "context_limit": req.context_limit})
    await get_registry().refresh()
    return {"ok": True, "name": req.name}


@router.put("/{name}")
async def update_provider(name: str, req: ProviderPatch, request: Request, user=Depends(require_user)):
    await _require_admin(request, user)
    db = request.app.state.db
    if await db.get_provider(name) is None:
        raise HTTPException(400, "cannot modify a non-DB (built-in) or missing provider")
    enc = encrypt(req.api_key, get_config().secret) if req.api_key else None
    await db.update_provider(name, {"api_mode": req.api_mode, "base_url": req.base_url,
        "api_key_enc": enc, "context_limit": req.context_limit})
    await get_registry().refresh()
    return {"ok": True}


@router.delete("/{name}")
async def delete_provider(name: str, request: Request, user=Depends(require_user)):
    await _require_admin(request, user)
    db = request.app.state.db
    if await db.get_provider(name) is None:
        raise HTTPException(400, "cannot delete a non-DB (built-in) or missing provider")
    await db.delete_provider(name)
    await get_registry().refresh()
    return {"ok": True}


@router.get("/{name}/models")
async def list_models(name: str, request: Request, user=Depends(require_user)):
    await _require_admin(request, user)
    try:
        return {"models": await get_registry().list_models(name)}
    except Exception as e:
        return {"models": [], "error": str(e)}
