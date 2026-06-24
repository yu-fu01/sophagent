"""SkillStore: hermes-compatible SKILL.md library with safe agent-driven CRUD.

Layout:  <skills_dir>/<name>/SKILL.md  (+ optional references/ scripts/
templates/ assets/ subdirectories).  SKILL.md starts with YAML frontmatter
containing at least `name` and `description`.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml

from ..config import get_config

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
ALLOWED_SUBDIRS = ("references", "scripts", "templates", "assets")


class SkillError(ValueError):
    pass


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter, body). Raises SkillError on malformed input."""
    if not content.startswith("---"):
        raise SkillError("SKILL.md must start with '---' YAML frontmatter")
    parts = content.split("\n---", 2)
    if len(parts) < 2:
        raise SkillError("unterminated frontmatter block")
    try:
        meta = yaml.safe_load(parts[0][3:]) or {}
    except yaml.YAMLError as e:
        raise SkillError(f"invalid YAML frontmatter: {e}")
    if not isinstance(meta, dict):
        raise SkillError("frontmatter must be a YAML mapping")
    body = parts[1] if len(parts) == 2 else parts[1] + "\n---" + parts[2]
    return meta, body


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class SkillStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.version = 0  # bumped on every mutation; cache-invalidation signal
        self._index_cache: list[dict[str, Any]] | None = None

    # -- reads -----------------------------------------------------------------

    def _scan(self) -> list[dict[str, Any]]:
        items = []
        for md in sorted(self.root.glob("*/SKILL.md")):
            try:
                meta, _ = parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
            except SkillError:
                continue  # skip malformed entries rather than break every prompt
            mblock = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
            tags = mblock.get("tags")
            if not tags and isinstance(mblock.get("hermes"), dict):  # hermes nests tags
                tags = mblock["hermes"].get("tags")
            items.append({
                "name": meta.get("name", md.parent.name),
                "description": str(meta.get("description", "")).strip(),
                "tags": tags or [],
                "dir": md.parent.name,
            })
        return items

    def index(self, only: list[str] | None = None) -> list[dict[str, Any]]:
        if self._index_cache is None:
            self._index_cache = self._scan()
        if only is None:
            return self._index_cache
        allowed = set(only)
        return [s for s in self._index_cache if s["name"] in allowed or s["dir"] in allowed]

    def _dir_for(self, name: str) -> Path:
        if not NAME_RE.match(name or ""):
            raise SkillError(f"invalid skill name {name!r} (lowercase letters, digits, hyphens; 2-64 chars)")
        return self.root / name

    def view(self, name: str) -> str:
        md = self._dir_for(name) / "SKILL.md"
        if not md.is_file():
            raise SkillError(f"skill {name!r} not found")
        content = md.read_text(encoding="utf-8", errors="replace")
        extras = [str(p.relative_to(md.parent)) for p in sorted(md.parent.rglob("*"))
                  if p.is_file() and p.name != "SKILL.md"]
        if extras:
            content += "\n\n[supporting files: " + ", ".join(extras[:50]) + "]"
        return content

    # -- writes ------------------------------------------------------------------

    def _invalidate(self) -> None:
        self.version += 1
        self._index_cache = None

    def _validate_skill_md(self, name: str, content: str) -> None:
        if len(content.encode()) > get_config().max_skill_md_bytes:
            raise SkillError(f"SKILL.md too large (max {get_config().max_skill_md_bytes} bytes)")
        meta, _ = parse_frontmatter(content)
        if not meta.get("name") or not meta.get("description"):
            raise SkillError("frontmatter must contain 'name' and 'description'")
        if meta["name"] != name:
            raise SkillError(f"frontmatter name {meta['name']!r} does not match skill name {name!r}")

    def create(self, name: str, content: str) -> None:
        d = self._dir_for(name)
        if d.exists():
            raise SkillError(f"skill {name!r} already exists (use edit/patch)")
        self._validate_skill_md(name, content)
        d.mkdir(parents=True)
        _atomic_write(d / "SKILL.md", content)
        self._invalidate()

    def edit(self, name: str, content: str) -> None:
        d = self._dir_for(name)
        if not (d / "SKILL.md").is_file():
            raise SkillError(f"skill {name!r} not found")
        self._validate_skill_md(name, content)
        _atomic_write(d / "SKILL.md", content)
        self._invalidate()

    def patch(self, name: str, old_string: str, new_string: str, replace_all: bool = False) -> int:
        md = self._dir_for(name) / "SKILL.md"
        if not md.is_file():
            raise SkillError(f"skill {name!r} not found")
        text = md.read_text(encoding="utf-8", errors="replace")
        count = text.count(old_string)
        if count == 0:
            raise SkillError("old_string not found in SKILL.md")
        if count > 1 and not replace_all:
            raise SkillError(f"old_string occurs {count} times; make it unique or set replace_all")
        new_text = text.replace(old_string, new_string)
        self._validate_skill_md(name, new_text)
        _atomic_write(md, new_text)
        self._invalidate()
        return count if replace_all else 1

    def delete(self, name: str) -> None:
        d = self._dir_for(name)
        if not d.is_dir():
            raise SkillError(f"skill {name!r} not found")
        shutil.rmtree(d)
        self._invalidate()

    def _support_path(self, name: str, file_path: str) -> Path:
        d = self._dir_for(name)
        if not d.is_dir():
            raise SkillError(f"skill {name!r} not found")
        p = (d / file_path).resolve()
        if not p.is_relative_to(d.resolve()):
            raise SkillError("file_path escapes the skill directory")
        rel = p.relative_to(d.resolve())
        if not rel.parts or rel.parts[0] not in ALLOWED_SUBDIRS:
            raise SkillError(f"supporting files must live under one of {ALLOWED_SUBDIRS}")
        return p

    def write_support_file(self, name: str, file_path: str, content: str) -> None:
        if len(content.encode()) > get_config().max_skill_file_bytes:
            raise SkillError(f"file too large (max {get_config().max_skill_file_bytes} bytes)")
        p = self._support_path(name, file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(p, content)
        self._invalidate()

    def remove_support_file(self, name: str, file_path: str) -> None:
        p = self._support_path(name, file_path)
        if not p.is_file():
            raise SkillError(f"file not found: {file_path}")
        p.unlink()
        self._invalidate()
