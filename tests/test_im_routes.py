"""IM pair-code REST route."""


def test_issue_pair_code(client, bob, agent_id):
    r = client.post("/api/im/pair-code", json={"agent_id": agent_id}, headers=bob)
    assert r.status_code == 200
    code = r.json()["code"]
    assert len(code) == 8


def test_issue_pair_code_unknown_agent(client, bob):
    r = client.post("/api/im/pair-code", json={"agent_id": 99999}, headers=bob)
    assert r.status_code == 404
