"""Group-level authorization helpers (pure async functions over the DB).

Two tiers per the design:
- access  : member of the group (or global admin) -> may read/use the group's agents
- manage  : owner, member with can_manage, or global admin -> may CRUD the group's
            agents and manage other members' sessions
"""

from __future__ import annotations


async def can_access_group(db, gid: int, user_id: int) -> bool:
    if await db.is_admin(user_id):
        return True
    return await db.is_member(gid, user_id)


async def can_manage_group(db, gid: int, user_id: int) -> bool:
    if await db.is_admin(user_id):
        return True
    group = await db.get_group(gid)
    if group is None:
        return False
    if group["owner_id"] == user_id:
        return True
    member = await db.get_member(gid, user_id)
    return bool(member and member["can_manage"])


async def can_admin_group(db, gid: int, user_id: int) -> bool:
    """Manage membership and permissions: owner or global admin only
    (a can_manage member governs agents/sessions, not the roster)."""
    if await db.is_admin(user_id):
        return True
    group = await db.get_group(gid)
    return group is not None and group["owner_id"] == user_id
