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
    assert client.delete("/api/providers/myp", headers=admin).status_code == 200


def test_provider_write_requires_admin(client, bob):
    r = client.post("/api/providers", headers=bob, json={
        "name": "np", "api_mode": "openai", "api_key": "k"})
    assert r.status_code == 403


def test_cannot_modify_builtin(client, admin):
    assert client.put("/api/providers/test", headers=admin, json={
        "api_mode": "openai", "context_limit": 1}).status_code == 400
    assert client.delete("/api/providers/test", headers=admin).status_code == 400
