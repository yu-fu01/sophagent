"""Weixin inbound/outbound media via iLink CDN."""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import re
import secrets
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

import aiohttp

from ....models import MessageAttachment
from . import cdn as wx_cdn

log = logging.getLogger(__name__)

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4

EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_SEND_TYPING = "ilink/bot/sendtyping"

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

TYPING_START = 1
TYPING_STOP = 2

_ATTACHMENT_REF_RE = re.compile(r"\[附加文件:\s*([^\]]+)\]")


def media_reference(item: dict[str, Any], key: str) -> dict[str, Any]:
    return (item.get(key) or {}).get("media") or {}


def mime_from_filename(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def attachment_kind_for_mime(mime: str, filename: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/") or filename.endswith(".silk"):
        return "audio"
    return "file"


class TypingTicketCache:
    def __init__(self, ttl_seconds: float = 600.0) -> None:
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[str, float]] = {}

    def get(self, user_id: str) -> str | None:
        import time
        entry = self._cache.get(user_id)
        if not entry:
            return None
        if time.time() - entry[1] >= self._ttl:
            self._cache.pop(user_id, None)
            return None
        return entry[0]

    def set(self, user_id: str, ticket: str) -> None:
        import time
        self._cache[user_id] = (ticket, time.time())


async def download_inbound_attachments(
    session: aiohttp.ClientSession,
    item_list: list[dict[str, Any]],
    *,
    cdn_base_url: str,
    inbound_dir: Path,
) -> list[MessageAttachment]:
    inbound_dir.mkdir(parents=True, exist_ok=True)
    out: list[MessageAttachment] = []
    for item in item_list:
        att = await _download_item(session, item, cdn_base_url=cdn_base_url, inbound_dir=inbound_dir)
        if att:
            out.append(att)
        ref_item = (item.get("ref_msg") or {}).get("message_item")
        if isinstance(ref_item, dict):
            att = await _download_item(session, ref_item, cdn_base_url=cdn_base_url, inbound_dir=inbound_dir)
            if att:
                out.append(att)
    return out


async def _download_item(
    session: aiohttp.ClientSession,
    item: dict[str, Any],
    *,
    cdn_base_url: str,
    inbound_dir: Path,
) -> MessageAttachment | None:
    item_type = item.get("type")
    if item_type == ITEM_TEXT:
        return None
    if item_type == ITEM_VOICE and (item.get("voice_item") or {}).get("text"):
        return None

    spec = _item_download_spec(item)
    if spec is None:
        return None
    suffix, mime, timeout = spec
    media = media_reference(item, _item_key(item_type))
    aes_key = media.get("aes_key")
    image_item = item.get("image_item") or {}
    if item_type == ITEM_IMAGE and image_item.get("aeskey"):
        aes_key = base64.b64encode(bytes.fromhex(str(image_item["aeskey"]))).decode("ascii")

    try:
        data = await wx_cdn.download_and_decrypt_media(
            session,
            cdn_base_url=cdn_base_url,
            encrypted_query_param=media.get("encrypt_query_param"),
            aes_key_b64=aes_key,
            full_url=media.get("full_url"),
            timeout_seconds=timeout,
        )
    except Exception as exc:
        log.warning("weixin: inbound media download failed type=%s: %s", item_type, exc)
        return None

    name = _default_name(item, suffix)
    path = inbound_dir / f"{uuid.uuid4().hex}{suffix}"
    path.write_bytes(data)
    return MessageAttachment(
        kind=attachment_kind_for_mime(mime, name),
        path=str(path),
        name=name,
        mime=mime,
        size=len(data),
        platform="weixin",
    )


def _item_key(item_type: int) -> str:
    return {
        ITEM_IMAGE: "image_item",
        ITEM_VIDEO: "video_item",
        ITEM_FILE: "file_item",
        ITEM_VOICE: "voice_item",
    }.get(item_type, "")


def _item_download_spec(item: dict[str, Any]) -> tuple[str, str, float] | None:
    item_type = item.get("type")
    if item_type == ITEM_IMAGE:
        return ".jpg", "image/jpeg", 30.0
    if item_type == ITEM_VIDEO:
        return ".mp4", "video/mp4", 120.0
    if item_type == ITEM_FILE:
        filename = str((item.get("file_item") or {}).get("file_name") or "document.bin")
        return Path(filename).suffix or ".bin", mime_from_filename(filename), 60.0
    if item_type == ITEM_VOICE:
        return ".silk", "audio/silk", 60.0
    return None


def _default_name(item: dict[str, Any], suffix: str) -> str:
    if item.get("type") == ITEM_FILE:
        return str((item.get("file_item") or {}).get("file_name") or f"file{suffix}")
    return f"weixin{suffix}"


def extract_outbound_file_paths(text: str) -> tuple[str, list[Path]]:
    paths: list[Path] = []
    cleaned = text
    for match in _ATTACHMENT_REF_RE.finditer(text):
        raw = match.group(1).strip().strip('"').strip("'")
        if raw:
            paths.append(Path(raw))
        cleaned = cleaned.replace(match.group(0), "").strip()
    return cleaned, paths


def outbound_media_builder(path: Path, *, force_file: bool = False) -> tuple[int, Callable[..., dict[str, Any]]]:
    mime = mime_from_filename(path.name)
    if mime.startswith("image/"):
        return MEDIA_IMAGE, lambda **kw: {
            "type": ITEM_IMAGE,
            "image_item": {
                "media": {
                    "encrypt_query_param": kw["encrypt_query_param"],
                    "aes_key": kw["aes_key_for_api"],
                    "encrypt_type": 1,
                },
                "mid_size": kw["ciphertext_size"],
            },
        }
    if mime.startswith("video/"):
        return MEDIA_VIDEO, lambda **kw: {
            "type": ITEM_VIDEO,
            "video_item": {
                "media": {
                    "encrypt_query_param": kw["encrypt_query_param"],
                    "aes_key": kw["aes_key_for_api"],
                    "encrypt_type": 1,
                },
                "video_size": kw["ciphertext_size"],
                "play_length": 0,
                "video_md5": kw.get("rawfilemd5", ""),
            },
        }
    if path.suffix.lower() == ".silk" and not force_file:
        return MEDIA_VOICE, lambda **kw: {
            "type": ITEM_VOICE,
            "voice_item": {
                "media": {
                    "encrypt_query_param": kw["encrypt_query_param"],
                    "aes_key": kw["aes_key_for_api"],
                    "encrypt_type": 1,
                },
                "encode_type": 6,
                "sample_rate": 24000,
                "bits_per_sample": 16,
                "playtime": 0,
            },
        }
    return MEDIA_FILE, lambda **kw: {
        "type": ITEM_FILE,
        "file_item": {
            "file_name": path.name,
            "media": {
                "encrypt_query_param": kw["encrypt_query_param"],
                "aes_key": kw["aes_key_for_api"],
                "encrypt_type": 1,
            },
            "file_size": kw["plaintext_size"],
            "file_md5": kw.get("rawfilemd5", ""),
        },
    }


async def send_file(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    cdn_base_url: str,
    chat_id: str,
    path: Path,
    context_token: str | None,
    api_post: Callable[..., Any],
    caption: str = "",
) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    plaintext = path.read_bytes()
    media_type, item_builder = outbound_media_builder(path)
    filekey = secrets.token_hex(16)
    aes_key = secrets.token_bytes(16)
    rawsize = len(plaintext)
    rawfilemd5 = hashlib.md5(plaintext).hexdigest()
    upload_response = await api_post(
        session,
        base_url=base_url,
        endpoint=EP_GET_UPLOAD_URL,
        payload={
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": chat_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": wx_cdn.aes_padded_size(rawsize),
            "no_need_thumb": True,
            "aeskey": aes_key.hex(),
        },
        token=token,
        timeout_ms=15_000,
    )
    upload_param = str(upload_response.get("upload_param") or "")
    upload_full_url = str(upload_response.get("upload_full_url") or "")
    ciphertext = wx_cdn.aes128_ecb_encrypt(plaintext, aes_key)
    if upload_full_url:
        upload_url = upload_full_url
    elif upload_param:
        upload_url = wx_cdn.cdn_upload_url(cdn_base_url, upload_param, filekey)
    else:
        raise RuntimeError(f"getUploadUrl missing upload target: {upload_response}")

    encrypted_query_param = await wx_cdn.upload_ciphertext(
        session, ciphertext=ciphertext, upload_url=upload_url,
    )
    aes_key_for_api = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")
    media_item = item_builder(
        encrypt_query_param=encrypted_query_param,
        aes_key_for_api=aes_key_for_api,
        ciphertext_size=len(ciphertext),
        plaintext_size=rawsize,
        rawfilemd5=rawfilemd5,
    )

    if caption:
        await api_post(
            session,
            base_url=base_url,
            endpoint=EP_SEND_MESSAGE,
            payload={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": chat_id,
                    "client_id": str(uuid.uuid4()),
                    "message_type": MSG_TYPE_BOT,
                    "message_state": MSG_STATE_FINISH,
                    "item_list": [{"type": 1, "text_item": {"text": caption}}],
                    **({"context_token": context_token} if context_token else {}),
                }
            },
            token=token,
            timeout_ms=15_000,
        )

    await api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_MESSAGE,
        payload={
            "msg": {
                "from_user_id": "",
                "to_user_id": chat_id,
                "client_id": str(uuid.uuid4()),
                "message_type": MSG_TYPE_BOT,
                "message_state": MSG_STATE_FINISH,
                "item_list": [media_item],
                **({"context_token": context_token} if context_token else {}),
            }
        },
        token=token,
        timeout_ms=15_000,
    )
