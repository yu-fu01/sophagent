"""IM 斜杠命令：/pair /new /stop /help。

仅这 4 条在 IM 暴露；sophclaw 现有 /compact /model 等不在 IM 暴露（避免冲突）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CommandResult:
    handled: bool
    text: str = ""           # 回复给 IM 用户的内容
    name: str = ""           # 命令名（供 driver 记日志）


def parse(text: str) -> tuple[str, str]:
    """'/pair abc' -> ('pair', 'abc')；非命令返回 ('', '')。"""
    if not text.startswith("/"):
        return "", ""
    parts = text.split(maxsplit=1)
    name = parts[0].lstrip("/").lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return name, args
