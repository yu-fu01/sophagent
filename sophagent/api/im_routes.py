"""IM 绑定 REST：sophagent 用户签发配对码（供 Telegram 端 /pair 用）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_user
from ..im import pairing
from ..perms import can_access_group

router = APIRouter()


class PairCodeRequest(BaseModel):
    agent_id: int


@router.post("/pair-code")
async def issue_pair_code(req: PairCodeRequest, request: Request, user=Depends(require_user)):
    db = request.app.state.db
    agent = await db.get_agent(req.agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    # 仅 agent 所属 group 的成员/owner 可签发（与 session chat 访问模型一致）
    if not await can_access_group(db, agent["group_id"], user["id"]):
        raise HTTPException(403, "cannot use this agent")
    code = await pairing.issue_code(db, user["id"], req.agent_id)
    return {"code": code, "expires_in": 600}
