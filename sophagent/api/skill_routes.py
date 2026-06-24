from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_admin, require_user
from ..models import SkillWrite
from ..skills.store import SkillError

router = APIRouter()


@router.get("")
async def list_skills(request: Request, _user=Depends(require_user)):
    return request.app.state.skill_store.index()


@router.get("/{name}")
async def view_skill(name: str, request: Request, _user=Depends(require_user)):
    try:
        return {"name": name, "content": request.app.state.skill_store.view(name)}
    except SkillError as e:
        raise HTTPException(404, str(e))


@router.put("/{name}")
async def put_skill(name: str, req: SkillWrite, request: Request, _admin=Depends(require_admin)):
    """Admin review channel: create or overwrite a skill."""
    store = request.app.state.skill_store
    try:
        if (store.root / name / "SKILL.md").is_file():
            store.edit(name, req.content)
        else:
            store.create(name, req.content)
    except SkillError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.delete("/{name}")
async def delete_skill(name: str, request: Request, _admin=Depends(require_admin)):
    try:
        request.app.state.skill_store.delete(name)
    except SkillError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}
