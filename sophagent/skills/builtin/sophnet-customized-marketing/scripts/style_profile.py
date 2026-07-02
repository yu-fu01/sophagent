#!/usr/bin/env python3
"""
User marketing style profile manager.

Reads, writes, and updates a Markdown-based style profile that captures the
user's personal expression habits, brand tone, and preferences. The profile
is stored as `user-style-profile.md` in a configurable directory.

This is the only script in the skill that performs data persistence.
It does NOT modify the user's USER.md file.

Usage:
    python style_profile.py read  [--profile-dir DIR] [--format text|json]
    python style_profile.py write --content "..." [--profile-dir DIR]
    python style_profile.py update --key "表达风格" --value "热情外放" [--profile-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROFILE_FILENAME = "user-style-profile.md"

# ---------------------------------------------------------------------------
# Profile parsing & serialization
# ---------------------------------------------------------------------------


def parse_sections(content: str) -> list[tuple[str, str]]:
    """Parse markdown content into a list of (heading, body) tuples.

    Sections are delimited by `## heading` lines. Content before the first
    `## ` is treated as the preamble (key="").
    """
    sections: list[tuple[str, str]] = []
    current_key = ""
    current_lines: list[str] = []

    for line in content.splitlines():
        if line.startswith("## "):
            sections.append((current_key, "\n".join(current_lines).strip()))
            current_key = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)

    sections.append((current_key, "\n".join(current_lines).strip()))
    return sections


def serialize_sections(sections: list[tuple[str, str]]) -> str:
    """Serialize (heading, body) tuples back to markdown."""
    parts: list[str] = []
    for key, body in sections:
        if key:
            parts.append(f"## {key}")
        if body:
            parts.append(body)
        parts.append("")
    return "\n".join(parts).strip() + "\n"


def update_section(content: str, key: str, value: str) -> str:
    """Update or append a section in the markdown content."""
    sections = parse_sections(content)
    found = False
    for i, (k, _) in enumerate(sections):
        if k == key:
            sections[i] = (k, value)
            found = True
            break
    if not found:
        sections.append((key, value))
    return serialize_sections(sections)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def resolve_profile_path(profile_dir: str) -> Path:
    directory = Path(profile_dir).expanduser()
    return directory / PROFILE_FILENAME


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def cmd_read(profile_dir: str | None, fmt: str) -> None:
    path = resolve_profile_path(profile_dir)

    if not path.exists():
        if fmt == "json":
            print(json.dumps({"STATUS": "not_found", "PROFILE_PATH": str(path)},
                             ensure_ascii=False, indent=2))
        else:
            print(f"STATUS=not_found")
            print(f"PROFILE_PATH={path}")
        return

    content = path.read_text(encoding="utf-8")

    if fmt == "json":
        sections = parse_sections(content)
        section_dict = {k: v for k, v in sections if k}
        print(json.dumps({
            "STATUS": "ok",
            "PROFILE_PATH": str(path),
            "sections": section_dict,
            "raw": content,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"STATUS=ok")
        print(f"PROFILE_PATH={path}")
        print("---")
        print(content, end="")


def cmd_write(content: str, profile_dir: str | None) -> None:
    path = resolve_profile_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"STATUS=ok")
    print(f"PROFILE_PATH={path}")
    print(f"MESSAGE=Profile written ({len(content)} chars)")


def cmd_update(key: str, value: str, profile_dir: str | None) -> None:
    path = resolve_profile_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    existed = path.exists()
    if existed:
        old_content = path.read_text(encoding="utf-8")
    else:
        old_content = "# 用户营销风格档案\n\n"

    new_content = update_section(old_content, key, value)
    path.write_text(new_content, encoding="utf-8")

    action = "updated" if existed else "created"
    print(f"STATUS=ok")
    print(f"PROFILE_PATH={path}")
    print(f"MESSAGE=Section '{key}' {action}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Read, write, or update the user's marketing style profile.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Subcommands:\n"
            "  read    Read the current style profile\n"
            "  write   Write (overwrite) the entire style profile\n"
            "  update  Update a single section by key\n\n"
            "Examples:\n"
            '  python style_profile.py read --profile-dir SophAgent 用户 workspace/sophnet-customized-marketing\n'
            '  python style_profile.py read --profile-dir /path/to/dir --format json\n'
            '  python style_profile.py write --profile-dir /path/to/dir --content "# 用户营销风格档案\\n\\n## 表达风格\\n简洁专业"\n'
            '  python style_profile.py update --profile-dir /path/to/dir --key "表达风格" --value "热情活泼，喜欢用 emoji"'
        ),
    )
    sub = parser.add_subparsers(dest="command", help="Operation to perform")
    sub.required = True

    # read
    p_read = sub.add_parser("read", help="Read the current style profile")
    p_read.add_argument("--profile-dir", required=True,
                        help="Directory to store/read profile (e.g. SophAgent 用户 workspace/sophnet-customized-marketing)")
    p_read.add_argument("--format", default="text", choices=["text", "json"],
                        help="Output format (default: text)")

    # write
    p_write = sub.add_parser("write", help="Write (overwrite) the entire style profile")
    p_write.add_argument("--content", required=True,
                         help="Markdown content to write")
    p_write.add_argument("--profile-dir", required=True,
                         help="Directory to store/read profile (e.g. SophAgent 用户 workspace/sophnet-customized-marketing)")

    # update
    p_update = sub.add_parser("update", help="Update a single section by key")
    p_update.add_argument("--key", required=True,
                          help="Section heading to update (e.g. '表达风格')")
    p_update.add_argument("--value", required=True,
                          help="New content for the section")
    p_update.add_argument("--profile-dir", required=True,
                          help="Directory to store/read profile (e.g. SophAgent 用户 workspace/sophnet-customized-marketing)")

    args = parser.parse_args(argv)

    if args.command == "read":
        cmd_read(args.profile_dir, args.format)
    elif args.command == "write":
        cmd_write(args.content, args.profile_dir)
    elif args.command == "update":
        cmd_update(args.key, args.value, args.profile_dir)


if __name__ == "__main__":
    main()
