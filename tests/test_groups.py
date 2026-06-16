"""Multi-user group model tests (REQ1.1-1.8)."""

from conftest import make_user


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
