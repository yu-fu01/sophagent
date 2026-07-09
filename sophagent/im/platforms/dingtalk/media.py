"""DingTalk inbound media: resolve download codes and materialize attachments."""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ....models import MessageAttachment

log = logging.getLogger(__name__)

_DINGTALK_OPENAPI = "https://api.dingtalk.com"
_TYPE_MAP = {
    "picture": "image",
    "photo": "image",
    "image": "image",
    "voice": "audio",
    "audio": "audio",
    "video": "video",
    "file": "file",
}
_DOWNLOAD_URL_RE = re.compile(
    r"^https://(?:[a-z0-9.-]+\.)?(?:dingtalk\.com|alicdn\.com|aliyuncs\.com)/",
    re.IGNORECASE,
)
_STANDALONE_MEDIA_TYPES = frozenset({"file", "audio", "voice"})


def sanitize_download_url(url: str) -> str:
    """Validate and normalize DingTalk CDN download URLs (http -> https)."""
    parsed = urlparse(url.strip())
    if parsed.scheme == "http":
        upgraded = f"https://{parsed.netloc}{parsed.path}"
        if parsed.query:
            upgraded += f"?{parsed.query}"
        if _DOWNLOAD_URL_RE.match(upgraded):
            return upgraded
        raise ValueError("disallowed download scheme: http")
    if parsed.scheme != "https":
        raise ValueError(f"disallowed download scheme: {parsed.scheme}")
    if not _DOWNLOAD_URL_RE.match(url):
        raise ValueError(f"download host not in allowlist: {parsed.hostname}")
    return url


def assert_download_url(url: str) -> None:
    sanitize_download_url(url)


def _standalone_content_dict(message: Any) -> dict[str, Any]:
    msg_type = str(getattr(message, "message_type", "") or "")
    if msg_type not in _STANDALONE_MEDIA_TYPES:
        return {}
    to_dict = getattr(message, "to_dict", None)
    if not callable(to_dict):
        return {}
    try:
        payload = to_dict() or {}
    except Exception:
        return {}
    content = payload.get("content")
    return content if isinstance(content, dict) else {}


def _collect_standalone_media(message: Any) -> list[_MediaItem]:
    content = _standalone_content_dict(message)
    if not content:
        return []
    code = content.get("downloadCode") or content.get("download_code") or ""
    if not code:
        return []
    msg_type = str(getattr(message, "message_type", "") or "")
    kind = _TYPE_MAP.get(msg_type, "file")
    mime = {
        "image": "image/*",
        "audio": "audio/*",
        "video": "video/*",
    }.get(kind, "application/octet-stream")
    filename = str(
        content.get("fileName")
        or content.get("filename")
        or f"media-{msg_type}"
    )
    return [_MediaItem(code=str(code), kind=kind, mime=mime, name=filename)]


@dataclass
class _MediaItem:
    code: str
    kind: str
    mime: str = ""
    name: str = ""


def message_has_media(message: Any) -> bool:
    image_content = getattr(message, "image_content", None)
    if image_content and getattr(image_content, "download_code", None):
        return True
    content = _standalone_content_dict(message)
    if content.get("downloadCode") or content.get("download_code"):
        return True
    return bool(collect_media_items(message, resolved_only=False))


def collect_media_items(message: Any, *, resolved_only: bool = True) -> list[_MediaItem]:
    """Collect media entries. Codes are opaque tokens; https URLs are post-resolve."""
    items: list[_MediaItem] = []
    image_content = getattr(message, "image_content", None)
    if image_content:
        code = getattr(image_content, "download_code", None) or ""
        if code and (not resolved_only or str(code).startswith("https://")):
            items.append(_MediaItem(code=str(code), kind="image", mime="image/*", name="image"))

    for item in _collect_standalone_media(message):
        if resolved_only and not str(item.code).startswith("https://"):
            continue
        items.append(item)

    rich_text = getattr(message, "rich_text_content", None) or getattr(message, "rich_text", None)
    if rich_text:
        rich_list = getattr(rich_text, "rich_text_list", None) or rich_text
        if isinstance(rich_list, list):
            for idx, item in enumerate(rich_list, start=1):
                if not isinstance(item, dict):
                    continue
                code = item.get("downloadCode") or item.get("pictureDownloadCode") or item.get("download_code") or ""
                if not code:
                    continue
                if resolved_only and not str(code).startswith("https://"):
                    continue
                item_type = str(item.get("type") or "")
                kind = _TYPE_MAP.get(item_type, "file")
                if item_type == "voice":
                    kind = "audio"
                mime = {
                    "image": "image/*",
                    "audio": "audio/*",
                    "video": "video/*",
                }.get(kind, "application/octet-stream")
                filename = str(item.get("fileName") or item.get("filename") or f"media-{idx}")
                items.append(_MediaItem(code=str(code), kind=kind, mime=mime, name=filename))
    return items


def _collect_download_codes(message: Any) -> list[_MediaItem]:
    return collect_media_items(message, resolved_only=False)


def media_placeholder(message: Any) -> str:
    msg_type = str(getattr(message, "message_type", "") or "")
    if msg_type == "voice":
        return "[语音]"
    if msg_type in {"file", "audio"}:
        return "[文件]"
    image_content = getattr(message, "image_content", None)
    if image_content and getattr(image_content, "download_code", None):
        return "[图片]"
    rich_text = getattr(message, "rich_text_content", None) or getattr(message, "rich_text", None)
    if rich_text:
        rich_list = getattr(rich_text, "rich_text_list", None) or rich_text
        if isinstance(rich_list, list):
            kinds = {str(item.get("type") or "") for item in rich_list if isinstance(item, dict)}
            if kinds & {"voice"}:
                return "[语音]"
            if kinds & {"picture", "photo", "image"}:
                return "[图片]"
    return "[文件]"


async def fetch_download_url(
    http: httpx.AsyncClient,
    download_code: str,
    *,
    access_token: str,
    client_id: str,
) -> str | None:
    """Resolve a download code via DingTalk OpenAPI (same as dingtalk-stream SDK)."""
    if not download_code or not access_token or not client_id:
        return None
    if str(download_code).startswith("https://"):
        return str(download_code)
    url = f"{_DINGTALK_OPENAPI}/v1.0/robot/messageFiles/download"
    try:
        resp = await http.post(
            url,
            headers={
                "Content-Type": "application/json",
                "Accept": "*/*",
                "x-acs-dingtalk-access-token": access_token,
                "User-Agent": "sophagent/dingtalk-stream",
            },
            content=json.dumps({
                "robotCode": client_id,
                "downloadCode": download_code,
            }),
            timeout=30.0,
        )
        if resp.status_code >= 300:
            log.warning(
                "dingtalk: fetch download url HTTP %s code=%s body=%s",
                resp.status_code,
                str(download_code)[:24],
                resp.text[:200],
            )
            return None
        data = resp.json()
        resolved = data.get("downloadUrl") or data.get("download_url")
        if not resolved:
            log.warning(
                "dingtalk: fetch download url empty code=%s body=%s",
                str(download_code)[:24],
                str(data)[:200],
            )
        return str(resolved) if resolved else None
    except Exception as exc:
        log.warning("dingtalk: fetch download url failed code=%s: %s", str(download_code)[:24], exc)
        return None


async def download_inbound_attachments(
    http: httpx.AsyncClient,
    message: Any,
    *,
    inbound_dir: Path,
    access_token: str,
    client_id: str,
) -> list[MessageAttachment]:
    inbound_dir.mkdir(parents=True, exist_ok=True)
    entries = _collect_download_codes(message)
    if entries:
        log.info(
            "dingtalk media: msg_type=%s codes=%d",
            getattr(message, "message_type", "?"),
            len(entries),
        )
    attachments: list[MessageAttachment] = []
    for item in entries:
        url = await fetch_download_url(
            http,
            item.code,
            access_token=access_token,
            client_id=client_id,
        )
        if not url:
            continue
        try:
            url = sanitize_download_url(url)
        except ValueError as exc:
            log.warning("dingtalk: rejected media url: %s", exc)
            continue
        try:
            resp = await http.get(url, timeout=60.0)
            resp.raise_for_status()
            ext = _guess_ext(item.kind, resp.headers.get("content-type", ""), item.name)
            filename = f"{uuid.uuid4().hex[:12]}{ext}"
            path = inbound_dir / filename
            path.write_bytes(resp.content)
            attachments.append(MessageAttachment(
                kind=item.kind,
                path=str(path),
                name=item.name or filename,
                mime=item.mime or (resp.headers.get("content-type") or ""),
                size=len(resp.content),
                platform="dingtalk",
                raw={"source_url": url, "download_code": item.code},
            ))
        except Exception as exc:
            log.warning("dingtalk: download media failed url=%s: %s", url[:80], exc)
    if entries:
        log.info("dingtalk media: downloaded %d/%d", len(attachments), len(entries))
    return attachments


def _guess_ext(kind: str, content_type: str, name: str = "") -> str:
    if name and "." in name:
        suffix = Path(name).suffix
        if suffix:
            return suffix
    ct = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "audio/mpeg": ".mp3",
        "audio/amr": ".amr",
        "application/pdf": ".pdf",
        "video/mp4": ".mp4",
    }
    if ct in mapping:
        return mapping[ct]
    return {
        "image": ".jpg",
        "audio": ".amr",
        "video": ".mp4",
        "file": ".bin",
    }.get(kind, ".bin")
