"""Create scheduled reminders / cron jobs from natural language.

The model is expected to translate the user's wording ("每天早上9点提醒我喝水",
"30分钟后提醒我开会", "每整点提醒我运动") into a `schedule` string that
`cron.schedule.parse_schedule` accepts, then call this tool. The created job
runs through the normal cron ticker and its result is appended to the
current session (so it surfaces as a chat message).
"""

from __future__ import annotations

from .registry import ToolContext, tool


@tool(
    "schedule_reminder",
    "Schedule a reminder / recurring task. Use this whenever the user asks to "
    "be reminded or to run something on a schedule (e.g. '每天9点提醒我…', "
    "'30分钟后…', '每整点…', '每周一…'). Translate their wording into a "
    "`schedule` (see formats below) and a `prompt` describing what to do at fire "
    "time, then call this tool — do not merely reply '好的'. The reminder fires "
    "through the cron ticker and the result appears as a chat message in the "
    "current session.",
    {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "What to run / say when the reminder fires. "
                "Write it as an instruction to yourself, e.g. '提醒用户：该喝水了'.",
            },
            "schedule": {
                "type": "string",
                "description": (
                    "When to fire. MUST be one of these formats: "
                    "relative one-shot '30m'/'2h'/'1d' (in N minutes/hours/days); "
                    "ISO timestamp '2026-02-03T14:00:00' (fire once at a specific time); "
                    "recurring interval 'every 30m'/'every 2h'; "
                    "cron expression with 5 fields 'min hour day month weekday' "
                    "(e.g. '0 9 * * *' daily at 09:00, '0 * * * *' every top of the hour, "
                    "'15 * * * *' at minute 15 of every hour, "
                    "'0 9 * * 1' every Monday 09:00). "
                    "Map common Chinese phrasings: 每天9点→'0 9 * * *', 每整点→'0 * * * *', "
                    "每小时的15分/每小时第15分钟→'15 * * * *', 每15分钟/每隔15分钟→'every 15m', "
                    "每隔2小时→'every 2h', 30分钟后→'30m', 每周一9点→'0 9 * * 1'."
                ),
            },
            "name": {
                "type": "string",
                "description": "Optional short label for the reminder.",
            },
            "repeat": {
                "type": "integer",
                "description": "How many times to run (for recurring schedules). "
                "Omit to repeat indefinitely. One-shot schedules auto-fire once.",
            },
        },
        "required": ["prompt", "schedule"],
    },
)
async def schedule_reminder(
    ctx: ToolContext,
    prompt: str,
    schedule: str,
    name: str = "",
    repeat: int | None = None,
) -> str:
    db = ctx.db
    if db is None:
        return "Error: scheduling unavailable (no database)"
    prompt = (prompt or "").strip()
    if not prompt:
        return "Error: prompt is required"
    if not (schedule or "").strip():
        return "Error: schedule is required"

    from ..cron.manager import create_job

    try:
        job = await create_job(
            db=db,
            user_id=ctx.user_id,
            agent_id=ctx.agent.id,
            prompt=prompt,
            schedule_str=schedule,
            name=name,
            repeat_times=repeat,
            session_id=ctx.session_id,
        )
    except ValueError as e:
        # Bad schedule — return the supported formats so the model can retry.
        return (
            f"Error: invalid schedule {schedule!r}: {e}\n"
            "Use one of: '30m'/'2h'/'1d' (one-shot after N), "
            "'every 30m'/'every 2h' (recurring), "
            "'0 9 * * *' (cron expression), "
            "'2026-02-03T14:00:00' (ISO timestamp one-shot)."
        )

    label = name or (job.get("id", "")[:12] or "reminder")
    display = job.get("schedule_display", schedule)
    bound = "结果将出现在当前会话中。" if ctx.session_id else "未绑定会话。"
    confirm_text = f"已设置提醒「{label}」：将在{display}提醒你：{prompt}"
    return (
        f"✅ 已创建定时任务「{label}」\n"
        f"  触发时间：{display}\n"
        f"  原始规则：{schedule}\n"
        f"  内容：{prompt[:80]}{'…' if len(prompt) > 80 else ''}\n"
        f"  {bound}\n"
        f"  建议确认文案：{confirm_text}"
    )
