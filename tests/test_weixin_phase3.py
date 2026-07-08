"""Tests for Weixin CDN and formatting helpers."""

import pytest

from sophagent.im.platforms.weixin.cdn import (
    aes128_ecb_decrypt,
    aes128_ecb_encrypt,
    aes_padded_size,
    parse_aes_key,
)
from sophagent.im.platforms.weixin.formatting import split_text_for_delivery, truncate_message
from sophagent.im.platforms.weixin.media import extract_outbound_file_paths


class TestCdnCrypto:
    def test_aes_roundtrip(self):
        key = b"0" * 16
        plain = b"hello weixin media"
        enc = aes128_ecb_encrypt(plain, key)
        assert aes128_ecb_decrypt(enc, key) == plain

    def test_aes_padded_size(self):
        assert aes_padded_size(1) == 16
        assert aes_padded_size(16) == 32

    def test_parse_aes_key_raw_bytes(self):
        import base64
        key = parse_aes_key(base64.b64encode(b"\x00" * 16).decode())
        assert key == b"\x00" * 16


class TestFormatting:
    def test_truncate_message(self):
        assert truncate_message("abc", 10) == ["abc"]
        assert truncate_message("a" * 25, 10) == ["a" * 10, "a" * 10, "a" * 5]

    def test_split_short_chatty_lines(self):
        chunks = split_text_for_delivery("第一行\n第二行\n第三行", 2000)
        assert chunks == ["第一行", "第二行", "第三行"]

    def test_split_long_content_by_blocks(self):
        text = "# Title\n\n" + ("paragraph " * 300)
        chunks = split_text_for_delivery(text, 500)
        assert len(chunks) > 1
        assert all(len(c) <= 500 for c in chunks)


class TestOutboundPaths:
    def test_extract_attachment_refs(self):
        text = '回复如下\n[附加文件: workspace/im/weixin/a.png]'
        cleaned, paths = extract_outbound_file_paths(text)
        assert "附加文件" not in cleaned
        assert paths[0].as_posix().endswith("workspace/im/weixin/a.png")
