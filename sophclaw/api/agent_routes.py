import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_user
from ..models import AgentCreate, AgentDef
from ..perms import can_access_group, can_manage_group
from ..providers.registry import get_registry
from ..tools.registry import all_tool_names

router = APIRouter()


def _public(row) -> dict:
    a = AgentDef.from_row(row)
    return {
        "id": a.id, "name": a.name, "description": a.description,
        "system_prompt": a.system_prompt, "provider": a.provider, "model": a.model,
        "tools": a.tools, "skills": a.skills,
        "max_iterations": a.max_iterations, "temperature": a.temperature,
        "group_id": a.group_id,
    }


def _validate(req: AgentCreate) -> None:
    names = get_registry().names()
    if req.provider not in names:
        raise HTTPException(400, f"unknown provider {req.provider!r}; configured: {names}")
    bad = set(req.tools) - set(all_tool_names())
    if bad:
        raise HTTPException(400, f"unknown tools: {sorted(bad)}; available: {all_tool_names()}")


async def _manageable_agent(request: Request, agent_id: int, user) -> aiosqlite.Row:
    """Load an agent and assert the caller may manage its group, else 403/404."""
    db = request.app.state.db
    row = await db.get_agent(agent_id)
    if row is None:
        raise HTTPException(404, "agent not found")
    if not await can_manage_group(db, row["group_id"], user["id"]):
        raise HTTPException(403, "you cannot manage agents in this group")
    return row


@router.get("")
async def list_agents(request: Request, user=Depends(require_user)):
    db = request.app.state.db
    rows = await db.list_agents() if await db.is_admin(user["id"]) else await db.list_agents_for_user(user["id"])
    return [_public(r) for r in rows]


@router.get("/meta/options")
async def agent_options(_user=Depends(require_user)):
    """Building blocks for the agent editor."""
    return {"tools": all_tool_names(), "providers": get_registry().names()}


@router.post("", status_code=201)
async def create_agent(req: AgentCreate, request: Request, user=Depends(require_user)):
    db = request.app.state.db
    if req.group_id is not None:
        gid = req.group_id
        if await db.get_group(gid) is None:
            raise HTTPException(404, "group not found")
    else:
        owned = await db.get_owned_group(user["id"])
        if owned is None:
            raise HTTPException(400, "no group to create the agent in")
        gid = owned["id"]
    if not await can_manage_group(db, gid, user["id"]):
        raise HTTPException(403, "you cannot create agents in this group")
    _validate(req)
    try:
        agent_id = await db.create_agent(req.model_dump(exclude={"group_id"}), user["id"], gid)
    except aiosqlite.IntegrityError:
        raise HTTPException(409, "agent name already exists in this group")
    return _public(await db.get_agent(agent_id))


@router.put("/{agent_id}")
async def update_agent(agent_id: int, req: AgentCreate, request: Request, user=Depends(require_user)):
    db = request.app.state.db
    await _manageable_agent(request, agent_id, user)
    _validate(req)
    await db.update_agent(agent_id, req.model_dump(exclude={"group_id"}))
    return _public(await db.get_agent(agent_id))


@router.delete("/{agent_id}")
async def delete_agent(agent_id: int, request: Request, user=Depends(require_user)):
    db = request.app.state.db
    await _manageable_agent(request, agent_id, user)
    try:
        await db.delete_agent(agent_id)
    except aiosqlite.IntegrityError:
        raise HTTPException(409, "agent has sessions; delete them first")
    return {"ok": True}
