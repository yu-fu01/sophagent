"""Group membership, ownership, permissions, join requests and invitations."""

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_admin, require_user
from ..models import GroupCreate, GroupPatch, MemberAdd, MemberPatch
from ..perms import can_admin_group, can_manage_group

router = APIRouter()


async def my_role(db, group_row, user_id: int) -> str:
    """How `user_id` relates to a group: owner | manager | member | admin | none."""
    if group_row["owner_id"] == user_id:
        return "owner"
    member = await db.get_member(group_row["id"], user_id)
    if member is not None:
        return "manager" if member["can_manage"] else "member"
    return "admin" if await db.is_admin(user_id) else "none"


def _group_dict(row, role: str) -> dict:
    return {
        "id": row["id"], "name": row["name"], "owner_id": row["owner_id"],
        "is_admin_group": row["is_admin_group"], "role": role,
    }


@router.get("")
async def list_groups(request: Request, user=Depends(require_user)):
    db = request.app.state.db
    rows = await db.list_groups() if await db.is_admin(user["id"]) else await db.list_user_groups(user["id"])
    return [_group_dict(r, await my_role(db, r, user["id"])) for r in rows]


@router.post("", status_code=201)
async def create_group(req: GroupCreate, request: Request, _admin=Depends(require_admin)):
    """Admin creates a group (REQ1.2), owned by `owner_id` or the caller."""
    db = request.app.state.db
    owner_id = req.owner_id if req.owner_id is not None else _admin["id"]
    if await db.get_user(owner_id) is None:
        raise HTTPException(404, "owner not found")
    gid = await db.create_group(req.name, owner_id)
    return _group_dict(await db.get_group(gid), await my_role(db, await db.get_group(gid), _admin["id"]))


async def _group_or_404(db, gid: int):
    g = await db.get_group(gid)
    if g is None:
        raise HTTPException(404, "group not found")
    return g


@router.get("/{gid}")
async def get_group(gid: int, request: Request, user=Depends(require_user)):
    db = request.app.state.db
    g = await _group_or_404(db, gid)
    role = await my_role(db, g, user["id"])
    if role == "none":
        raise HTTPException(403, "not a member of this group")
    members = [
        {"user_id": m["user_id"], "username": m["username"],
         "can_manage": m["can_manage"], "is_owner": m["user_id"] == g["owner_id"]}
        for m in await db.list_members(gid)
    ]
    return {**_group_dict(g, role), "members": members}


@router.patch("/{gid}")
async def rename_group(gid: int, req: GroupPatch, request: Request, user=Depends(require_user)):
    db = request.app.state.db
    g = await _group_or_404(db, gid)
    if not await can_admin_group(db, gid, user["id"]):
        raise HTTPException(403, "you cannot manage this group")
    await db.rename_group(gid, req.name)
    return _group_dict(await db.get_group(gid), await my_role(db, g, user["id"]))


@router.delete("/{gid}")
async def delete_group(gid: int, request: Request, _admin=Depends(require_admin)):
    db = request.app.state.db
    g = await _group_or_404(db, gid)
    if g["is_admin_group"]:
        raise HTTPException(400, "cannot delete the admin group")
    primary = await db.get_owned_group(g["owner_id"])  # a user's personal group is their primary
    if primary is not None and primary["id"] == gid:
        raise HTTPException(400, "cannot delete a user's personal group; delete the user instead")
    await db.delete_group(gid)
    return {"ok": True}


@router.get("/{gid}/members")
async def list_members(gid: int, request: Request, user=Depends(require_user)):
    db = request.app.state.db
    g = await _group_or_404(db, gid)
    if await my_role(db, g, user["id"]) == "none":
        raise HTTPException(403, "not a member of this group")
    return [
        {"user_id": m["user_id"], "username": m["username"],
         "can_manage": m["can_manage"], "is_owner": m["user_id"] == g["owner_id"]}
        for m in await db.list_members(gid)
    ]


@router.post("/{gid}/members", status_code=201)
async def add_member(gid: int, req: MemberAdd, request: Request, user=Depends(require_user)):
    """Owner/admin directly adds a member (also the admin-group path, REQ1.8)."""
    db = request.app.state.db
    await _group_or_404(db, gid)
    if not await can_admin_group(db, gid, user["id"]):
        raise HTTPException(403, "you cannot manage this group's members")
    if await db.get_user(req.user_id) is None:
        raise HTTPException(404, "user not found")
    await db.add_member(gid, req.user_id)
    await _sync_if_admin_group(db, gid)
    return {"ok": True}


@router.patch("/{gid}/members/{member_id}")
async def set_member_permission(gid: int, member_id: int, req: MemberPatch,
                                request: Request, user=Depends(require_user)):
    db = request.app.state.db
    g = await _group_or_404(db, gid)
    if not await can_admin_group(db, gid, user["id"]):
        raise HTTPException(403, "you cannot manage this group's members")
    if member_id == g["owner_id"]:
        raise HTTPException(400, "the owner always has manage rights")
    if await db.get_member(gid, member_id) is None:
        raise HTTPException(404, "member not found")
    await db.set_can_manage(gid, member_id, req.can_manage)
    return {"ok": True}


@router.delete("/{gid}/members/{member_id}")
async def remove_member(gid: int, member_id: int, request: Request, user=Depends(require_user)):
    db = request.app.state.db
    g = await _group_or_404(db, gid)
    if not await can_admin_group(db, gid, user["id"]):
        raise HTTPException(403, "you cannot manage this group's members")
    if member_id == g["owner_id"]:
        raise HTTPException(400, "cannot remove the group owner")
    if await db.get_member(gid, member_id) is None:
        raise HTTPException(404, "member not found")
    await db.remove_member(gid, member_id)
    await _sync_if_admin_group(db, gid)
    return {"ok": True}


async def _sync_if_admin_group(db, gid: int) -> None:
    ag = await db.get_admin_group()
    if ag is not None and ag["id"] == gid:
        await db.sync_roles()  # admin-group membership drives users.role


@router.get("/{gid}/sessions")
async def list_group_sessions(gid: int, request: Request, user=Depends(require_user)):
    """All sessions in a group (owner / can_manage / admin)."""
    db = request.app.state.db
    if await db.get_group(gid) is None:
        raise HTTPException(404, "group not found")
    if not await can_manage_group(db, gid, user["id"]):
        raise HTTPException(403, "you cannot manage this group")
    return [dict(r) for r in await db.list_sessions_by_group(gid)]
