import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..agent.runtime import build_runner
from ..auth import require_user
from ..models import AgentDef, ChatRequest, SessionCreate, SessionOverridePatch
from ..perms import can_access_group, can_manage_group

log = logging.getLogger(__name__)
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
    messages = await db.load_messages(session_id)
    return {**dict(session), "messages": [m.to_dict() for m in messages]}


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
    await db.delete_session(session_id)
    return {"ok": True}


@router.post("/{session_id}/stop")
async def stop_session(session_id: str, request: Request, user=Depends(require_user)):
    if await _owned_or_managed(request, session_id, user) is None:
        raise HTTPException(404, "session not found")
    return {"stopped": request.app.state.manager.stop(session_id)}


@router.post("/{session_id}/chat")
async def chat(session_id: str, req: ChatRequest, request: Request, user=Depends(require_user)):
    state = request.app.state
    db, manager = state.db, state.manager
    session = await db.get_session(session_id, user["id"])
    if session is None:
        raise HTTPException(404, "session not found")
    if manager.is_busy(session_id):
        raise HTTPException(409, "session is already running a turn")
    agent_row = await db.get_agent(session["agent_id"])
    if agent_row is None:
        raise HTTPException(410, "agent definition was deleted")
    agent = AgentDef.from_row(agent_row)

    queue: asyncio.Queue = asyncio.Queue()

    async def worker():
        # independent task: client disconnects don't interrupt the turn
        try:
            async with manager.lock_for(session_id), manager.semaphore:
                history = await db.load_messages(session_id)

                async def persist(msgs):
                    await db.append_messages(session_id, msgs)

                runner = await build_runner(
                    db=db, skill_store=state.skill_store, agent=agent,
                    user_id=user["id"], history=history, on_persist=persist,
                    override_provider=session["override_provider"],
                    override_model=session["override_model"],
                    thinking_mode=session["thinking_mode"],
                )
                try:
                    async for ev in runner.run(req.content):
                        await queue.put(ev)
                except asyncio.CancelledError:
                    await queue.put({"type": "error", "message": "stopped by user"})
                    raise
                finally:
                    if runner.compressed:
                        await db.compact_session(session_id, runner.history)
                    title = None if session["title"] else req.content[:60]
                    await db.touch_session(session_id, title)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("chat worker failed")
            await queue.put({"type": "error", "message": str(e)})
        finally:
            await queue.put(None)

    task = asyncio.create_task(worker())
    manager.register_task(session_id, task)

    async def sse():
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
