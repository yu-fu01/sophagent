"""Background self-improvement review (sophagent's self-evolution engine).

After a turn's user-visible reply has streamed, a decoupled background task
replays the conversation through a tool-restricted review agent that decides
whether to save memory or patch a skill — hermes' background review, adapted to
sophagent's stateless-per-turn server.

The review never writes to the session message log (``on_persist=None``); only
the memory / skill side effects of its tool calls land. It runs the agent loop
directly (not ``run_turns``), so it never schedules another review — no
recursion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace

from ..config import get_config
from ..models import AgentDef, Message
from .runtime import build_runner

log = logging.getLogger(__name__)

# Tools the review agent may use — memory writes + the skill-management surface.
REVIEW_TOOLS = ("memory", "skills_list", "skill_view", "skill_manage")

# A tool result is a successful mutation unless it starts with "Error" / is a
# read-only / no-op reply. We only count mutations as "changed".
_MUTATING_TOOLS = ("memory", "skill_manage")

COMBINED_REVIEW_PROMPT = (
    "Review the conversation above and update two things.\n\n"
    "**Memory** (who the user is): did the user reveal persona, preferences, "
    "personal details, or expectations about how you should behave? Save durable "
    "facts and preferences with the memory tool (target='user' for the person, "
    "target='memory' for environment/convention/lessons).\n\n"
    "**Skills** (how to do this class of task): if the user corrected your "
    "style/approach, or a non-trivial technique or fix emerged, or a loaded skill "
    "turned out wrong — capture it with skill_manage (patch an existing skill "
    "first; create a new class-level skill only when none fits).\n\n"
    "Do NOT save: environment-dependent failures, negative claims about tools "
    "('X is broken'), one-off task narratives, or transient errors that resolved.\n\n"
    "If nothing durable is worth saving, reply exactly 'Nothing to save.' and stop. "
    "Otherwise act, then briefly state what you saved."
)


@dataclass
class ReviewResult:
    changed: bool = False
    actions: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.changed:
            return "Nothing to save."
        return "💾 " + "; ".join(self.actions)


def _review_tools(agent: AgentDef) -> list[str]:
    """Review may only use REVIEW_TOOLS the agent actually has enabled."""
    return [t for t in REVIEW_TOOLS if t in agent.tools]


# Result-prefixes that mark an actual memory write (not a read / no-op / error).
_MEMORY_WRITE_PREFIXES = ("Saved", "Updated", "Removed", "Staged")


def _describe(name: str, result_preview: str) -> str | None:
    """Turn a successful *write* tool result into a short action line, or None
    if it wasn't a real change (error / no-op / read). A memory `read` returns
    the entry listing, which must NOT be counted as a change."""
    p = (result_preview or "").strip()
    if p.startswith("Error"):
        return None
    if name == "memory":
        return p if p.startswith(_MEMORY_WRITE_PREFIXES) else None
    if name == "skill_manage":
        return p
    return None


async def run_review(
    *,
    db,
    skill_store,
    agent: AgentDef,
    user_id: int,
    history: list[Message],
) -> ReviewResult:
    """Replay ``history`` through a tool-restricted review agent. Returns what
    changed. Never raises — a failed review is logged and reported as no-change."""
    cfg = get_config()
    tools = _review_tools(agent)
    if not tools:
        return ReviewResult(changed=False)  # nothing the review could act on

    review_agent = dc_replace(
        agent, tools=tools, max_iterations=cfg.review_max_iterations
    )
    result = ReviewResult()
    try:
        runner = await build_runner(
            db=db, skill_store=skill_store, agent=review_agent,
            user_id=user_id, history=list(history), on_persist=None,
        )
        # tag any write-approval staging done during review as auto-origin
        runner.ctx.services["write_origin"] = "review"
        pending: dict[str, str] = {}
        async for ev in runner.run(COMBINED_REVIEW_PROMPT):
            etype = ev.get("type")
            if etype == "tool_call" and ev.get("name") in _MUTATING_TOOLS:
                pending[ev["id"]] = ev["name"]
            elif etype == "tool_result" and ev.get("id") in pending:
                name = pending.pop(ev["id"])
                action = _describe(name, ev.get("preview", ""))
                if action:
                    result.actions.append(action)
    except Exception:
        log.exception("background review failed for user=%s agent=%s", user_id, agent.name)
        return ReviewResult(changed=False)

    result.changed = bool(result.actions)
    return result
