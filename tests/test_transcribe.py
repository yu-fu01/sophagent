"""Tests for sophnet / OpenAI STT routing in transcribe.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sophagent.im.transcribe import (
    _extract_sophnet_text,
    _normalize_sophnet_stt_root,
    _resolve_sophnet_stt,
    transcribe_audio_attachment,
)
from sophagent.models import MessageAttachment


def test_normalize_sophnet_stt_root_defaults_to_easyllm():
    root = _normalize_sophnet_stt_root(
        "",
        agent_base_url="https://www.sophnet.com/api/open-apis/v1",
    )
    assert root == "https://www.sophnet.com/api/open-apis/projects/easyllms/speechtotext"


def test_extract_sophnet_text_from_results():
    body = {
        "status": "SUCCEEDED",
        "results": [
            {
                "transcripts": [
                    {"text": "你好，测试一下。"},
                ],
            },
        ],
    }
    assert _extract_sophnet_text(body) == "你好，测试一下。"


@pytest.mark.asyncio
async def test_transcribe_uses_sophnet_when_provider_is_sophnet(tmp_path: Path):
    audio = tmp_path / "voice.opus"
    audio.write_bytes(b"fake-audio")
    attachment = MessageAttachment(
        kind="audio",
        path="voice.opus",
        name="voice.opus",
        mime="audio/opus",
    )

    fake_cfg = MagicMock()
    fake_cfg.api_key = "test-key"
    fake_cfg.root_url = "https://www.sophnet.com/api/open-apis/projects/easyllms/speechtotext"
    fake_cfg.model = "Fun-ASR"

    with patch("sophagent.im.transcribe._resolve_sophnet_stt", return_value=fake_cfg), patch(
        "sophagent.im.transcribe._transcribe_sophnet",
        new=AsyncMock(return_value=("转写结果", "sophnet:Fun-ASR")),
    ) as mock_sophnet:
        out = await transcribe_audio_attachment(
            attachment,
            workspace_root=tmp_path,
            provider_name="deepseek",
        )

    mock_sophnet.assert_awaited_once()
    assert out.raw is not None
    assert out.raw["transcript"] == "转写结果"
    assert out.raw["stt_backend"] == "sophnet:Fun-ASR"


def test_resolve_sophnet_stt_from_explicit_key_only(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SOPHAGENT_STT_API_KEY", "sk-test")
    monkeypatch.delenv("SOPHAGENT_STT_BASE_URL", raising=False)
    cfg = _resolve_sophnet_stt("")
    assert cfg is not None
    assert cfg.api_key == "sk-test"
    assert cfg.model == "Fun-ASR"
    assert cfg.root_url.endswith("/easyllms/speechtotext")
