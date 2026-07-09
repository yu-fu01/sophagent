"""Speech-to-text for IM audio attachments.

Sophnet uses EasyLLM ASR (``Fun-ASR``), not OpenAI ``/audio/transcriptions``.
When the agent provider or STT env points at sophnet.com, we call:

``POST .../easyllms/speechtotext/transcriptions`` with ``file_urls`` (upload to OSS first).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..models import MessageAttachment
from ..providers.registry import get_registry

log = logging.getLogger(__name__)

_AUDIO_SUFFIXES = {".opus", ".ogg", ".mp3", ".wav", ".m4a", ".amr", ".silk", ".aac", ".webm", ".bin"}
_SOPHNET_STT_PATH = "/api/open-apis/projects/easyllms/speechtotext"
_SOPHNET_UPLOAD_PATH = "/api/open-apis/projects/upload"
_DEFAULT_SOPHNET_HOST = "https://www.sophnet.com"
_DEFAULT_SOPHNET_MODEL = "Fun-ASR"
_OPENAI_BASE = os.getenv("STT_OPENAI_BASE_URL", "https://api.openai.com/v1")
_GROQ_BASE = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

_AUDIO_MIME = {
    ".opus": "audio/opus",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".amr": "audio/amr",
    ".aac": "audio/aac",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
    ".silk": "audio/silk",
}


@dataclass(frozen=True)
class _SttBackend:
    name: str
    api_key: str
    base_url: str
    model: str


@dataclass(frozen=True)
class _SophnetSttConfig:
    api_key: str
    root_url: str
    model: str


def _resolve_audio_path(workspace_root: Path, rel_path: str) -> Path | None:
    if not rel_path:
        return None
    try:
        root = workspace_root.resolve()
        path = (root / rel_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def _is_sophnet_url(url: str) -> bool:
    return "sophnet.com" in (url or "").lower()


def _sophnet_host_from_url(url: str) -> str:
    parsed = urlparse(url or _DEFAULT_SOPHNET_HOST)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return _DEFAULT_SOPHNET_HOST


def _normalize_sophnet_stt_root(base_url: str, *, agent_base_url: str = "") -> str:
    raw = (base_url or "").strip()
    if not raw:
        return _sophnet_host_from_url(agent_base_url) + _SOPHNET_STT_PATH
    raw = raw.rstrip("/")
    if raw.endswith("/speechtotext"):
        return raw
    if "/easyllms/" in raw:
        prefix = raw.split("/easyllms/")[0]
        return f"{prefix}/easyllms/speechtotext"
    if _is_sophnet_url(raw):
        return _sophnet_host_from_url(raw) + _SOPHNET_STT_PATH
    return raw


def _resolve_sophnet_stt(agent_provider_name: str = "") -> _SophnetSttConfig | None:
    explicit_key = (os.getenv("SOPHAGENT_STT_API_KEY") or os.getenv("STT_API_KEY") or "").strip()
    explicit_base = (os.getenv("SOPHAGENT_STT_BASE_URL") or os.getenv("STT_BASE_URL") or "").strip()
    model = (os.getenv("SOPHAGENT_STT_MODEL") or os.getenv("STT_MODEL") or _DEFAULT_SOPHNET_MODEL).strip()

    agent_key = ""
    agent_base = ""
    if agent_provider_name:
        try:
            resolved = get_registry().resolve(agent_provider_name)
            agent_key = (resolved.api_key or "").strip()
            agent_base = (resolved.base_url or "").strip()
        except (KeyError, RuntimeError):
            pass

    api_key = explicit_key or agent_key
    if not api_key:
        return None

    use_sophnet = (
        (explicit_key and not explicit_base)
        or _is_sophnet_url(explicit_base)
        or (not explicit_base and _is_sophnet_url(agent_base))
    )
    if not use_sophnet:
        return None

    root = _normalize_sophnet_stt_root(explicit_base, agent_base_url=agent_base)
    return _SophnetSttConfig(api_key=api_key, root_url=root, model=model or _DEFAULT_SOPHNET_MODEL)


def _stt_backends(agent_provider_name: str = "") -> list[_SttBackend]:
    """OpenAI-compatible backends (non-sophnet only)."""
    out: list[_SttBackend] = []

    explicit_key = (os.getenv("SOPHAGENT_STT_API_KEY") or os.getenv("STT_API_KEY") or "").strip()
    explicit_base = (os.getenv("SOPHAGENT_STT_BASE_URL") or os.getenv("STT_BASE_URL") or "").strip()
    explicit_model = (os.getenv("SOPHAGENT_STT_MODEL") or os.getenv("STT_MODEL") or "whisper-1").strip()
    if explicit_key and explicit_base and not _is_sophnet_url(explicit_base):
        out.append(_SttBackend("configured", explicit_key, explicit_base, explicit_model))

    groq_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if groq_key:
        out.append(_SttBackend(
            "groq",
            groq_key,
            _GROQ_BASE,
            os.getenv("STT_GROQ_MODEL", "whisper-large-v3-turbo"),
        ))

    openai_key = (os.getenv("OPENAI_API_KEY") or os.getenv("VOICE_TOOLS_OPENAI_KEY") or "").strip()
    if openai_key:
        out.append(_SttBackend(
            "openai",
            openai_key,
            _OPENAI_BASE,
            os.getenv("STT_OPENAI_MODEL", "whisper-1"),
        ))

    if agent_provider_name and not _resolve_sophnet_stt(agent_provider_name):
        try:
            resolved = get_registry().resolve(agent_provider_name)
        except (KeyError, RuntimeError):
            resolved = None
        if resolved and resolved.api_mode == "openai" and resolved.api_key:
            out.append(_SttBackend(
                f"agent:{agent_provider_name}",
                resolved.api_key,
                resolved.base_url or _OPENAI_BASE,
                "whisper-1",
            ))

    deduped: list[_SttBackend] = []
    seen: set[tuple[str, str]] = set()
    for backend in out:
        key = (backend.api_key, backend.base_url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(backend)
    return deduped


def _extract_sophnet_text(body: dict) -> str:
    legacy = body.get("result")
    if isinstance(legacy, str) and legacy.strip():
        return legacy.strip()

    parts: list[str] = []
    for item in body.get("results") or []:
        if not isinstance(item, dict):
            continue
        for transcript in item.get("transcripts") or []:
            if not isinstance(transcript, dict):
                continue
            text = str(transcript.get("text") or "").strip()
            if text:
                parts.append(text)
    return " ".join(parts).strip()


def _guess_audio_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _AUDIO_MIME:
        return _AUDIO_MIME[suffix]
    if suffix:
        return "application/octet-stream"
    return "audio/opus"


def _sophnet_upload_url(root_url: str) -> str:
    host = _sophnet_host_from_url(root_url)
    return host + _SOPHNET_UPLOAD_PATH


def _parse_sophnet_upload_url(body: dict) -> str:
    result = body.get("result")
    if isinstance(result, dict):
        for key in ("signedUrl", "signed_url", "url"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(result, str) and result.strip():
        return result.strip()
    for key in ("signedUrl", "url", "data"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def _upload_sophnet_file(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    upload_url: str,
    path: Path,
) -> str:
    content = path.read_bytes()
    upload_name = path.name
    if path.suffix.lower() not in _AUDIO_SUFFIXES:
        upload_name = f"{path.name}.opus"
    resp = await client.post(
        upload_url,
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (upload_name, content, _guess_audio_mime(path))},
    )
    if resp.status_code >= 400:
        log.warning("sophnet upload failed status=%s body=%s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    signed_url = _parse_sophnet_upload_url(resp.json())
    if not signed_url:
        raise RuntimeError(f"sophnet upload missing signedUrl: {resp.text[:300]}")
    return signed_url


async def _poll_sophnet_task(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    root_url: str,
    task_id: str,
    timeout_s: float = 90.0,
) -> str:
    poll_url = f"{root_url.rstrip('/')}/transcriptions/{task_id}"
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_status = ""
    while asyncio.get_running_loop().time() < deadline:
        resp = await client.get(
            poll_url,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        body = resp.json()
        status = str(body.get("status") or body.get("task_status") or "").lower()
        last_status = status or last_status
        if status in {"success", "succeeded"}:
            text = _extract_sophnet_text(body)
            if text:
                return text
            raise RuntimeError("sophnet asr succeeded but returned empty text")
        if status in {"failed", "fail", "error"}:
            raise RuntimeError(str(body.get("errorMsg") or body.get("message") or "sophnet asr failed"))
        await asyncio.sleep(1.5)
    raise TimeoutError(f"sophnet asr poll timed out (last_status={last_status or 'unknown'})")


async def _transcribe_sophnet(path: Path, cfg: _SophnetSttConfig) -> tuple[str, str]:
    create_url = f"{cfg.root_url.rstrip('/')}/transcriptions"
    upload_url = _sophnet_upload_url(cfg.root_url)
    headers = {"Authorization": f"Bearer {cfg.api_key}"}

    async with httpx.AsyncClient(timeout=120.0) as client:
        signed_url = await _upload_sophnet_file(
            client,
            api_key=cfg.api_key,
            upload_url=upload_url,
            path=path,
        )
        payload = {
            "file_urls": [signed_url],
            "speech_recognition_param": {"model": cfg.model},
        }
        resp = await client.post(
            create_url,
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code >= 400:
            log.warning("sophnet asr create failed status=%s body=%s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        body = resp.json()
        task_id = str(body.get("taskId") or body.get("task_id") or "").strip()
        if not task_id:
            text = _extract_sophnet_text(body)
            if text:
                return text, f"sophnet:{cfg.model}"
            raise RuntimeError(f"sophnet asr missing task id: {body}")

        text = await _poll_sophnet_task(
            client,
            api_key=cfg.api_key,
            root_url=cfg.root_url,
            task_id=task_id,
        )
        return text, f"sophnet:{cfg.model}"


async def _transcribe_openai(path: Path, backends: list[_SttBackend]) -> tuple[str, str]:
    from openai import AsyncOpenAI

    last_error = ""
    for backend in backends:
        client = AsyncOpenAI(api_key=backend.api_key, base_url=backend.base_url or None)
        try:
            with path.open("rb") as handle:
                result = await client.audio.transcriptions.create(
                    model=backend.model,
                    file=handle,
                )
            text = str(getattr(result, "text", "") or result or "").strip()
            if text:
                log.info("audio transcribed via %s (%d chars)", backend.name, len(text))
                return text, backend.name
        except Exception as exc:
            last_error = str(exc)
            log.warning("audio transcribe via %s failed: %s", backend.name, exc)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                await close()
    if last_error:
        log.warning("audio transcribe exhausted backends path=%s last_error=%s", path.name, last_error)
    return "", ""


async def transcribe_audio_attachment(
    attachment: MessageAttachment,
    *,
    workspace_root: Path,
    provider_name: str = "",
) -> MessageAttachment:
    """Fill attachment.raw['transcript'] when transcription succeeds."""
    if attachment.kind != "audio":
        return attachment
    if attachment.raw and attachment.raw.get("transcript"):
        return attachment
    path = _resolve_audio_path(workspace_root, attachment.path)
    if path is None:
        return attachment
    if path.suffix.lower() not in _AUDIO_SUFFIXES and attachment.mime:
        if not str(attachment.mime).lower().startswith("audio/"):
            return attachment

    sophnet_cfg = _resolve_sophnet_stt(provider_name)
    text = ""
    backend_name = ""
    if sophnet_cfg:
        try:
            text, backend_name = await _transcribe_sophnet(path, sophnet_cfg)
            if text:
                log.info("audio transcribed via %s (%d chars)", backend_name, len(text))
        except Exception as exc:
            log.warning("sophnet asr failed path=%s error=%s", path.name, exc)
    else:
        backends = _stt_backends(provider_name)
        if not backends:
            log.debug("audio transcribe skipped: no STT backend configured")
            return attachment
        text, backend_name = await _transcribe_openai(path, backends)

    if not text:
        return attachment

    raw = dict(attachment.raw or {})
    raw["transcript"] = text
    raw["stt_backend"] = backend_name
    return MessageAttachment(
        kind=attachment.kind,
        path=attachment.path,
        name=attachment.name,
        mime=attachment.mime,
        size=attachment.size,
        platform=attachment.platform,
        raw=raw,
    )
