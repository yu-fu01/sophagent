import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..agent.turn import pre_submit, run_turns
from ..auth import require_user
from ..models import ChatRequest, SessionCreate, SessionOverridePatch, TruncateRequest
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


@router.post("/{session_id}/stop")
async def stop_session(session_id: str, request: Request, user=Depends(require_user)):
    if await _owned_or_managed(request, session_id, user) is None:
        raise HTTPException(404, "session not found")
    manager = request.app.state.manager
    stopped = manager.stop(session_id)
    manager.clear_queue(session_id)
    return {"stopped": stopped}


@router.post("/{session_id}/truncate")
async def truncate_session(session_id: str, req: TruncateRequest, request: Request,
                           user=Depends(require_user)):
    """Restore / re-edit: delete the given message and everything after it.
    Refused while a turn is running to avoid racing the chat worker."""
    db = request.app.state.db
    manager = request.app.state.manager
    if await _owned_or_managed(request, session_id, user) is None:
        raise HTTPException(404, "session not found")
    if manager.is_busy(session_id):
        raise HTTPException(409, "session is running a turn")
    deleted = await db.truncate_from(session_id, req.message_id)
    await db.touch_session(session_id)
    return {"ok": True, "deleted": deleted}


@router.post("/{session_id}/chat")
async def chat(session_id: str, req: ChatRequest, request: Request, user=Depends(require_user)):
    state = request.app.state
    db, manager = state.db, state.manager
    session = await db.get_session(session_id, user["id"])
    if session is None:
        raise HTTPException(404, "session not found")

    kind, payload = await pre_submit(
        session_id=session_id, content=req.content, db=db, manager=manager,
        skill_store=state.skill_store, user=user,
    )
    if kind == "slash":
        return JSONResponse(payload)
    if kind == "retry":
        return await _start_sse_turn(session_id, None, user, request)
    if kind == "queued":
        return JSONResponse({"queued": True, "position": payload})
    if kind == "queue_full":
        raise HTTPException(429, "消息队列已满（最多 3 条），请等待当前回复完成后再试")
    # kind == "start"
    return await _start_sse_turn(session_id, payload, user, request)


async def _start_sse_turn(
    session_id: str,
    user_input: str | None,
    user: dict,
    request: Request,
) -> StreamingResponse:
    """Start one chat turn (and any queued follow-ups), streaming SSE.

    Pumps the shared :func:`run_turns` event stream into an :class:`asyncio.Queue`
    that the ``StreamingResponse`` drains. The turn core is identical to the
    WebSocket ``prompt.submit`` path (see :mod:`sophclaw.gateway`)."""
    state = request.app.state
    db, manager = state.db, state.manager
    queue: asyncio.Queue = asyncio.Queue()

    async def _worker():
        try:
            async for ev in run_turns(
                session_id=session_id, user_input=user_input,
                db=db, manager=manager, skill_store=state.skill_store,
                user_id=user["id"],
            ):
                await queue.put(ev)
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(_worker())
    manager.register_task(session_id, task)

    async def sse():
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})