"""System prompt assembly: agent prompt + skill index + memory + environment."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..config import get_config
from ..models import AgentDef

_MEMORY_SECTIONS = (
    ("memory", "MEMORY (your notes)",
     "Facts you saved about the environment, conventions, and lessons learned:"),
    ("user", "USER PROFILE",
     "What you know about this user — identity, preferences, communication style:"),
)


def _render_memory(memories: dict[str, list[str]]) -> list[str]:
    """Render the dual-store memory blocks, each with a hermes-style usage
    header. Empty stores are omitted entirely."""
    cfg = get_config()
    limits = {"memory": cfg.memory_total_chars, "user": cfg.user_total_chars}
    parts: list[str] = []
    for key, heading, intro in _MEMORY_SECTIONS:
        entries = memories.get(key) or []
        if not entries:
            continue
        used = sum(len(e) for e in entries)
        limit = limits[key]
        pct = round(used / limit * 100) if limit else 0
        body = "\n".join(f"- {e}" for e in entries)
        parts.append(f"## {heading} [{pct}% — {used}/{limit} chars]\n{intro}\n{body}")
    return parts

SELF_EVOLVE_GUIDE = """\
## Skills
Skills are reusable procedural knowledge stored as SKILL.md files. The index
below lists what exists; load a skill's full content with skill_view(name)
when it is relevant to the task at hand.

Self-evolution: when you discover a reusable procedure, a non-obvious fix, or
a workflow worth repeating, capture it with skill_manage(action="create").
When an existing skill turns out to be wrong or incomplete, improve it with
skill_manage(action="patch"). Keep skills narrow, procedural and actionable."""

SESSION_SEARCH_GUIDE = """\
## Recall
When the user references something from a past conversation ("last time", "as I
mentioned", "we did this before") or you suspect relevant prior context exists,
use session_search to recall it before asking them to repeat themselves. It
searches only this user's own past conversations."""


CRON_GUIDE = """\
## Scheduled tasks (cron)
When the user wants something to happen on a schedule — "每天9点提醒我吃饭",
"每个整点提醒我运动一下", "每周一发周报", "30分钟后叫我" — use the `cron`
tool to create a job bound to this session. At the scheduled time the job fires
in this session: `mode=agent` runs a full turn (you can use tools/memory);
`mode=text` just posts the prompt verbatim without calling the model (cheap,
good for plain reminders — prefer it when no reasoning is needed).

`schedule` syntax: ① cron `0 9 * * *` (daily 9am) / `0 * * * *` (every hour) /
`0 9 * * 1-5` (weekdays) / `*/30 * * * *` (every 30min); ② `every 30m` / `every
2h` / `every 1d`; ③ once: `30m` (in 30 min) or `2026-06-25T14:00`. Translate
the user's natural-language time into one of these. Confirm the schedule back
to the user when you create the job."""


def build_system_prompt(
    agent: AgentDef,
    skill_index: list[dict] | None = None,
    memories: dict[str, list[str]] | None = None,
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

    if "session_search" in agent.tools:
        parts.append(SESSION_SEARCH_GUIDE)

    if "cron" in agent.tools:
        parts.append(CRON_GUIDE)

    has_skill_tools = any(t in agent.tools for t in ("skills_list", "skill_view", "skill_manage"))
    if has_skill_tools:
        parts.append(SELF_EVOLVE_GUIDE)
        if skill_index:
            lines = [f"- {s['name']}: {s['description']}" for s in skill_index]
            parts.append("## Available Skills\n" + "\n".join(lines))
        else:
            parts.append("## Available Skills\n(none yet — create the first one when you learn something reusable)")

    if memories:
        parts.extend(_render_memory(memories))

    return "\n\n".join(parts)
