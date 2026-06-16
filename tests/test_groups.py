"""Multi-user group model tests (REQ1.1-1.8)."""

import aiosqlite
import pytest

from conftest import make_user

from sophclaw.db import Database

# An excerpt of the pre-multiuser schema (no groups, agents UNIQUE(name), no group_id).
OLD_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL);
CREATE TABLE agents (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '',
  system_prompt TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
  tools TEXT NOT NULL DEFAULT '[]', skills TEXT, max_iterations INTEGER DEFAULT 30, temperature REAL,
  created_by INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, agent_id INTEGER NOT NULL,
  title TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


@pytest.mark.asyncio
async def test_migration_from_pre_multiuser_db(tmp_path):
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as c:
        await c.executescript(OLD_SCHEMA)
        await c.execute("INSERT INTO users (id, username, password_hash, role, created_at)"
                        " VALUES (1,'root','h','admin','t'),(2,'carol','h','user','t')")
        await c.execute("INSERT INTO agents (id, name, system_prompt, provider, model, created_at, updated_at)"
                        " VALUES (1,'legacy','p','test','m','t','t')")
        await c.execute("INSERT INTO sessions (id, user_id, agent_id, created_at, updated_at)"
                        " VALUES ('s1',2,1,'t','t')")
        await c.commit()

    db = Database(path)
    await db.connect()
    await db.ensure_groups()
    try:
        ag = await db.get_admin_group()
        assert ag is not None and ag["owner_id"] == 1            # root admin = first admin
        agent = await db.get_agent(1)
        assert agent["group_id"] == ag["id"]                     # legacy agent -> admin group
        sess = await db.get_session("s1")
        carol_group = await db.get_owned_group(2)
        assert sess["group_id"] == carol_group["id"]             # session -> creator's personal group
        # idempotent: a second pass changes nothing
        await db.ensure_groups()
        assert len(await db.list_groups()) == 2
    finally:
        await db.close()


# -- Slice 1: group bootstrap, personal groups, admin derivation -------------


def test_admin_has_admin_group(client, admin):
    groups = client.get("/api/groups", headers=admin).json()
    assert len(groups) == 1
    g = groups[0]
    assert g["is_admin_group"] == 1
    assert g["role"] == "owner"


def test_me_reports_admin_flag(client, admin, bob):
    assert client.get("/api/auth/me", headers=admin).json()["is_admin"] is True
    assert client.get("/api/auth/me", headers=bob).json()["is_admin"] is False


def test_new_user_gets_personal_group(client, admin, bob):
    groups = client.get("/api/groups", headers=bob).json()
    assert len(groups) == 1
    g = groups[0]
    assert g["is_admin_group"] == 0
    assert g["role"] == "owner"          # each user owns their personal group (REQ1.7)


def test_groups_are_isolated_between_users(client, admin, bob):
    alice = make_user(client, admin, "alice", "alicepw1")
    bob_gids = {g["id"] for g in client.get("/api/groups", headers=bob).json()}
    alice_gids = {g["id"] for g in client.get("/api/groups", headers=alice).json()}
    assert bob_gids.isdisjoint(alice_gids)


def test_admin_sees_all_groups(client, admin, bob):
    make_user(client, admin, "alice", "alicepw1")
    # admin group + bob's + alice's = 3
    assert len(client.get("/api/groups", headers=admin).json()) == 3


# -- Slice 2: agent group scoping --------------------------------------------

AGENT_BODY = {
    "name": "a1", "system_prompt": "p", "provider": "test", "model": "m", "tools": [],
}


def test_owner_creates_agent_in_own_group(client, admin, bob):
    resp = client.post("/api/agents", json=AGENT_BODY, headers=bob)
    assert resp.status_code == 201, resp.text
    g = client.get("/api/groups", headers=bob).json()[0]
    assert resp.json()["group_id"] == g["id"]


def test_agent_visible_only_within_group(client, admin, bob):
    alice = make_user(client, admin, "alice", "alicepw1")
    aid = client.post("/api/agents", json=AGENT_BODY, headers=bob).json()["id"]
    assert aid in [a["id"] for a in client.get("/api/agents", headers=bob).json()]
    assert aid not in [a["id"] for a in client.get("/api/agents", headers=alice).json()]
    # global admin sees every group's agents
    assert aid in [a["id"] for a in client.get("/api/agents", headers=admin).json()]


def test_outsider_cannot_modify_or_delete_agent(client, admin, bob):
    alice = make_user(client, admin, "alice", "alicepw1")
    aid = client.post("/api/agents", json=AGENT_BODY, headers=bob).json()["id"]
    assert client.put(f"/api/agents/{aid}", json=AGENT_BODY, headers=alice).status_code in (403, 404)
    assert client.delete(f"/api/agents/{aid}", headers=alice).status_code in (403, 404)
    # owner can delete
    assert client.delete(f"/api/agents/{aid}", headers=bob).status_code == 200


def test_admin_agent_defaults_to_admin_group(client, admin):
    resp = client.post("/api/agents", json=AGENT_BODY, headers=admin)
    assert resp.status_code == 201, resp.text
    admin_group = client.get("/api/groups", headers=admin).json()[0]
    assert resp.json()["group_id"] == admin_group["id"]


# -- Slice 3: session group scoping ------------------------------------------


def test_session_inherits_agent_group(client, admin, bob, agent_id):
    session = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()
    bob_gid = client.get("/api/groups", headers=bob).json()[0]["id"]
    assert session["group_id"] == bob_gid


def test_non_member_cannot_create_session(client, admin, bob, agent_id):
    alice = make_user(client, admin, "alice", "alicepw1")
    r = client.post("/api/sessions", json={"agent_id": agent_id}, headers=alice)
    assert r.status_code in (403, 404)


def test_manager_lists_and_deletes_group_sessions(client, admin, bob, agent_id):
    sid = client.post("/api/sessions", json={"agent_id": agent_id}, headers=bob).json()["id"]
    bob_gid = client.get("/api/groups", headers=bob).json()[0]["id"]
    listed = client.get(f"/api/groups/{bob_gid}/sessions", headers=admin)  # admin = global manager
    assert listed.status_code == 200
    assert sid in [s["id"] for s in listed.json()]
    assert client.delete(f"/api/sessions/{sid}", headers=admin).status_code == 200
    assert client.get(f"/api/sessions/{sid}", headers=bob).status_code == 404


def test_outsider_cannot_list_group_sessions(client, admin, bob):
    alice = make_user(client, admin, "alice", "alicepw1")
    bob_gid = client.get("/api/groups", headers=bob).json()[0]["id"]
    assert client.get(f"/api/groups/{bob_gid}/sessions", headers=alice).status_code == 403
