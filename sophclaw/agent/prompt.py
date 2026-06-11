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


def build_system_prompt(
    agent: AgentDef,
    skill_index: list[dict] | None = None,
    memories: list[str] | None = None,
    workspace: Path | None = None,
) -> str:
    parts = [agent.system_prompt.strip()]

    env_lines = [f"Current date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} (UTC)"]
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

    if memories:
        parts.append("## Memory\nFacts you saved in earlier conversations with this user:\n" +
                     "\n".join(f"- {m}" for m in memories))

    return "\n\n".join(parts)
