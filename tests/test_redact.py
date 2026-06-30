"""凭据脱敏 redact_sensitive_text 的单元测试。"""

import importlib

import pytest

from sophagent.agent import redact as R


# -- 逐类凭据被替换为 [REDACTED] --------------------------------------------

def test_vendor_prefix_openai():
    out = R.redact_sensitive_text("key is sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c done")
    assert "sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c" not in out
    assert "[REDACTED]" in out


def test_vendor_prefix_aws():
    out = R.redact_sensitive_text("aws id AKIA1234567890SOPHNET here")
    assert "AKIA1234567890SOPHNET" not in out
    assert "[REDACTED]" in out


def test_vendor_prefix_github_and_others():
    for secret in ("ghp_abcdefghij1234567890", "xai-abcdefghij1234567890abcdefghij12",
                   "tvly-abcdefghij1234567890"):
        out = R.redact_sensitive_text(f"token={secret}")
        assert secret not in out, secret
        assert "[REDACTED]" in out


def test_env_assignment():
    out = R.redact_sensitive_text("OPENAI_API_KEY=super-secret-value-123")
    assert "super-secret-value-123" not in out
    assert "OPENAI_API_KEY=" in out  # 键名保留
    assert "[REDACTED]" in out


def test_env_assignment_password():
    out = R.redact_sensitive_text("DB_PASSWORD='Pa55w0rd!2026'")
    assert "Pa55w0rd!2026" not in out
    assert "[REDACTED]" in out


def test_json_field():
    out = R.redact_sensitive_text('{"api_key": "abcd1234efgh5678", "name": "ok"}')
    assert "abcd1234efgh5678" not in out
    assert "[REDACTED]" in out
    assert "ok" in out  # 非敏感字段不动


def test_authorization_header():
    out = R.redact_sensitive_text("Authorization: Bearer abcdef.ghijkl.mnopqr")
    assert "abcdef.ghijkl.mnopqr" not in out
    assert "Authorization:" in out
    assert "[REDACTED]" in out


def test_api_key_header():
    out = R.redact_sensitive_text("x-api-key: my-opaque-key-value-9999")
    assert "my-opaque-key-value-9999" not in out
    assert "[REDACTED]" in out


def test_db_connection_string_password():
    out = R.redact_sensitive_text("postgres://admin:Pa55w0rd!2026@db.internal:5432/sophagent")
    assert "Pa55w0rd!2026" not in out
    assert "[REDACTED]" in out
    assert "admin" in out          # 用户名保留
    assert "db.internal" in out    # host 保留


def test_jwt():
    jwt = "eyJhbGciOiJIUzI1Ni}.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
    out = R.redact_sensitive_text(f"token {jwt} end")
    assert jwt not in out
    assert "[REDACTED]" in out


def test_private_key_block():
    pk = ("-----BEGIN RSA PRIVATE KEY-----\n"
          "MIIEowIBAAKCAQEA1234567890abcdef\n"
          "-----END RSA PRIVATE KEY-----")
    out = R.redact_sensitive_text(f"here is the key:\n{pk}\ndone")
    assert "MIIEowIBAAKCAQEA1234567890abcdef" not in out
    assert "[REDACTED]" in out


# -- 不误伤正常文本 ---------------------------------------------------------

def test_normal_text_untouched():
    txt = "这是一段正常的中文说明，包含代码 foo() 和路径 sophagent/agent/loop.py:42。"
    assert R.redact_sensitive_text(txt) == txt


def test_plain_numbers_and_urls_untouched():
    txt = "访问 https://example.com/docs?page=2 共 12345 行，版本 v1.2.3。"
    assert R.redact_sensitive_text(txt) == txt


# -- 开关 -------------------------------------------------------------------

def test_disabled_returns_original(monkeypatch):
    monkeypatch.setenv("SOPHAGENT_REDACT_SECRETS", "0")
    importlib.reload(R)
    try:
        secret = "sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c"
        assert R.redact_sensitive_text(f"key {secret}") == f"key {secret}"
    finally:
        monkeypatch.setenv("SOPHAGENT_REDACT_SECRETS", "1")
        importlib.reload(R)


def test_empty_and_none():
    assert R.redact_sensitive_text("") == ""
    assert R.redact_sensitive_text(None) is None


def test_uppercase_authorization_header_redacted():
    """全大写 AUTHORIZATION header 也必须脱敏（gate 大小写无关回归）。"""
    out = R.redact_sensitive_text("AUTHORIZATION: Bearer leakedtoken123456789")
    assert "leakedtoken123456789" not in out
    assert "[REDACTED]" in out


def test_multiple_credentials_in_one_text():
    """单段文本含多个不同凭据时全部脱敏。"""
    txt = ("key sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c 和 "
           "AKIA1234567890SOPHNET 还有 "
           "postgres://admin:Pa55w0rd!2026@db.internal:5432/x")
    out = R.redact_sensitive_text(txt)
    assert "sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c" not in out
    assert "AKIA1234567890SOPHNET" not in out
    assert "Pa55w0rd!2026" not in out
    assert out.count("[REDACTED]") >= 3


def test_redaction_is_idempotent():
    """对已脱敏文本再跑一次结果不变（占位符不被二次匹配）。"""
    txt = "key sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c done"
    once = R.redact_sensitive_text(txt)
    twice = R.redact_sensitive_text(once)
    assert once == twice
    assert "[REDACTED]" in once


# -- redact_known_secrets：按配置中的实际密钥值脱敏（Landlock 缺失时的兜底） ----

def test_redact_known_secrets_masks_provider_key(monkeypatch, tmp_path):
    monkeypatch.setenv("SOPHAGENT_DATA_DIR", str(tmp_path / "data"))
    from sophagent import config as config_mod
    from sophagent.config import ProviderConfig
    config_mod.reset_config()
    cfg = config_mod.get_config()
    cfg.providers["p"] = ProviderConfig(
        name="p", api_mode="openai", api_key="VaDnSECRET86charsSophnetKeyValue1234567890")
    out = R.redact_known_secrets("dump: VaDnSECRET86charsSophnetKeyValue1234567890 <eof>")
    config_mod.reset_config()
    assert "VaDnSECRET86charsSophnetKeyValue1234567890" not in out
    assert "[REDACTED]" in out


def test_redact_known_secrets_masks_server_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("SOPHAGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SOPHAGENT_SECRET", "server-secret-0123456789abcdef-very-long")
    from sophagent import config as config_mod
    config_mod.reset_config()
    config_mod.get_config()
    out = R.redact_known_secrets("leaked server-secret-0123456789abcdef-very-long now")
    config_mod.reset_config()
    assert "server-secret-0123456789abcdef-very-long" not in out


def test_redact_known_secrets_noop_without_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("SOPHAGENT_DATA_DIR", str(tmp_path / "data"))
    from sophagent import config as config_mod
    config_mod.reset_config()
    config_mod.get_config()
    text = "nothing sensitive here, just prose"
    assert R.redact_known_secrets(text) == text
    config_mod.reset_config()
