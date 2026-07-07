"""Feishu interactive card helpers."""

from __future__ import annotations

import json
from typing import Any


def build_help_card() -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Sophagent 帮助"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": "命令：/pair <code> 绑定 · /new 新会话 · /stop 停止 · /help",
                },
            },
            {
                "tag": "action",
                "actions": [
                    _button("new", "新会话", "cmd:/new", "primary"),
                    _button("stop", "停止", "cmd:/stop"),
                    _button("help", "帮助", "cmd:/help"),
                ],
            },
        ],
    }


def build_card_content(card: dict[str, Any]) -> str:
    return json.dumps(card, ensure_ascii=False)


def _button(action_id: str, label: str, data: str, button_type: str = "default") -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": button_type,
        "value": {"cmd": data},
    }
