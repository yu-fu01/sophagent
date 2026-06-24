"""Seed bundled built-in skills into the runtime skills_dir.

Built-in skills (imported from hermes-agent, flattened to one level) live in the
committed ``builtin/`` directory next to this module.  On startup we copy any
that are *missing* from the runtime ``data_dir/skills`` — existing skill
directories are left untouched so user/agent edits survive restarts.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger("sophagent.skills.seed")


def builtin_dir() -> Path:
    """Directory holding the committed built-in skill bundle."""
    return Path(__file__).parent / "builtin"


def seed_builtin_skills(dest: Path, source: Path | None = None) -> int:
    """Copy missing built-in skills from ``source`` into ``dest``.

    A built-in skill is any immediate subdirectory of ``source`` that contains a
    ``SKILL.md``.  If ``dest/<name>`` already exists it is skipped (no overwrite).
    Returns the number of skills newly seeded.  Idempotent; never raises on a
    single bad/missing skill — it logs and moves on so startup is never blocked.
    """
    src = source or builtin_dir()
    if not src.is_dir():
        return 0
    dest.mkdir(parents=True, exist_ok=True)

    seeded = 0
    for skill in sorted(src.iterdir()):
        if not skill.is_dir() or not (skill / "SKILL.md").is_file():
            continue
        target = dest / skill.name
        if target.exists():
            continue
        try:
            shutil.copytree(skill, target)
            seeded += 1
        except OSError as e:
            log.warning("failed to seed built-in skill %s: %s", skill.name, e)
    return seeded
