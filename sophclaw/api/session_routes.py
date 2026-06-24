import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..agent.runtime import build_runner
from ..auth import require_user
from ..models import AgentDef, ChatRequest, Message, SessionCreate, SessionOverridePatch, TruncateRequest
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

    # ── Slash command dispatch ──────────────────────────────────────────
    if req.content and req.content.startswith("/"):
        from ..agent.commands import dispatch_command
        result = await dispatch_command(
            req.content, db, session, user, manager, skill_store=state.skill_store,
        )
        if result["handled"]:
            # 清空队列的命令：undo / retry / clear
            cmd_name = req.content.lstrip("/").split(maxsplit=1)[0].lower()
            if cmd_name in ("undo", "retry", "clear", "new"):
                manager.clear_queue(session_id)
            # retry_stream: truncate 已完成，直接用用户原文发起流式回复
            if result.get("action") == "retry_stream":
                return await _start_chat_turn(session_id, None, user, request)
            # 持久化用户命令和 assistant 回复，这样 openSession 能看到它们
            await db.append_messages(session_id, [
                Message(role="user", content=req.content),
                Message(role="assistant", content=result.get("content", "")),
            ])
            await db.touch_session(session_id)
            return JSONResponse(result)

    if manager.is_busy(session_id):
        # ── 模型正在回复，消息入队（最多 3 条） ──────────────────────
        if not manager.try_put(session_id, req.content):
            raise HTTPException(429, "消息队列已满（最多 3 条），请等待当前回复完成后再试")
        await db.touch_session(session_id)
        return JSONResponse({"queued": True, "position": manager.pending_count(session_id)})

    return await _start_chat_turn(session_id, req.content, user, request)


async def _start_chat_turn(
    session_id: str,
    user_input: str | None,
    user: dict,
    request: Request,
) -> StreamingResponse:
    """Start one chat turn, streaming SSE. After finishing, auto-drain queued messages."""
    state = request.app.state
    db, manager = state.db, state.manager

    queue: asyncio.Queue = asyncio.Queue()

    async def _run_one_turn(input_text: str | None) -> bool:
        """Run one turn. Returns True if more queued messages follow.
        When True, the next message is already drained: use _chain to
        store it as the next iteration's input_text."""
        session = await db.get_session(session_id, user["id"])
        if session is None:
            await queue.put({"type": "error", "message": "session was deleted"})
            return False

        try:
            async with manager.semaphore:
                history = await db.load_messages(session_id)

                async def persist(msgs):
                    await db.append_messages(session_id, msgs)

                agent_row = await db.get_agent(session["agent_id"])
                if agent_row is None:
                    await queue.put({"type": "error", "message": "agent definition was deleted"})
                    return False
                agent = AgentDef.from_row(agent_row)

                runner = await build_runner(
                    db=db, skill_store=state.skill_store, agent=agent,
                    user_id=user["id"], history=history, on_persist=persist,
                    override_provider=session["override_provider"],
                    override_model=session["override_model"],
                    thinking_mode=session["thinking_mode"],
                    session_id=session_id,
                )
                try:
                    async for ev in runner.run(input_text):
                        await queue.put(ev)
                except asyncio.CancelledError:
                    await queue.put({"type": "error", "message": "stopped by user"})
                    raise
                finally:
                    if runner.compressed:
                        await db.compact_session(session_id, runner.history)
                    title = None if session["title"] or input_text is None else input_text[:60]
                    await db.touch_session(session_id, title)
        except asyncio.CancelledError:
            return False
        except Exception as e:
            log.exception("chat worker failed")
            await queue.put({"type": "error", "message": str(e)})
        finally:
            pass

        # 检查下一条排队消息（不取走，只报告存在）
        next_msg = manager.drain_queue(session_id)
        if next_msg is not None:
            # 取到了，作为 _chain 循环的下一个 input_text
            # 通过闭包变量传回，避免二次 drain
            _chain._next_input = next_msg
            await queue.put({"type": "queued_next", "content": next_msg})
            return True
        else:
            _chain._next_input = None
            return False

    async def _chain(initial_input: str | None):
        """Chain multiple turns in one SSE stream."""
        _chain._next_input = None
        input_text = initial_input
        try:
            async with manager.lock_for(session_id):
                while True:
                    has_more = await _run_one_turn(input_text)
                    if not has_more:
                        break
                    # _run_one_turn 已经把下一条消息 drain 到 _chain._next_input 了
                    input_text = _chain._next_input
                    if input_text is None:
                        break
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(_chain(user_input))
    manager.register_task(session_id, task)

    async def sse():
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})