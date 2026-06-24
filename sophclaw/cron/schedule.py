"""Schedule parsing and next-run computation for cron jobs.

Port of hermes-agent's cron/jobs.py schedule parsing, adapted for sophclaw.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    from croniter import croniter
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False

# One-shot grace window: jobs created a few seconds after their requested
# minute still run on the next tick.
ONESHOT_GRACE_SECONDS = 120


def now() -> datetime:
    """Return the current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


def parse_duration(s: str) -> int:
    """Parse a duration string into minutes.

    Examples:
        "30m" -> 30
        "2h" -> 120
        "1d" -> 1440
    """
    s = s.strip().lower()
    match = re.match(
        r"^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$",
        s,
    )
    if not match:
        raise ValueError(
            f"Invalid duration: '{s}'. Use format like '30m', '2h', or '1d'"
        )
    value = int(match.group(1))
    unit = match.group(2)[0]  # First char: m, h, or d
    multipliers = {"m": 1, "h": 60, "d": 1440}
    return value * multipliers[unit]


def parse_schedule(schedule: str) -> dict[str, Any]:
    """Parse a schedule string into structured format.

    Returns dict with:
        - kind: "once" | "interval" | "cron"
        - For "once": "run_at" (ISO timestamp)
        - For "interval": "minutes" (int)
        - For "cron": "expr" (cron expression)

    Examples:
        "30m"              -> once in 30 minutes
        "2h"               -> once in 2 hours
        "every 30m"        -> recurring every 30 minutes
        "every 2h"         -> recurring every 2 hours
        "0 9 * * *"        -> cron expression
        "2026-02-03T14:00" -> once at timestamp
    """
    schedule = schedule.strip()
    original = schedule
    schedule_lower = schedule.lower()

    # "every X" pattern -> recurring interval
    if schedule_lower.startswith("every "):
        duration_str = schedule[6:].strip()
        minutes = parse_duration(duration_str)
        return {"kind": "interval", "minutes": minutes, "display": f"every {minutes}m"}

    # Check for cron expression (5 or 6 space-separated fields)
    parts = schedule.split()
    if len(parts) >= 5 and all(
        re.match(r"^[\d\*\-,/]+$", p) for p in parts[:5]
    ):
        if not HAS_CRONITER:
            raise ValueError(
                "Cron expressions require 'croniter' package. "
                "Install with: pip install croniter"
            )
        try:
            croniter(schedule)
        except Exception as e:
            raise ValueError(f"Invalid cron expression '{schedule}': {e}")
        return {"kind": "cron", "expr": schedule, "display": describe_cron(schedule)}

    # ISO timestamp (contains T or looks like date)
    if "T" in schedule or re.match(r"^\d{4}-\d{2}-\d{2}", schedule):
        try:
            dt = datetime.fromisoformat(schedule.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return {
                "kind": "once",
                "run_at": dt.isoformat(),
                "display": f"once at {dt.strftime('%Y-%m-%d %H:%M')}",
            }
        except ValueError as e:
            raise ValueError(f"Invalid timestamp '{schedule}': {e}")

    # Duration like "30m", "2h", "1d" -> one-shot from now
    try:
        minutes = parse_duration(schedule)
        run_at = now() + timedelta(minutes=minutes)
        return {
            "kind": "once",
            "run_at": run_at.isoformat(),
            "display": f"once in {original}",
        }
    except ValueError:
        pass

    raise ValueError(
        f"Invalid schedule '{original}'. Use:\n"
        f"  - Duration: '30m', '2h', '1d' (one-shot)\n"
        f"  - Interval: 'every 30m', 'every 2h' (recurring)\n"
        f"  - Cron: '0 9 * * *' (cron expression)\n"
        f"  - Timestamp: '2026-02-03T14:00:00' (one-shot at time)"
    )


def compute_next_run(
    schedule: dict[str, Any],
    last_run_at: Optional[str] = None,
) -> Optional[str]:
    """Compute the next run time for a schedule.

    Returns ISO timestamp string, or None if no more runs.
    """
    current = now()

    if schedule["kind"] == "once":
        return _recoverable_oneshot_run_at(schedule, current, last_run_at=last_run_at)

    elif schedule["kind"] == "interval":
        minutes = schedule["minutes"]
        if last_run_at:
            last = _ensure_aware(datetime.fromisoformat(last_run_at))
            next_run = last + timedelta(minutes=minutes)
        else:
            next_run = current + timedelta(minutes=minutes)
        return next_run.isoformat()

    elif schedule["kind"] == "cron":
        if not HAS_CRONITER:
            logger.warning(
                "Cannot compute next run for cron schedule %r: "
                "'croniter' is not installed.",
                schedule.get("expr"),
            )
            return None
        base_time = current
        if last_run_at:
            base_time = _ensure_aware(datetime.fromisoformat(last_run_at))
        cron = croniter(schedule["expr"], base_time)
        next_run = cron.get_next(datetime)
        return next_run.isoformat()

    return None


def _recoverable_oneshot_run_at(
    schedule: dict[str, Any],
    current: datetime,
    *,
    last_run_at: Optional[str] = None,
) -> Optional[str]:
    """Return a one-shot run time if it is still eligible to fire."""
    if schedule.get("kind") != "once":
        return None
    if last_run_at:
        return None

    run_at = schedule.get("run_at")
    if not run_at:
        return None

    run_at_dt = _ensure_aware(datetime.fromisoformat(run_at))
    if run_at_dt >= current - timedelta(seconds=ONESHOT_GRACE_SECONDS):
        return run_at
    return None


def _ensure_aware(dt: datetime) -> datetime:
    """Return a timezone-aware datetime (default to UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def describe_cron(expr: str) -> str:
    """Return a concise Chinese description for common 5-field cron expressions."""
    parts = expr.split()
    if len(parts) != 5:
        return expr
    minute, hour, day, month, weekday = parts
    if day == "*" and month == "*" and weekday == "*":
        if hour == "*":
            if minute == "0":
                return "每小时整点"
            if minute.isdigit():
                return f"每小时第 {int(minute)} 分钟"
        if minute.isdigit() and hour.isdigit():
            return f"每天 {int(hour):02d}:{int(minute):02d}"
    if day == "*" and month == "*" and minute.isdigit() and hour.isdigit() and weekday != "*":
        weekday_names = {
            "0": "周日",
            "1": "周一",
            "2": "周二",
            "3": "周三",
            "4": "周四",
            "5": "周五",
            "6": "周六",
            "7": "周日",
        }
        if weekday in weekday_names:
            return f"每{weekday_names[weekday]} {int(hour):02d}:{int(minute):02d}"
    return f"cron: {expr}"