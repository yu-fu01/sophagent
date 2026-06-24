"""配对码签发/消费、IM 绑定 CRUD。

db 方法在 sophagent/db.py；本模块是薄封装，集中 IM 语义（platform 常量、
session 创建）供 driver/commands 调用。
"""

from __future__ import annotations

import uuid
from typing import Optional

PLATFORM = "telegram"


async def issue_code(db, user_id: int, agent_id: int) -> str:
    return await db.create_pair_code(user_id, agent_id)


async def consume_code(db, code: str) -> Optional[tuple[int, int]]:
    return await db.consume_pair_code(code)


async def bind(db, chat_id: str, user_id: int, agent_id: int) -> str:
    """为 (chat_id, user, agent) 建新 sophagent session 并写绑定。返回 session_id。
    若已有绑定则覆盖（换 agent 时新建 session）。"""
    agent = await db.get_agent(agent_id)
    group_id = agent["group_id"] if agent else None
    session_id = uuid.uuid4().hex
    await db.create_session(session_id, user_id, agent_id, group_id, title="")
    await db.upsert_binding(PLATFORM, chat_id, user_id, agent_id, session_id)
    return session_id


async def renew_session(db, chat_id: str) -> Optional[str]:
    """为已绑定的 chat 新建一个 session（/new）。返回新 session_id 或 None（未绑定）。"""
    row = await db.get_binding(PLATFORM, chat_id)
    if row is None:
        return None
    return await bind(db, chat_id, row["user_id"], row["agent_id"])


async def lookup(db, chat_id: str):
    """返回绑定 row 或 None。"""
    return await db.get_binding(PLATFORM, chat_id)
