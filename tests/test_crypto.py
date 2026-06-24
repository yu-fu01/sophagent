import pytest
from cryptography.fernet import InvalidToken

from sophagent.crypto import encrypt, decrypt, mask_key

SECRET = "0" * 64

def test_encrypt_decrypt_roundtrip():
    token = encrypt("sk-secret-1234", SECRET)
    assert token != "sk-secret-1234"
    assert decrypt(token, SECRET) == "sk-secret-1234"

def test_decrypt_with_wrong_secret_raises():
    token = encrypt("abc", SECRET)
    with pytest.raises(InvalidToken):
        decrypt(token, "1" * 64)

def test_mask_key():
    assert mask_key("sk-abcdefgh1234") == "sk-***1234"
    assert mask_key("short") == "***"
    assert mask_key("") == ""
