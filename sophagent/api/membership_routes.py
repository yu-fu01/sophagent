"""Cross-cutting join-request / invitation actions (not group-prefixed)."""

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_user
from ..perms import can_admin_group

router = APIRouter()


@router.get("/me/invitations")
async def my_invitations(request: Request, user=Depends(require_user)):
    return [dict(r) for r in await request.app.state.db.list_user_invitations(user["id"])]


@router.post("/joinreq/{join_id}/approve")
async def approve_request(join_id: int, request: Request, user=Depends(require_user)):
    """Owner/admin approves a join *request* -> the applicant becomes a member."""
    db = request.app.state.db
    row = await db.get_join(join_id)
    if row is None or row["kind"] != "request":
        raise HTTPException(404, "request not found")
    if not await can_admin_group(db, row["group_id"], user["id"]):
        raise HTTPException(403, "you cannot manage this group's members")
    await db.add_member(row["group_id"], row["user_id"])
    await db.delete_join(join_id)
    return {"ok": True}


@router.post("/joinreq/{join_id}/accept")
async def accept_invite(join_id: int, request: Request, user=Depends(require_user)):
    """The invited user accepts an *invitation* -> they become a member."""
    db = request.app.state.db
    row = await db.get_join(join_id)
    if row is None or row["kind"] != "invite":
        raise HTTPException(404, "invitation not found")
    if row["user_id"] != user["id"]:
        raise HTTPException(403, "this invitation is not yours")
    await db.add_member(row["group_id"], row["user_id"])
    await db.delete_join(join_id)
    return {"ok": True}


@router.delete("/joinreq/{join_id}")
async def dismiss_join(join_id: int, request: Request, user=Depends(require_user)):
    """Reject/decline/cancel. The target user or a group admin may dismiss it."""
    db = request.app.state.db
    row = await db.get_join(join_id)
    if row is None:
        raise HTTPException(404, "not found")
    is_target = row["user_id"] == user["id"]
    if not is_target and not await can_admin_group(db, row["group_id"], user["id"]):
        raise HTTPException(403, "not allowed")
    await db.delete_join(join_id)
    return {"ok": True}
