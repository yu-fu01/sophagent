"""OpenAI-compatible layer: /v1/chat/completions + /v1/models.

`model` selects an agent definition by name. Requests are stateless: the
incoming message list is replayed as history and the turn runs the full
agent loop (tools included); only the final assistant text is returned.
"""

from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..agent.runtime import build_runner
from ..auth import require_user
from ..models import AgentDef, Message, OpenAIChatRequest

router = APIRouter()


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multimodal parts; keep text only
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


async def _visible_agents(db, user) -> list:
    return await db.list_agents() if await db.is_admin(user["id"]) else await db.list_agents_for_user(user["id"])


@router.get("/models")
async def list_models(request: Request, user=Depends(require_user)):
    rows = await _visible_agents(request.app.state.db, user)
    return {
        "object": "list",
        "data": [{"id": r["name"], "object": "model", "owned_by": "sophagent"} for r in rows],
    }


@router.post("/chat/completions")
async def chat_completions(req: OpenAIChatRequest, request: Request, user=Depends(require_user)):
    state = request.app.state
    # resolve the model name among the caller's visible agents (cross-group: first match)
    row = next((r for r in await _visible_agents(state.db, user) if r["name"] == req.model), None)
    if row is None:
        raise HTTPException(404, f"unknown model (agent) {req.model!r}")
    agent = AgentDef.from_row(row)
    if req.temperature is not None:
        agent.temperature = req.temperature

    history: list[Message] = []
    user_input: str | None = None
    for i, m in enumerate(req.messages):
        text = _content_to_text(m.content)
        if m.role == "system":
            # client system prompt augments the agent's own
            agent.system_prompt += "\n\n## Caller instructions\n" + text
        elif i == len(req.messages) - 1 and m.role == "user":
            user_input = text
        else:
            history.append(Message(role=m.role if m.role in ("user", "assistant") else "user", content=text))
    if user_input is None:
        raise HTTPException(400, "last message must have role 'user'")

    runner = await build_runner(db=state.db, skill_store=state.skill_store,
                                agent=agent, user_id=user["id"], history=history)
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if req.stream:
        async def stream():
            def chunk(delta: dict, finish: str | None = None) -> str:
                return "data: " + json.dumps({
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": req.model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }, ensure_ascii=False) + "\n\n"

            yield chunk({"role": "assistant"})
            async with state.manager.semaphore:
                async for ev in runner.run(user_input):
                    if ev["type"] == "text_delta":
                        yield chunk({"content": ev["text"]})
                    elif ev["type"] == "error":
                        yield chunk({"content": f"\n[error: {ev['message']}]"})
            yield chunk({}, finish="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    text_parts: list[str] = []
    async with state.manager.semaphore:
        async for ev in runner.run(user_input):
            if ev["type"] == "text_delta":
                text_parts.append(ev["text"])
            elif ev["type"] == "error":
                raise HTTPException(502, ev["message"])
    return {
        "id": completion_id, "object": "chat.completion", "created": created, "model": req.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": runner.final_text() or "".join(text_parts)},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": runner.usage["input_tokens"],
            "completion_tokens": runner.usage["output_tokens"],
            "total_tokens": runner.usage["input_tokens"] + runner.usage["output_tokens"],
        },
    }
