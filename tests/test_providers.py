"""Format conversion tests for the two provider adapters."""

from types import SimpleNamespace

from sophagent.config import ProviderConfig
from sophagent.models import Message, MessageAttachment, ToolCall
from sophagent.providers.anthropic_provider import messages_to_anthropic, tools_to_anthropic
from sophagent.providers.openai_provider import OpenAIProvider, _parse_arguments, messages_to_openai

HISTORY = [
    Message(role="user", content="hi"),
    Message(
        role="assistant",
        content="let me check",
        tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})],
    ),
    Message(role="tool", content="contents A", tool_call_id="c1"),
    Message(role="assistant", content="done"),
]


def test_openai_conversion():
    out = messages_to_openai("sys", HISTORY)
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[1] == {"role": "user", "content": "hi"}
    asst = out[2]
    assert asst["tool_calls"][0]["function"]["name"] == "read_file"
    assert asst["tool_calls"][0]["id"] == "c1"
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "contents A"}


def test_openai_vl_conversion_includes_image_data_url(tmp_path):
    img = tmp_path / "im" / "qqbot" / "pic.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    msg = Message(
        role="user",
        content="看图",
        attachments=[
            MessageAttachment(kind="image", path="im/qqbot/pic.png", mime="image/png"),
        ],
    )
    out = messages_to_openai("", [msg], model="qwen3-vl-flash", workspace=tmp_path)
    content = out[0]["content"]
    assert content[0] == {"type": "text", "text": "看图"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_text_model_keeps_image_as_text(tmp_path):
    msg = Message(
        role="user",
        content="看图",
        attachments=[MessageAttachment(kind="image", path="im/qqbot/pic.png")],
    )
    assert messages_to_openai("", [msg], model="deepseek-chat", workspace=tmp_path)[0] == {
        "role": "user",
        "content": "看图",
    }


def test_openai_argument_parsing():
    assert _parse_arguments('{"a": 1}') == {"a": 1}
    assert _parse_arguments("") == {}
    assert _parse_arguments("not json") == {"_raw": "not json"}


def test_anthropic_conversion_tool_use_and_result():
    out = messages_to_anthropic(HISTORY)
    assert out[0] == {"role": "user", "content": "hi"}
    asst = out[1]
    assert asst["role"] == "assistant"
    assert asst["content"][0] == {"type": "text", "text": "let me check"}
    assert asst["content"][1]["type"] == "tool_use"
    assert asst["content"][1]["input"] == {"path": "a.txt"}
    tool_result = out[2]
    assert tool_result["role"] == "user"
    assert tool_result["content"][0]["type"] == "tool_result"
    assert tool_result["content"][0]["tool_use_id"] == "c1"


def test_anthropic_merges_consecutive_tool_results():
    history = [
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(id="c1", name="a", arguments={}),
                ToolCall(id="c2", name="b", arguments={}),
            ],
        ),
        Message(role="tool", content="r1", tool_call_id="c1"),
        Message(role="tool", content="r2", tool_call_id="c2"),
    ]
    out = messages_to_anthropic(history)
    assert len(out) == 2  # one assistant + one merged user
    assert [b["tool_use_id"] for b in out[1]["content"]] == ["c1", "c2"]


def test_anthropic_tool_schema():
    tools = [{"name": "t", "description": "d", "parameters": {"type": "object", "properties": {}}}]
    out = tools_to_anthropic(tools)
    assert out[0]["input_schema"] == {"type": "object", "properties": {}}
    assert "parameters" not in out[0]


def test_message_roundtrip():
    m = Message(role="assistant", content="x", tool_calls=[ToolCall(id="1", name="n", arguments={"k": "v"})])
    assert Message.from_json(m.to_json()).to_dict() == m.to_dict()


def _chunk(content=None, reasoning=None, finish=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=None)


async def test_openai_streams_reasoning_delta():
    """Thinking models stream reasoning_content separately; the adapter emits it
    as reasoning_delta events, keeps it out of the visible text, and captures the
    full reasoning on the final turn for echo-back."""
    provider = OpenAIProvider(ProviderConfig(name="t", api_mode="openai"))

    async def fake_stream():
        yield _chunk(reasoning="think ")
        yield _chunk(reasoning="more")
        yield _chunk(content="answer")
        yield _chunk(finish="stop")

    async def fake_create(**kwargs):
        return fake_stream()

    provider.client.chat.completions.create = fake_create

    events = [ev async for ev in provider.chat(
        model="m", system="", messages=[Message(role="user", content="hi")])]
    assert [e.text for e in events if e.type == "reasoning_delta"] == ["think ", "more"]
    # reasoning is not leaked into the visible text stream
    assert [e.text for e in events if e.type == "text_delta"] == ["answer"]
    # the full reasoning is still captured on the turn (for persistence + echo-back)
    turn = next(e.turn for e in events if e.type == "turn_done")
    assert turn.reasoning == "think more"
    assert turn.content == "answer"


def test_openai_reasoning_roundtrip():
    m = Message(role="assistant", content="x", reasoning="thinking...",
                tool_calls=[ToolCall(id="c1", name="t", arguments={})])
    out = messages_to_openai("", [m])
    assert out[0]["reasoning_content"] == "thinking..."
    # JSON persistence keeps reasoning
    assert Message.from_json(m.to_json()).reasoning == "thinking..."
    # messages without reasoning don't get the key
    assert "reasoning_content" not in messages_to_openai("", [Message(role="assistant", content="y")])[0]
