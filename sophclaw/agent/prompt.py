"""System prompt assembly: agent prompt + skill index + memory + environment."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..models import AgentDef

SELF_EVOLVE_GUIDE = """\
## Skills
Skills are reusable procedural knowledge stored as SKILL.md files. The index
below lists what exists; load a skill's full content with skill_view(name)
when it is relevant to the task at hand.

Self-evolution: when you discover a reusable procedure, a non-obvious fix, or
a workflow worth repeating, capture it with skill_manage(action="create").
When an existing skill turns out to be wrong or incomplete, improve it with
skill_manage(action="patch"). Keep skills narrow, procedural and actionable."""


REMINDER_GUIDE = """\
## Reminders / scheduled tasks
When the user asks to set up a reminder or to run something on a schedule
(phrasings like "提醒我…", "每天/每周/每隔…", "X分钟后…", "到点…"), call the
`schedule_reminder` tool instead of just acknowledging verbally. You must
translate the user's wording into a `schedule` string in ONE of these formats:

- Relative one-shot: `30m`, `2h`, `1d` (fire once after N minutes/hours/days)
- ISO timestamp one-shot: `2026-02-03T14:00:00` (fire once at a specific time)
- Recurring interval: `every 30m`, `every 2h`
- Cron expression (5 fields `min hour day month weekday`): e.g. `0 9 * * *`
  (daily 09:00), `0 * * * *` (every top of the hour), `15 * * * *`
  (at minute 15 of every hour), `0 9 * * 1` (every Mon 09:00)

Common mappings: 每天9点→`0 9 * * *`, 每整点→`0 * * * *`, 每隔2小时→`every 2h`,
每小时的15分/每小时第15分钟→`15 * * * *`, 每15分钟/每隔15分钟→`every 15m`,
30分钟后→`30m`, 每周一9点→`0 9 * * 1`. Be careful: "每小时的15分" means
minute 15 of each hour, not a 15-minute duration. If the user omits a time, ask
before creating. The reminder fires through the cron ticker and its result
appears as a chat message in the current session — confirm using the tool's
suggested confirmation text."""


def build_system_prompt(
    agent: AgentDef,
    skill_index: list[dict] | None = None,
    memories: list[str] | None = None,
    workspace: Path | None = None,
) -> str:
    parts = [agent.system_prompt.strip()]

    env_lines = [f"Current date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} (UTC)"]
    # Model identity (BUG3.1): some endpoints don't echo the real model name, so
    # inject it explicitly — the agent answers "what model are you?" from this.
    # agent.model is the effective model (session overrides already applied).
    env_lines.append(
        f"You are powered by the model named {agent.model} (provider: {agent.provider}). "
        f"When asked which model you are, answer with this name."
    )
    if workspace is not None:
        env_lines.append("You have a private workspace directory; file and terminal tools operate inside it.")
    parts.append("## Environment\n" + "\n".join(env_lines))

    has_skill_tools = any(t in agent.tools for t in ("skills_list", "skill_view", "skill_manage"))
    if has_skill_tools:
        parts.append(SELF_EVOLVE_GUIDE)
        if skill_index:
            lines = [f"- {s['name']}: {s['description']}" for s in skill_index]
            parts.append("## Available Skills\n" + "\n".join(lines))
        else:
            parts.append("## Available Skills\n(none yet — create the first one when you learn something reusable)")

    if "schedule_reminder" in agent.tools:
        parts.append(REMINDER_GUIDE)

    if memories:
        parts.append("## Memory\nFacts you saved in earlier conversations with this user:\n" +
                     "\n".join(f"- {m}" for m in memories))

    return "\n\n".join(parts)
