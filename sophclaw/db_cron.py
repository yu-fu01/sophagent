"""Cron job database operations."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def create_cron_job(
    db: Any,
    user_id: int,
    agent_id: int,
    prompt: str,
    schedule_json: dict,
    schedule_display: str,
    name: str = "",
    repeat_times: int | None = None,
    session_id: str | None = None,
) -> dict:
    """Create a new cron job. Returns the created job dict."""
    job_id = uuid.uuid4().hex[:12]
    now = _now()

    from .cron.schedule import compute_next_run
    next_run_at = compute_next_run(schedule_json)

    await db._exec(
        """INSERT INTO cron_jobs
           (id, user_id, agent_id, name, prompt, schedule_json, schedule_display,
            repeat_times, repeat_completed, enabled, state, next_run_at,
            session_id, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,0,1,'scheduled',?,?,?,?)""",
        (job_id, user_id, agent_id, name, prompt,
         json.dumps(schedule_json), schedule_display,
         repeat_times, next_run_at, session_id, now, now),
    )
    return await get_cron_job(db, job_id)


async def get_cron_job(db: Any, job_id: str) -> Optional[dict]:
    """Get a single cron job by ID."""
    row = await db._one("SELECT * FROM cron_jobs WHERE id=?", (job_id,))
    if row is None:
        return None
    return _row_to_dict(row)


async def list_cron_jobs(
    db: Any,
    user_id: int | None = None,
    include_disabled: bool = False,
) -> list[dict]:
    """List cron jobs, optionally filtered by user."""
    sql = "SELECT * FROM cron_jobs"
    params: list = []
    conditions: list[str] = []

    if user_id is not None:
        conditions.append("user_id=?")
        params.append(user_id)

    if not include_disabled:
        conditions.append("enabled=1")

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    sql += " ORDER BY created_at DESC"
    rows = await db._all(sql, tuple(params))
    return [_row_to_dict(r) for r in rows]


async def update_cron_job(db: Any, job_id: str, updates: dict) -> Optional[dict]:
    """Update a cron job. Only provided fields are changed.
    Returns the updated job, or None if not found."""
    existing = await get_cron_job(db, job_id)
    if existing is None:
        return None

    allowed = {
        "name", "prompt", "schedule_json", "schedule_display",
        "repeat_times", "repeat_completed", "enabled", "state",
        "agent_id", "next_run_at", "last_run_at", "last_status",
        "last_error", "output", "session_id",
    }
    set_parts: list[str] = []
    params: list[Any] = []

    for key in allowed:
        if key in updates:
            val = updates[key]
            if key == "schedule_json" and isinstance(val, dict):
                val = json.dumps(val)
            set_parts.append(f"{key}=?")
            params.append(val)

    if not set_parts:
        return existing  # nothing to update

    now = _now()
    set_parts.append("updated_at=?")
    params.append(now)
    params.append(job_id)

    await db._exec(
        f"UPDATE cron_jobs SET {', '.join(set_parts)} WHERE id=?",
        tuple(params),
    )
    return await get_cron_job(db, job_id)


async def delete_cron_job(db: Any, job_id: str) -> bool:
    """Delete a cron job. Returns True if deleted."""
    cur = await db._exec("DELETE FROM cron_jobs WHERE id=?", (job_id,))
    return cur.rowcount > 0


async def get_due_cron_jobs(db: Any) -> list[dict]:
    """Return all enabled cron jobs whose next_run_at <= now."""
    now = _now()
    rows = await db._all(
        "SELECT * FROM cron_jobs WHERE enabled=1 AND state='scheduled'"
        " AND next_run_at IS NOT NULL AND next_run_at <= ?"
        " ORDER BY next_run_at ASC",
        (now,),
    )
    return [_row_to_dict(r) for r in rows]


async def mark_cron_job_run(
    db: Any,
    job_id: str,
    success: bool,
    error: str | None = None,
    output: str | None = None,
) -> None:
    """Update a cron job after execution: set last_run_at, last_status,
    last_error, output, and compute the next run.
    If repeat_times is reached, the job is disabled."""
    job = await get_cron_job(db, job_id)
    if job is None:
        return

    now = _now()
    schedule = json.loads(job["schedule_json"])
    repeat_completed = (job["repeat_completed"] or 0) + 1
    repeat_times = job.get("repeat_times")

    from .cron.schedule import compute_next_run
    next_run_at = None
    existing_next = job.get("next_run_at")
    if schedule.get("kind") in ("cron", "interval") and existing_next:
        # The ticker pre-advances recurring jobs before execution so a long run
        # is not picked up again by the next tick. Preserve that precomputed
        # next_run_at instead of computing from finish time and drifting/skipping.
        try:
            if datetime.fromisoformat(existing_next) > datetime.fromisoformat(now):
                next_run_at = existing_next
        except ValueError:
            next_run_at = None
    if next_run_at is None:
        next_run_at = compute_next_run(schedule, now)

    # If we've hit the repeat limit, don't compute a next run
    if repeat_times is not None and repeat_completed >= repeat_times:
        next_run_at = None

    sets = {
        "last_run_at": now,
        "last_status": "ok" if success else "error",
        "last_error": error if not success else None,
        "output": output,
        "repeat_completed": repeat_completed,
        "updated_at": now,
    }

    if next_run_at:
        sets["next_run_at"] = next_run_at
        sets["state"] = "scheduled"
    else:
        sets["next_run_at"] = None
        sets["enabled"] = 0
        if success and (
            schedule.get("kind") == "once" or
            (repeat_times is not None and repeat_completed >= repeat_times)
        ):
            sets["state"] = "completed"
        else:
            sets["state"] = "error"

    await update_cron_job(db, job_id, sets)


async def advance_next_run(db: Any, job_id: str) -> bool:
    """Preemptively advance next_run_at before execution (at-most-once)."""
    job = await get_cron_job(db, job_id)
    if job is None:
        return False

    schedule = json.loads(job["schedule_json"])
    kind = schedule.get("kind")

    # One-shot jobs keep their next_run_at (can retry on failure)
    if kind not in ("cron", "interval"):
        return False

    from .cron.schedule import compute_next_run
    new_next = compute_next_run(schedule, _now())
    if new_next and new_next != job.get("next_run_at"):
        await update_cron_job(db, job_id, {"next_run_at": new_next})
        return True
    return False


def _row_to_dict(row: Any) -> dict:
    d = dict(row)
    # Parse schedule_json for convenience
    if isinstance(d.get("schedule_json"), str):
        try:
            d["schedule"] = json.loads(d["schedule_json"])
        except (json.JSONDecodeError, TypeError):
            d["schedule"] = {}
    else:
        d["schedule"] = {}
    return d