"""Internal message format (OpenAI chat.completions shaped) and API schemas."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Internal conversation format. This is the canonical shape stored in the DB;
# provider adapters convert to/from their native wire formats.
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolCall":
        return cls(id=d["id"], name=d["name"], arguments=d.get("arguments") or {})


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    # some thinking models (e.g. DeepSeek) require reasoning_content to be
    # echoed back on subsequent calls within the same tool-use sequence
    reasoning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.reasoning:
            d["reasoning"] = self.reasoning
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        return cls(
            role=d["role"],
            content=d.get("content") or "",
            tool_calls=[ToolCall.from_dict(tc) for tc in d["tool_calls"]] if d.get("tool_calls") else None,
            tool_call_id=d.get("tool_call_id"),
            reasoning=d.get("reasoning"),
        )

    @classmethod
    def from_json(cls, s: str) -> "Message":
        return cls.from_dict(json.loads(s))


@dataclass
class AssistantTurn:
    """One completed model response."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str = ""
    reasoning: str = ""

    def as_message(self) -> Message:
        return Message(role="assistant", content=self.content, tool_calls=self.tool_calls or None,
                       reasoning=self.reasoning or None)


@dataclass
class StreamEvent:
    """Events yielded by Provider.chat(): incremental text or the final turn."""

    type: Literal["text_delta", "reasoning_delta", "turn_done"]
    text: str = ""
    turn: AssistantTurn | None = None


@dataclass
class AgentDef:
    """An agent definition row (the configurable persona)."""

    id: int
    name: str
    description: str
    system_prompt: str
    provider: str
    model: str
    tools: list[str]
    skills: list[str] | None  # None = all skills visible
    max_iterations: int = 30
    temperature: float | None = None
    group_id: int | None = None

    @classmethod
    def from_row(cls, row: Any) -> "AgentDef":
        keys = row.keys()
        return cls(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            system_prompt=row["system_prompt"],
            provider=row["provider"],
            model=row["model"],
            tools=json.loads(row["tools"] or "[]"),
            skills=json.loads(row["skills"]) if row["skills"] else None,
            max_iterations=row["max_iterations"] or 30,
            temperature=row["temperature"],
            group_id=row["group_id"] if "group_id" in keys else None,
        )


# ---------------------------------------------------------------------------
# API (pydantic) models
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6)


class UserCreate(BaseModel):
    # admin privilege is granted by adding a user to the admin group, not a role flag
    username: str = Field(pattern=r"^[a-zA-Z0-9_.-]{2,32}$")
    password: str = Field(min_length=6)


class UserPatch(BaseModel):
    # admin status is changed via admin-group membership, not here
    password: Optional[str] = Field(default=None, min_length=6)


class AgentCreate(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    description: str = ""
    system_prompt: str
    provider: str
    model: str
    tools: list[str] = []
    skills: Optional[list[str]] = None
    max_iterations: int = Field(default=30, ge=1, le=200)
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    group_id: Optional[int] = None  # omitted => caller's primary (owned) group


class SessionCreate(BaseModel):
    agent_id: int
    title: str = ""


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    owner_id: Optional[int] = None  # admin only; defaults to the caller


class GroupPatch(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class MemberAdd(BaseModel):
    user_id: int


class MemberPatch(BaseModel):
    can_manage: bool


class SessionOverridePatch(BaseModel):
    override_provider: Optional[str] = None
    override_model: Optional[str] = None
    thinking_mode: Optional[str] = Field(default=None, pattern=r"^(default|thinking|fast)$")


class ChatRequest(BaseModel):
    content: str = Field(min_length=1)


class TruncateRequest(BaseModel):
    """Restore / re-edit: delete the given user message and everything after it."""
    message_id: int


class SkillWrite(BaseModel):
    content: str  # full SKILL.md content


class ProviderCreate(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    api_mode: str = Field(pattern=r"^(openai|anthropic)$")
    base_url: Optional[str] = None
    api_key: str = ""
    context_limit: int = Field(default=100_000, ge=1)


class ProviderPatch(BaseModel):
    api_mode: str = Field(pattern=r"^(openai|anthropic)$")
    base_url: Optional[str] = None
    api_key: Optional[str] = None   # None/"" => 不变
    context_limit: int = Field(default=100_000, ge=1)


# OpenAI compatible layer ----------------------------------------------------


class OpenAIChatMessage(BaseModel):
    role: str
    content: Any = None


class OpenAIChatRequest(BaseModel):
    model: str
    messages: list[OpenAIChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
