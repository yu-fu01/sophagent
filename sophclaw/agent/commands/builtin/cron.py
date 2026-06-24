"""/cron — manage cron jobs (list, create, pause, resume, delete, trigger, set).

Usage:
  /cron list                        — list all cron jobs
  /cron create <agent_id> <schedule> <prompt> [--name <name>] [--repeat <N>]
  /cron pause   <job_id>           — pause a cron job
  /cron resume  <job_id>           — resume a paused cron job
  /cron delete  <job_id>           — delete a cron job
  /cron trigger <job_id>           — trigger immediate execution
  /cron set     <job_id> <field> <value>  — update a cron job field
"""

from __future__ import annotations

import logging
import shlex

log = logging.getLogger(__name__)

# Subcommand dispatch
SUBCOMMANDS = ("list", "create", "pause", "resume", "delete", "trigger", "set")


async def handle(args: str, ctx: dict) -> dict:
    db = ctx["db"]
    user = ctx["user"]
    session = ctx.get("session")

    try:
        parts = shlex.split(args.strip())
    except ValueError as e:
        return {"content": f"参数解析失败：{e}\n\n{_help_text()}"}
    if not parts:
        return {"content": _help_text()}

    sub = parts[0].lower()
    rest = parts[1:]

    if sub not in SUBCOMMANDS:
        return {"content": f"Unknown subcommand: {sub}\n\n{_help_text()}"}

    if sub == "list":
        return await _cmd_list(db, user)
    elif sub == "create":
        return await _cmd_create(db, user, session, rest)
    elif sub == "pause":
        return await _cmd_pause(db, user, rest)
    elif sub == "resume":
        return await _cmd_resume(db, user, rest)
    elif sub == "delete":
        return await _cmd_delete(db, user, rest)
    elif sub == "trigger":
        return await _cmd_trigger(db, user, rest)
    elif sub == "set":
        return await _cmd_set(db, user, rest)

    return {"content": _help_text()}


# ---------------------------------------------------------------------------
# /cron list
# ---------------------------------------------------------------------------

async def _cmd_list(db, user) -> dict:
    from ....cron.manager import list_jobs
    jobs = await list_jobs(db, user_id=user["id"], include_disabled=True)

    if not jobs:
        return {"content": "No cron jobs found."}

    lines = ["**定时任务列表：**\n"]
    by_state = {"scheduled": [], "paused": [], "completed": [], "error": []}
    for j in jobs:
        st = j.get("state", "scheduled")
        by_state.setdefault(st, []).append(j)

    for state_label in ("scheduled", "paused", "completed", "error"):
        items = by_state.get(state_label, [])
        if not items:
            continue
        emoji = {"scheduled": "🟢", "paused": "⏸", "completed": "✅", "error": "❌"}.get(state_label, "⚪")
        lines.append(f"{emoji} **{state_label.upper()}**")
        for j in items:
            name = j.get("name") or j["id"][:12]
            next_run = j.get("next_run_at") or "-"
            last_status = j.get("last_status") or "-"
            sched = j.get("schedule_display") or "-"
            lines.append(f"  • `{j['id'][:12]}` **{name}** — {sched} | 上次: {last_status} | 下次: {next_run[:16] if next_run != '-' else '-'}")
        lines.append("")

    return {"content": "\n".join(lines)}


# ---------------------------------------------------------------------------
# /cron create <agent_id> <schedule> <prompt> [--name <name>] [--repeat <N>]
# ---------------------------------------------------------------------------

async def _cmd_create(db, user, session, rest) -> dict:
    if len(rest) < 3:
        return {"content": _usage_create()}

    positional: list[str] = []
    name = ""
    repeat_times: int | None = None
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--name":
            if i + 1 >= len(rest):
                return {"content": "Usage: --name <name>"}
            name = rest[i + 1]
            i += 2
        elif token == "--repeat":
            if i + 1 >= len(rest):
                return {"content": "Usage: --repeat <N>"}
            try:
                repeat_times = int(rest[i + 1])
            except ValueError:
                return {"content": f"Invalid repeat count: {rest[i + 1]}"}
            if repeat_times < 1:
                return {"content": "Repeat count must be >= 1."}
            i += 2
        elif token.startswith("--"):
            return {"content": f"Unknown option: {token}"}
        else:
            positional.append(token)
            i += 1

    if len(positional) < 3:
        return {"content": _usage_create()}

    agent_id_str = positional[0]
    if not agent_id_str.isdigit():
        return {"content": f"Invalid agent_id: {agent_id_str}"}
    agent_id = int(agent_id_str)
    schedule_str = positional[1]
    prompt = " ".join(positional[2:]).strip()
    if not prompt:
        return {"content": "Prompt cannot be empty."}

    # Verify agent access
    agent = await db.get_agent(agent_id)
    if agent is None:
        return {"content": f"Agent {agent_id} not found."}

    from ....perms import can_access_group
    if not await can_access_group(db, agent["group_id"], user["id"]):
        return {"content": "You don't have access to this agent."}

    from ....cron.manager import create_job
    try:
        job = await create_job(
            db=db,
            user_id=user["id"],
            agent_id=agent_id,
            prompt=prompt,
            schedule_str=schedule_str,
            name=name,
            repeat_times=repeat_times,
            session_id=session["id"] if session else None,
        )
    except ValueError as e:
        return {"content": f"Failed to create cron job: {e}"}

    name_display = name or job["id"][:12]
    return {
        "content": (
            f"✅ Cron job created: **{name_display}**\n"
            f"  ID: `{job['id']}`\n"
            f"  Agent: {agent_id} | Schedule: {job.get('schedule_display', '')}\n"
            f"  Prompt: {prompt[:80]}{'…' if len(prompt) > 80 else ''}\n"
            f"  session_id: auto-bound to current session"
        ),
    }


# ---------------------------------------------------------------------------
# /cron pause <job_id>
# ---------------------------------------------------------------------------

async def _cmd_pause(db, user, rest) -> dict:
    if not rest:
        return {"content": "Usage: /cron pause <job_id>"}
    job_id = rest[0]

    from ....cron.manager import get_job, pause_job
    existing = await get_job(db, job_id)
    if existing is None:
        return {"content": f"Cron job `{job_id[:12]}` not found."}
    if existing["user_id"] != user["id"]:
        return {"content": "Cron job not found."}

    await pause_job(db, job_id)
    return {"content": f"⏸ Cron job `{job_id[:12]}` paused."}


# ---------------------------------------------------------------------------
# /cron resume <job_id>
# ---------------------------------------------------------------------------

async def _cmd_resume(db, user, rest) -> dict:
    if not rest:
        return {"content": "Usage: /cron resume <job_id>"}
    job_id = rest[0]

    from ....cron.manager import get_job, resume_job
    existing = await get_job(db, job_id)
    if existing is None:
        return {"content": f"Cron job `{job_id[:12]}` not found."}
    if existing["user_id"] != user["id"]:
        return {"content": "Cron job not found."}

    await resume_job(db, job_id)
    return {"content": f"▶️ Cron job `{job_id[:12]}` resumed."}


# ---------------------------------------------------------------------------
# /cron delete <job_id>
# ---------------------------------------------------------------------------

async def _cmd_delete(db, user, rest) -> dict:
    if not rest:
        return {"content": "Usage: /cron delete <job_id>"}
    job_id = rest[0]

    from ....cron.manager import get_job, remove_job
    existing = await get_job(db, job_id)
    if existing is None:
        return {"content": f"Cron job `{job_id[:12]}` not found."}
    if existing["user_id"] != user["id"]:
        return {"content": "Cron job not found."}

    await remove_job(db, job_id)
    return {"content": f"🗑 Cron job `{job_id[:12]}` deleted."}


# ---------------------------------------------------------------------------
# /cron trigger <job_id>
# ---------------------------------------------------------------------------

async def _cmd_trigger(db, user, rest) -> dict:
    if not rest:
        return {"content": "Usage: /cron trigger <job_id>"}
    job_id = rest[0]

    from ....cron.manager import get_job, trigger_job
    existing = await get_job(db, job_id)
    if existing is None:
        return {"content": f"Cron job `{job_id[:12]}` not found."}
    if existing["user_id"] != user["id"]:
        return {"content": "Cron job not found."}

    await trigger_job(db, job_id)
    return {"content": f"🚀 Cron job `{job_id[:12]}` triggered — will run on next tick (within ~60s)."}


# ---------------------------------------------------------------------------
# /cron set <job_id> <field> <value>
# ---------------------------------------------------------------------------

async def _cmd_set(db, user, rest) -> dict:
    if len(rest) < 3:
        return {"content": "Usage: /cron set <job_id> <field> <value>\nFields: name, prompt, schedule, repeat_times"}
    job_id = rest[0]
    field = rest[1].lower()
    value = " ".join(rest[2:])

    from ....cron.manager import get_job, update_job
    existing = await get_job(db, job_id)
    if existing is None:
        return {"content": f"Cron job `{job_id[:12]}` not found."}
    if existing["user_id"] != user["id"]:
        return {"content": "Cron job not found."}

    mapping = {
        "name": "name",
        "prompt": "prompt",
        "schedule": "schedule_str",
        "repeat": "repeat_times",
        "repeat_times": "repeat_times",
    }
    db_field = mapping.get(field)
    if db_field is None:
        return {"content": f"Unknown field: {field}. Supported: name, prompt, schedule, repeat_times"}

    if db_field == "repeat_times":
        try:
            repeat_value = int(value)
        except ValueError:
            return {"content": f"Invalid repeat count: {value}"}
        if repeat_value < 1:
            return {"content": "Repeat count must be >= 1."}
        updates = {db_field: repeat_value}
    else:
        updates = {db_field: value}
    try:
        await update_job(db, job_id, updates)
    except ValueError as e:
        return {"content": f"Failed to update: {e}"}

    return {"content": f"✅ Cron job `{job_id[:12]}` updated: {field} = {value[:60]}{'…' if len(str(value)) > 60 else ''}"}


def _usage_create() -> str:
    return "Usage: /cron create <agent_id> <schedule> <prompt> [--name <name>] [--repeat <N>]"


def _help_text() -> str:
    return (
        "**定时任务命令 (/cron)**\n\n"
        "  • `list` — 列出所有定时任务\n"
        "  • `create <agent_id> <schedule> <prompt> [--name <name>] [--repeat <N>]` — 创建定时任务\n"
        "  • `pause <job_id>` — 暂停任务\n"
        "  • `resume <job_id>` — 恢复任务\n"
        "  • `delete <job_id>` — 删除任务\n"
        "  • `trigger <job_id>` — 手动触发执行\n"
        "  • `set <job_id> <field> <value>` — 修改字段(name/prompt/schedule/repeat_times)\n\n"
        "**调度格式：**\n"
        "  `30m` — 30分钟后执行一次\n"
        "  `every 30m` — 每30分钟执行一次\n"
        "  `0 9 * * *` — 每天9点cron表达式\n"
        "  `2026-06-24T14:00` — 指定时间执行一次\n\n"
        "**示例：**\n"
        "  `/cron create 1 'every 5m' '你好' --name 问候`\n"
        "  `/cron create 1 '0 9 * * *' '生成日报' --name 日报 --repeat 10`\n"
        "  `/cron list`\n"
        "  `/cron pause <job_id>`\n"
        "  `/cron trigger <job_id>`"
    )

