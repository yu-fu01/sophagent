"""cron 工具：让 agent 在对话里用自然语言建/管定时任务。

用户说"每天9点提醒我吃饭"→ agent 把它翻成 schedule（``0 9 * * *``）调本工具
create。任务绑定当前 session（ctx.session_id），到期由 CronScheduler 在该
session 产出 output（agent 模式跑一轮 turn；text 模式直接发文本）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ..cron import jobs as cron_jobs
from .registry import ToolContext, tool

_SCHEDULE_HELP = (
    "schedule 接受三种语法："
    "① cron 表达式（5 字段，如 `0 9 * * *`=每天9点、`0 * * * *`=每个整点、`0 9 * * 1-5`=工作日9点、`*/30 * * * *`=每半小时）；"
    "② 固定间隔 `every 30m` / `every 2h` / `every 1d`；"
    "③ 一次性 `30m`(30分钟后) 或绝对时间 `2026-06-25T14:00`。"
)

_MODE_HELP = "mode: agent=到期跑一轮对话产出回复(默认，可调用工具/记忆)；text=到期直接把 prompt 文本当消息发出(不调模型，省 token，适合纯提醒)。"


@tool(
    "cron",
    "创建/管理定时任务（到点在当前会话自动产出 output）。"
    "当用户说「每天X点/每隔/定时/定期/提醒我」等时用它。"
    + _SCHEDULE_HELP + _MODE_HELP,
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "list", "pause", "resume", "delete"]},
            "name": {"type": "string", "description": "任务名（create 必填）"},
            "prompt": {
                "type": "string",
                "description": "到期时要产出/执行的内容。agent 模式=给 agent 的指令（如'提醒用户吃饭并说点暖心话'）；text 模式=直接发出的文本（如'该吃饭啦！'）",
            },
            "schedule": {"type": "string", "description": "调度表达式，见工具描述"},
            "mode": {"type": "string", "enum": ["agent", "text"], "description": "默认 agent"},
            "repeat": {"type": "integer", "description": "重复次数（省略=无限）；一次性任务可设 1"},
            "job_id": {"type": "string", "description": "任务 id（pause/resume/delete 必填）"},
        },
        "required": ["action"],
    },
)
async def cron(
    ctx: ToolContext,
    action: str,
    name: str = "",
    prompt: str = "",
    schedule: str = "",
    mode: str = "agent",
    repeat: int | None = None,
    job_id: str = "",
) -> str:
    db = ctx.db
    if db is None:
        return "Error: cron unavailable (no database)"
    if ctx.session_id is None:
        return "Error: cron 只能在会话中使用（当前无 session）"

    if action == "create":
        if not name or not prompt or not schedule:
            return "Error: create 需要 name / prompt / schedule"
        if mode not in ("agent", "text"):
            return "Error: mode 必须是 agent 或 text"
        try:
            sched = cron_jobs.parse_schedule(schedule)
        except ValueError as e:
            return f"Error: {e}\n{_SCHEDULE_HELP}"
        now = datetime.now()
        try:
            next_run = cron_jobs.compute_next_run(sched, now)
        except Exception as e:
            return f"Error: 计算下次运行时间失败: {e}"
        next_run_iso = next_run.isoformat(timespec="seconds") if next_run else None
        display = cron_jobs.describe_schedule(sched)
        new_id = uuid.uuid4().hex[:12]
        await db.create_cron_job({
            "id": new_id,
            "session_id": ctx.session_id,
            "user_id": ctx.user_id,
            "name": name,
            "prompt": prompt,
            "mode": mode,
            "schedule": sched,
            "schedule_display": display,
            "repeat_times": repeat,
            "next_run_at": next_run_iso,
        })
        return (
            f"✅ 已创建定时任务「{name}」（{display}，{mode} 模式）。\n"
            f"下次运行: {cron_jobs.fmt_local(next_run_iso)}\n"
            f"任务 id: {new_id}（可用 cron list/pause/resume/delete 管理）"
        )

    if action == "list":
        rows = await db.list_cron_jobs_by_session(ctx.session_id)
        if not rows:
            return "当前会话没有定时任务。"
        lines = []
        for r in rows:
            j = cron_jobs.row_to_job(r)
            lines.append(
                f"- [{j['id']}] {j['name']} | {j.get('schedule_display') or '?'} | "
                f"{j.get('mode')} | state={j.get('state')} | 下次={cron_jobs.fmt_local(j.get('next_run_at'))} | "
                f"上次={j.get('last_status') or '—'}"
            )
        return "\n".join(lines)

    if action == "pause":
        if not job_id:
            return "Error: pause 需要 job_id"
        ok = await db.set_cron_job_state(job_id, "paused", ctx.user_id)
        return f"⏸ 已暂停 {job_id}" if ok else f"Error: 未找到属于你的任务 {job_id}"

    if action == "resume":
        if not job_id:
            return "Error: resume 需要 job_id"
        ok = await db.set_cron_job_state(job_id, "scheduled", ctx.user_id)
        return f"▶ 已恢复 {job_id}" if ok else f"Error: 未找到属于你的任务 {job_id}"

    if action == "delete":
        if not job_id:
            return "Error: delete 需要 job_id"
        ok = await db.delete_cron_job(job_id, ctx.user_id)
        return f"🗑 已删除 {job_id}" if ok else f"Error: 未找到属于你的任务 {job_id}"

    return f"Error: 未知 action {action!r}"
