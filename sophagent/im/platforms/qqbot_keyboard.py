"""Small QQ Bot inline-keyboard helpers.

The shape mirrors QQ Bot v2's ``keyboard`` message field and is intentionally
minimal: enough for callback buttons whose ``button_data`` is returned in
``INTERACTION_CREATE``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class KeyboardButtonPermission:
    type: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type}


@dataclass
class KeyboardButtonAction:
    data: str
    type: int = 1
    permission: KeyboardButtonPermission = field(default_factory=KeyboardButtonPermission)
    click_limit: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "data": self.data,
            "permission": self.permission.to_dict(),
            "click_limit": self.click_limit,
        }


@dataclass
class KeyboardButtonRenderData:
    label: str
    visited_label: str = ""
    style: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "visited_label": self.visited_label or self.label,
            "style": self.style,
        }


@dataclass
class KeyboardButton:
    id: str
    label: str
    data: str
    visited_label: str = ""
    style: int = 1
    group_id: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "render_data": KeyboardButtonRenderData(
                label=self.label,
                visited_label=self.visited_label,
                style=self.style,
            ).to_dict(),
            "action": KeyboardButtonAction(data=self.data).to_dict(),
            "group_id": self.group_id,
        }


@dataclass
class InlineKeyboard:
    rows: list[list[KeyboardButton]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": {
                "rows": [
                    {"buttons": [button.to_dict() for button in row]}
                    for row in self.rows
                ]
            }
        }


def build_help_keyboard() -> InlineKeyboard:
    return InlineKeyboard(rows=[[
        KeyboardButton(id="new", label="新会话", visited_label="已选择新会话", data="cmd:/new"),
        KeyboardButton(id="stop", label="停止", visited_label="已选择停止", data="cmd:/stop", style=0),
        KeyboardButton(id="help", label="帮助", visited_label="已选择帮助", data="cmd:/help"),
    ]])


@dataclass
class InteractionEvent:
    id: str = ""
    scene: str = ""
    chat_id: str = ""
    operator_id: str = ""
    button_data: str = ""
    button_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def parse_interaction_event(raw: dict[str, Any]) -> InteractionEvent:
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    resolved = data.get("resolved") if isinstance(data.get("resolved"), dict) else {}
    chat_type = int(raw.get("chat_type", 0) or 0)
    scene = {0: "guild", 1: "group", 2: "c2c"}.get(chat_type, "")
    if scene == "group":
        chat_id = str(raw.get("group_openid") or "")
        operator = str(raw.get("group_member_openid") or resolved.get("user_id") or "")
    elif scene == "guild":
        chat_id = str(raw.get("channel_id") or raw.get("guild_id") or "")
        operator = str(resolved.get("user_id") or raw.get("user_openid") or "")
    else:
        chat_id = str(raw.get("user_openid") or resolved.get("user_id") or "")
        operator = chat_id
    return InteractionEvent(
        id=str(raw.get("id") or ""),
        scene=scene or "c2c",
        chat_id=chat_id,
        operator_id=operator,
        button_data=str(resolved.get("button_data") or ""),
        button_id=str(resolved.get("button_id") or ""),
        raw=raw,
    )


def interaction_text(button_data: str) -> str:
    data = (button_data or "").strip()
    if data.startswith("cmd:"):
        return data[4:].strip()
    if data.startswith("text:"):
        return data[5:].strip()
    return f"[按钮点击: {data}]" if data else "[按钮点击]"
