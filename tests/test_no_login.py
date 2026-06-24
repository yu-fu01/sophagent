"""SOPHAGENT_NO_LOGIN: local-dev auth bypass that auto-resolves token-less
requests to the bootstrap admin. Production default is off."""

from sophagent.config import get_config


def test_no_login_disabled_by_default_requires_token(client):
    # Out of the box a protected route rejects an anonymous request.
    assert get_config().no_login is False
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_no_login_auto_admin(client):
    # With the switch on, a request carrying no bearer token is served as admin.
    get_config().no_login = True
    r = client.get("/api/auth/me")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "admin"
    assert body["is_admin"] is True


def test_no_login_honours_explicit_token(client, bob):
    # An explicit, valid token still resolves to that user (not admin).
    get_config().no_login = True
    r = client.get("/api/auth/me", headers=bob)
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "bob"


def test_no_login_endpoint_404_when_disabled(client):
    # The bootstrap endpoint is invisible unless no-login is on.
    assert get_config().no_login is False
    assert client.get("/api/auth/no-login").status_code == 404


def test_no_login_endpoint_mints_working_admin_token(client):
    # With the switch on, the token-less frontend can fetch a real admin JWT.
    get_config().no_login = True
    r = client.get("/api/auth/no-login")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "admin" and body["role"] == "admin"
    assert body["token"]
    # the minted token actually authenticates a normal request
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200 and me.json()["username"] == "admin"


def test_no_login_env_parsing(tmp_path, monkeypatch):
    from sophagent import config as config_mod

    monkeypatch.setenv("SOPHAGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SOPHAGENT_NO_LOGIN", "1")
    config_mod.reset_config()
    try:
        assert config_mod.get_config().no_login is True
    finally:
        config_mod.reset_config()
