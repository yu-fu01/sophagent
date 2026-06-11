import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_admin, require_user
from ..config import get_config
from ..models import AgentCreate, AgentDef
from ..tools.registry import all_tool_names

router = APIRouter()


def _public(row) -> dict:
    a = AgentDef.from_row(row)
    return {
        "id": a.id, "name": a.name, "description": a.description,
        "system_prompt": a.system_prompt, "provider": a.provider, "model": a.model,
        "tools": a.tools, "skills": a.skills,
        "max_iterations": a.max_iterations, "temperature": a.temperature,
    }


def _validate(req: AgentCreate) -> None:
    if req.provider not in get_config().providers:
        raise HTTPException(400, f"unknown provider {req.provider!r}; configured: {sorted(get_config().providers)}")
    bad = set(req.tools) - set(all_tool_names())
    if bad:
        raise HTTPException(400, f"unknown tools: {sorted(bad)}; available: {all_tool_names()}")


@router.get("")
async def list_agents(request: Request, _user=Depends(require_user)):
    return [_public(r) for r in await request.app.state.db.list_agents()]


@router.get("/meta/options")
async def agent_options(_admin=Depends(require_admin)):
    """Building blocks for the admin UI."""
    return {"tools": all_tool_names(), "providers": sorted(get_config().providers)}


@router.post("", status_code=201)
async def create_agent(req: AgentCreate, request: Request, admin=Depends(require_admin)):
    _validate(req)
    try:
        agent_id = await request.app.state.db.create_agent(req.model_dump(), admin["id"])
    except aiosqlite.IntegrityError:
        raise HTTPException(409, "agent name already exists")
    return _public(await request.app.state.db.get_agent(agent_id))


@router.put("/{agent_id}")
async def update_agent(agent_id: int, req: AgentCreate, request: Request, _admin=Depends(require_admin)):
    db = request.app.state.db
    if await db.get_agent(agent_id) is None:
        raise HTTPException(404, "agent not found")
    _validate(req)
    await db.update_agent(agent_id, req.model_dump())
    return _public(await db.get_agent(agent_id))


@router.delete("/{agent_id}")
async def delete_agent(agent_id: int, request: Request, _admin=Depends(require_admin)):
    db = request.app.state.db
    if await db.get_agent(agent_id) is None:
        raise HTTPException(404, "agent not found")
    try:
        await db.delete_agent(agent_id)
    except aiosqlite.IntegrityError:
        raise HTTPException(409, "agent has sessions; delete them first")
    return {"ok": True}
