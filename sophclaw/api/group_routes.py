"""Group membership, ownership, permissions, join requests and invitations."""

from fastapi import APIRouter, Depends, Request

from ..auth import require_user

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
