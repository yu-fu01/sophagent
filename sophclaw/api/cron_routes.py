"""REST API endpoints for cron job management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth import require_user

log = logging.getLogger(__name__)
router = APIRouter()


# -- Pydantic schemas ---------------------------------------------------------


class CronJobCreate(BaseModel):
    agent_id: int
    prompt: str = Field(min_length=1)
    schedule: str = Field(min_length=1, description="Schedule: '30m', 'every 30m', '0 9 * * *', or ISO timestamp")
    name: str = ""
    repeat_times: int | None = Field(default=None, ge=1)
    session_id: str | None = None


class CronJobUpdate(BaseModel):
    name: str | None = None
    prompt: str | None = None
    schedule: str | None = None
    agent_id: int | None = None
    repeat_times: int | None = None
    session_id: str | None = None


# -- Handlers -----------------------------------------------------------------


@router.get("")
async def list_cron_jobs(
    request: Request,
    include_disabled: bool = False,
    user=Depends(require_user),
):
    """List all cron jobs visible to the current user."""
    from ..cron.manager import list_jobs

    jobs = await list_jobs(request.app.state.db, user_id=user["id"], include_disabled=include_disabled)

    # Admins see all jobs; regular users only see their own.
    # The list_jobs(user_id=...) already filters by user_id.
    return {"jobs": [_format_job(job) for job in jobs]}


@router.post("", status_code=201)
async def create_cron_job(
    req: CronJobCreate,
    request: Request,
    user=Depends(require_user),
):
    """Create a new cron job."""
    db = request.app.state.db

    # Verify the agent exists and the user has access
    agent = await db.get_agent(req.agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")

    from ..perms import can_access_group
    if not await can_access_group(db, agent["group_id"], user["id"]):
        raise HTTPException(403, "you cannot use this agent")
    if req.session_id is not None and await db.get_session(req.session_id, user["id"]) is None:
        raise HTTPException(404, "session not found")

    from ..cron.manager import create_job

    try:
        job = await create_job(
            db=db,
            user_id=user["id"],
            agent_id=req.agent_id,
            prompt=req.prompt,
            schedule_str=req.schedule,
            name=req.name,
            repeat_times=req.repeat_times,
            session_id=req.session_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {"job": _format_job(job)}


@router.get("/{job_id}")
async def get_cron_job(
    job_id: str,
    request: Request,
    user=Depends(require_user),
):
    """Get a single cron job."""
    db = request.app.state.db
    from ..cron.manager import get_job

    job = await get_job(db, job_id)
    if job is None:
        raise HTTPException(404, "cron job not found")

    # Only the owner can view
    if job["user_id"] != user["id"]:
        from ..perms import is_admin
        if not await is_admin(db, user["id"]):
            raise HTTPException(404, "cron job not found")

    return {"job": _format_job(job)}


@router.patch("/{job_id}")
async def update_cron_job(
    job_id: str,
    req: CronJobUpdate,
    request: Request,
    user=Depends(require_user),
):
    """Update a cron job."""
    db = request.app.state.db
    from ..cron.manager import get_job, update_job

    existing = await get_job(db, job_id)
    if existing is None:
        raise HTTPException(404, "cron job not found")

    if existing["user_id"] != user["id"]:
        from ..perms import is_admin
        if not await is_admin(db, user["id"]):
            raise HTTPException(404, "cron job not found")

    updates: dict = {}
    if req.name is not None:
        updates["name"] = req.name
    if req.prompt is not None:
        updates["prompt"] = req.prompt
    if req.schedule is not None:
        updates["schedule_str"] = req.schedule
    if req.agent_id is not None:
        # Verify the new agent exists and can be used by the caller.
        agent = await db.get_agent(req.agent_id)
        if agent is None:
            raise HTTPException(404, "agent not found")
        from ..perms import can_access_group
        if not await can_access_group(db, agent["group_id"], user["id"]):
            raise HTTPException(403, "you cannot use this agent")
        updates["agent_id"] = req.agent_id
    if req.repeat_times is not None:
        updates["repeat_times"] = req.repeat_times
    if req.session_id is not None:
        if await db.get_session(req.session_id, user["id"]) is None:
            raise HTTPException(404, "session not found")
        updates["session_id"] = req.session_id

    if not updates:
        raise HTTPException(400, "no fields to update")

    try:
        job = await update_job(db, job_id, updates)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {"job": _format_job(job)}


@router.delete("/{job_id}")
async def delete_cron_job(
    job_id: str,
    request: Request,
    user=Depends(require_user),
):
    """Delete a cron job."""
    db = request.app.state.db
    from ..cron.manager import get_job, remove_job

    existing = await get_job(db, job_id)
    if existing is None:
        raise HTTPException(404, "cron job not found")

    if existing["user_id"] != user["id"]:
        from ..perms import is_admin
        if not await is_admin(db, user["id"]):
            raise HTTPException(404, "cron job not found")

    await remove_job(db, job_id)
    return {"ok": True}


@router.post("/{job_id}/pause")
async def pause_cron_job(
    job_id: str,
    request: Request,
    user=Depends(require_user),
):
    """Pause a cron job."""
    db = request.app.state.db
    from ..cron.manager import get_job, pause_job as _pause

    existing = await get_job(db, job_id)
    if existing is None:
        raise HTTPException(404, "cron job not found")
    if existing["user_id"] != user["id"]:
        raise HTTPException(404, "cron job not found")

    job = await _pause(db, job_id)
    if job is None:
        raise HTTPException(404, "cron job not found")
    return {"job": _format_job(job)}


@router.post("/{job_id}/resume")
async def resume_cron_job(
    job_id: str,
    request: Request,
    user=Depends(require_user),
):
    """Resume a paused cron job."""
    db = request.app.state.db
    from ..cron.manager import get_job, resume_job as _resume

    existing = await get_job(db, job_id)
    if existing is None:
        raise HTTPException(404, "cron job not found")
    if existing["user_id"] != user["id"]:
        raise HTTPException(404, "cron job not found")

    job = await _resume(db, job_id)
    if job is None:
        raise HTTPException(404, "cron job not found")
    return {"job": _format_job(job)}


@router.post("/{job_id}/trigger")
async def trigger_cron_job(
    job_id: str,
    request: Request,
    user=Depends(require_user),
):
    """Trigger immediate execution of a cron job."""
    db = request.app.state.db
    from ..cron.manager import get_job, trigger_job as _trigger

    existing = await get_job(db, job_id)
    if existing is None:
        raise HTTPException(404, "cron job not found")
    if existing["user_id"] != user["id"]:
        raise HTTPException(404, "cron job not found")

    job = await _trigger(db, job_id)
    if job is None:
        raise HTTPException(404, "cron job not found")
    return {"job": _format_job(job)}


# -- Format helpers -----------------------------------------------------------


def _format_job(job: dict) -> dict:
    """Format a cron job dict for API output."""
    return {
        "id": job["id"],
        "user_id": job["user_id"],
        "agent_id": job["agent_id"],
        "name": job.get("name", ""),
        "prompt": job.get("prompt", ""),
        "schedule": job.get("schedule", {}),
        "schedule_display": job.get("schedule_display", ""),
        "repeat": {
            "times": job.get("repeat_times"),
            "completed": job.get("repeat_completed", 0),
        },
        "enabled": bool(job.get("enabled", True)),
        "state": job.get("state", "scheduled"),
        "session_id": job.get("session_id"),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "last_error": job.get("last_error"),
        "output": job.get("output"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }