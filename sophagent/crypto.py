"""Symmetric encryption for provider API keys, keyed off config.secret.

The Fernet key is derived from the server secret with HKDF so the secret can
be any length; ciphertext is urlsafe-base64 text safe to store in a TEXT column.
"""
from __future__ import annotations

import base64

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def _fernet(secret: str) -> Fernet:
    # salt=None: the server secret already carries sufficient entropy; a random salt would need per-key storage
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"sophagent-provider-key").derive(secret.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt(plaintext: str, secret: str) -> str:
    return _fernet(secret).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str, secret: str) -> str:
    return _fernet(secret).decrypt(token.encode("ascii")).decode("utf-8")


def mask_key(plaintext: str) -> str:
    if not plaintext:
        return ""
    if len(plaintext) <= 7:
        return "***"
    return f"{plaintext[:3]}***{plaintext[-4:]}"
