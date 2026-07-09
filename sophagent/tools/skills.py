"""Skill tools: list / view / manage. skill_manage is the self-evolution entry."""

from __future__ import annotations

import json

from ..skills.store import SkillError, filter_index_for_agent
from .registry import ToolContext, tool


@tool(
    "skills_list",
    "List available skills (name, description, tags).",
    {"type": "object", "properties": {}},
)
async def skills_list(ctx: ToolContext) -> str:
    if ctx.skill_store is None:
        return "Error: skill store unavailable"
    index = filter_index_for_agent(ctx.skill_store.index(ctx.agent.skills), ctx.agent)
    if not index:
        return "No skills exist yet."
    return "\n".join(f"- {s['name']}: {s['description']}" +
                     (f" [tags: {', '.join(s['tags'])}]" if s["tags"] else "")
                     for s in index)


@tool(
    "skill_view",
    "Load the full content of a skill (SKILL.md), or a supporting file under references/, scripts/, templates/ or assets/.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "file_path": {
                "type": "string",
                "description": "Optional supporting file path inside the skill dir, e.g. references/playbook.md",
            },
        },
        "required": ["name"],
    },
)
async def skill_view(ctx: ToolContext, name: str, file_path: str = "") -> str:
    if ctx.skill_store is None:
        return "Error: skill store unavailable"
    try:
        return ctx.skill_store.view(name, file_path)
    except SkillError as e:
        return f"Error: {e}"


SKILL_MANAGE_DESC = """\
Create, improve or delete skills (reusable procedural knowledge).
Actions:
- create: new skill; content must be a full SKILL.md with YAML frontmatter (name, description)
- edit: rewrite SKILL.md completely
- patch: targeted old_string -> new_string replacement (preferred for small fixes)
- delete: remove a skill
- write_file / remove_file: manage supporting files under references/, scripts/, templates/ or assets/
Example SKILL.md:
---
name: my-skill
description: "One-line summary of when to use this"
version: 1.0.0
metadata:
  tags: [example]
---

# My Skill
Step-by-step instructions..."""


@tool(
    "skill_manage",
    SKILL_MANAGE_DESC,
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "edit", "patch", "delete", "write_file", "remove_file"]},
            "name": {"type": "string", "description": "Skill name (lowercase, hyphens)"},
            "content": {"type": "string", "description": "Full SKILL.md content (create/edit)"},
            "old_string": {"type": "string", "description": "Text to find (patch)"},
            "new_string": {"type": "string", "description": "Replacement text (patch)"},
            "replace_all": {"type": "boolean", "default": False},
            "file_path": {"type": "string", "description": "Relative path inside the skill dir (write_file/remove_file)"},
            "file_content": {"type": "string", "description": "File content (write_file)"},
        },
        "required": ["action", "name"],
    },
)
async def skill_manage(
    ctx: ToolContext,
    action: str,
    name: str,
    content: str = "",
    old_string: str = "",
    new_string: str = "",
    replace_all: bool = False,
    file_path: str = "",
    file_content: str = "",
) -> str:
    store = ctx.skill_store
    if store is None:
        return "Error: skill store unavailable"
    try:
        if action == "create":
            store.create(name, content)
            return json.dumps({"ok": True, "action": "create", "name": name})
        if action == "edit":
            store.edit(name, content)
            return json.dumps({"ok": True, "action": "edit", "name": name})
        if action == "patch":
            n = store.patch(name, old_string, new_string, replace_all)
            return json.dumps({"ok": True, "action": "patch", "name": name, "replacements": n})
        if action == "delete":
            store.delete(name)
            return json.dumps({"ok": True, "action": "delete", "name": name})
        if action == "write_file":
            store.write_support_file(name, file_path, file_content)
            return json.dumps({"ok": True, "action": "write_file", "name": name, "file": file_path})
        if action == "remove_file":
            store.remove_support_file(name, file_path)
            return json.dumps({"ok": True, "action": "remove_file", "name": name, "file": file_path})
        return f"Error: unknown action {action!r}"
    except SkillError as e:
        return f"Error: {e}"
