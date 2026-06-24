"""Cron job business logic layer.

Bridges DB operations (db_cron) with schedule parsing (cron/schedule.py).
"""

from __future__ import annotations

from typing import Any, Optional

from ..db_cron import (
    create_cron_job as _db_create,
    delete_cron_job as _db_delete,
    get_cron_job as _db_get,
    list_cron_jobs as _db_list,
    update_cron_job as _db_update,
)
from .schedule import parse_schedule, compute_next_run


async def create_job(
    db: Any,
    user_id: int,
    agent_id: int,
    prompt: str,
    schedule_str: str,
    name: str = "",
    repeat_times: int | None = None,
    session_id: str | None = None,
) -> dict:
    """Create a new cron job with parsed schedule."""
    parsed = parse_schedule(schedule_str)
    display = parsed.get("display", schedule_str)

    # Auto-set repeat=1 for one-shot schedules
    if parsed["kind"] == "once" and repeat_times is None:
        repeat_times = 1

    job = await _db_create(
        db=db,
        user_id=user_id,
        agent_id=agent_id,
        prompt=prompt,
        schedule_json=parsed,
        schedule_display=display,
        name=name,
        repeat_times=repeat_times,
        session_id=session_id,
    )
    return job


async def get_job(db: Any, job_id: str) -> Optional[dict]:
    """Get a single cron job."""
    return await _db_get(db, job_id)


async def list_jobs(
    db: Any,
    user_id: int | None = None,
    include_disabled: bool = False,
) -> list[dict]:
    """List cron jobs."""
    return await _db_list(db, user_id=user_id, include_disabled=include_disabled)


async def update_job(db: Any, job_id: str, updates: dict) -> Optional[dict]:
    """Update a cron job. If schedule is provided, re-parse it."""
    if "schedule_str" in updates:
        parsed = parse_schedule(updates["schedule_str"])
        updates["schedule_json"] = parsed
        updates["schedule_display"] = parsed.get("display", updates["schedule_str"])
        del updates["schedule_str"]

        # Recompute next_run_at if the job is enabled and not paused
        job = await _db_get(db, job_id)
        if job and job.get("enabled") and job.get("state") != "paused":
            last_run = job.get("last_run_at")
            updates["next_run_at"] = compute_next_run(parsed, last_run)

    if "enabled" in updates:
        updates["state"] = "scheduled" if updates["enabled"] else "paused"

    return await _db_update(db, job_id, updates)


async def pause_job(db: Any, job_id: str) -> Optional[dict]:
    """Pause a cron job."""
    return await _db_update(db, job_id, {"enabled": 0, "state": "paused"})


async def resume_job(db: Any, job_id: str) -> Optional[dict]:
    """Resume a paused cron job and recompute next_run."""
    job = await _db_get(db, job_id)
    if job is None:
        return None

    from .schedule import compute_next_run

    schedule = job.get("schedule", {})
    next_run = compute_next_run(schedule)

    return await _db_update(db, job_id, {
        "enabled": 1,
        "state": "scheduled",
        "next_run_at": next_run,
    })


async def trigger_job(db: Any, job_id: str) -> Optional[dict]:
    """Schedule a job to run on the next tick."""
    job = await _db_get(db, job_id)
    if job is None:
        return None

    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    return await _db_update(db, job_id, {
        "enabled": 1,
        "state": "scheduled",
        "next_run_at": now,
    })


async def remove_job(db: Any, job_id: str) -> bool:
    """Delete a cron job."""
    return await _db_delete(db, job_id)