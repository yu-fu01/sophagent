import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_user
from ..models import SessionCreate, SessionOverridePatch
from ..perms import can_access_group, can_manage_group

router = APIRouter()


async def _owned_or_managed(request: Request, session_id: str, user):
    """Resolve a session the caller may act on: its creator, or a group manager.
    Returns None if it doesn't exist or the caller has no claim (caller sees 404)."""
    db = request.app.state.db
    session = await db.get_session(session_id)
    if session is None:
        return None
    if session["user_id"] == user["id"]:
        return session
    if await can_manage_group(db, session["group_id"], user["id"]):
        return session
    return None


@router.get("")
async def list_sessions(request: Request, user=Depends(require_user)):
    rows = await request.app.state.db.list_sessions(user["id"])
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_session(req: SessionCreate, request: Request, user=Depends(require_user)):
    db = request.app.state.db
    agent = await db.get_agent(req.agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    if not await can_access_group(db, agent["group_id"], user["id"]):
        raise HTTPException(403, "you cannot use agents in this group")
    session_id = uuid.uuid4().hex
    await db.create_session(session_id, user["id"], req.agent_id, agent["group_id"], req.title)
    return dict(await db.get_session(session_id))


@router.get("/{session_id}")
async def get_session(session_id: str, request: Request, user=Depends(require_user)):
    db = request.app.state.db
    session = await db.get_session(session_id, user["id"])  # content stays creator-private
    if session is None:
        raise HTTPException(404, "session not found")
    messages = await db.load_messages_with_ids(session_id)
    return {**dict(session), "messages": [{"id": mid, **m.to_dict()} for mid, m in messages]}


@router.patch("/{session_id}")
async def patch_session(session_id: str, req: SessionOverridePatch, request: Request,
                        user=Depends(require_user)):
    db = request.app.state.db
    # creator-only：覆盖参数只影响 chat，而 chat 本身就是创建者私有（与 chat 路由访问模型一致）
    if await db.get_session(session_id, user["id"]) is None:
        raise HTTPException(404, "session not found")
    await db.set_session_overrides(session_id, override_provider=req.override_provider,
        override_model=req.override_model, thinking_mode=req.thinking_mode)
    return dict(await db.get_session(session_id, user["id"]))


@router.delete("/{session_id}")
async def delete_session(session_id: str, request: Request, user=Depends(require_user)):
    db = request.app.state.db
    if await _owned_or_managed(request, session_id, user) is None:
        raise HTTPException(404, "session not found")
    request.app.state.manager.stop(session_id)
    request.app.state.manager.clear_queue(session_id)
    await db.delete_session(session_id)
    return {"ok": True}
