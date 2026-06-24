"""File tools, confined to the per-user workspace."""

from __future__ import annotations

from pathlib import Path

from .registry import ToolContext, tool

MAX_READ_BYTES = 256 * 1024


def safe_path(workspace: Path, p: str) -> Path:
    """Resolve p inside workspace; reject escapes (.., absolute paths, symlinks)."""
    candidate = (workspace / p).resolve()
    if not candidate.is_relative_to(workspace.resolve()):
        raise ValueError(f"path escapes workspace: {p!r}")
    return candidate


@tool(
    "read_file",
    "Read a text file from your workspace. Paths are relative to the workspace root.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative file path"},
            "offset": {"type": "integer", "description": "1-based start line (optional)"},
            "limit": {"type": "integer", "description": "Max lines to read (optional)"},
        },
        "required": ["path"],
    },
)
async def read_file(ctx: ToolContext, path: str, offset: int = 1, limit: int = 0) -> str:
    f = safe_path(ctx.workspace, path)
    if not f.is_file():
        return f"Error: file not found: {path}"
    if f.stat().st_size > MAX_READ_BYTES:
        return f"Error: file too large (> {MAX_READ_BYTES} bytes); read a slice with offset/limit after splitting"
    text = f.read_text(encoding="utf-8", errors="replace")
    if offset > 1 or limit:
        lines = text.splitlines()
        end = offset - 1 + limit if limit else len(lines)
        text = "\n".join(lines[offset - 1 : end])
    return text or "(empty file)"


@tool(
    "write_file",
    "Create or overwrite a text file in your workspace.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
)
async def write_file(ctx: ToolContext, path: str, content: str) -> str:
    f = safe_path(ctx.workspace, path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {path}"


@tool(
    "edit_file",
    "Replace an exact string in a file. old_string must be unique in the file unless replace_all is true.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
        },
        "required": ["path", "old_string", "new_string"],
    },
)
async def edit_file(ctx: ToolContext, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    f = safe_path(ctx.workspace, path)
    if not f.is_file():
        return f"Error: file not found: {path}"
    text = f.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_string)
    if count == 0:
        return "Error: old_string not found in file"
    if count > 1 and not replace_all:
        return f"Error: old_string occurs {count} times; make it unique or set replace_all"
    f.write_text(text.replace(old_string, new_string), encoding="utf-8")
    return f"Replaced {count if replace_all else 1} occurrence(s) in {path}"


@tool(
    "list_dir",
    "List files in a workspace directory.",
    {
        "type": "object",
        "properties": {"path": {"type": "string", "default": "."}},
    },
)
async def list_dir(ctx: ToolContext, path: str = ".") -> str:
    d = safe_path(ctx.workspace, path)
    if not d.is_dir():
        return f"Error: not a directory: {path}"
    entries = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name))
    if not entries:
        return "(empty directory)"
    lines = [f"{p.name}/" if p.is_dir() else f"{p.name}  ({p.stat().st_size} bytes)" for p in entries[:500]]
    return "\n".join(lines)
