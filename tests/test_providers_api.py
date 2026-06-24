"""Tests for provider CRUD + model-list API endpoints."""


def test_provider_crud_and_mask(client, admin):
    r = client.post("/api/providers", headers=admin, json={
        "name": "myp", "api_mode": "openai", "base_url": "https://x/v1",
        "api_key": "sk-abcdefgh1234", "context_limit": 64000})
    assert r.status_code == 201, r.text
    lst = client.get("/api/providers", headers=admin).json()
    me = next(p for p in lst if p["name"] == "myp")
    assert me["api_key"] == "sk-***1234" and me["source"] == "db"
    bt = next(p for p in lst if p["name"] == "test")
    assert bt["source"] == "builtin"
    r = client.put("/api/providers/myp", headers=admin, json={
        "api_mode": "openai", "base_url": "https://y/v1", "context_limit": 128000})
    assert r.status_code == 200
    after = client.get("/api/providers", headers=admin).json()
    assert next(p for p in after if p["name"] == "myp")["base_url"] == "https://y/v1"
    assert client.delete("/api/providers/myp", headers=admin).status_code == 200


def test_provider_write_requires_admin(client, bob):
    r = client.post("/api/providers", headers=bob, json={
        "name": "np", "api_mode": "openai", "api_key": "k"})
    assert r.status_code == 403


def test_provider_list_requires_admin(client, bob):
    assert client.get("/api/providers", headers=bob).status_code == 403


def test_cannot_modify_builtin(client, admin):
    assert client.put("/api/providers/test", headers=admin, json={
        "api_mode": "openai", "context_limit": 1}).status_code == 400
    assert client.delete("/api/providers/test", headers=admin).status_code == 400


def test_models_unknown_provider_404(client, admin):
    assert client.get("/api/providers/nope/models", headers=admin).status_code == 404


def test_provider_default_model_parsed_from_yaml(tmp_path, monkeypatch):
    """`model:` in providers.yaml becomes ProviderConfig.default_model."""
    from sophagent import config as config_mod

    data = tmp_path / "data"
    data.mkdir()
    (data / "providers.yaml").write_text(
        "providers:\n  p1:\n    api_mode: openai\n    model: DeepSeek-V4-Flash\n",
        encoding="utf-8")
    monkeypatch.setenv("SOPHAGENT_DATA_DIR", str(data))
    config_mod.reset_config()
    try:
        assert config_mod.get_config().providers["p1"].default_model == "DeepSeek-V4-Flash"
    finally:
        config_mod.reset_config()


def test_models_endpoint_exposes_configured_default(client, admin, monkeypatch):
    """The models endpoint surfaces the provider's configured default model so
    the new-agent form can preselect it instead of the first listed model."""
    from sophagent.providers.registry import get_registry

    reg = get_registry()
    reg.resolve("test").default_model = "DeepSeek-V4-Flash"

    async def fake_list(name, ttl=300.0):
        return ["m-a", "DeepSeek-V4-Flash", "m-b"]

    monkeypatch.setattr(reg, "list_models", fake_list)
    body = client.get("/api/providers/test/models", headers=admin).json()
    assert body["models"] == ["m-a", "DeepSeek-V4-Flash", "m-b"]
    assert body["default"] == "DeepSeek-V4-Flash"


def test_models_endpoint_default_falls_back_to_first(client, admin, monkeypatch):
    """With no configured default, the endpoint falls back to the first model."""
    from sophagent.providers.registry import get_registry

    reg = get_registry()
    reg.resolve("test").default_model = ""

    async def fake_list(name, ttl=300.0):
        return ["first-model", "second-model"]

    monkeypatch.setattr(reg, "list_models", fake_list)
    body = client.get("/api/providers/test/models", headers=admin).json()
    assert body["default"] == "first-model"
