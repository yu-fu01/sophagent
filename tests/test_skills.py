import pytest

from sophclaw.skills.store import SkillError, SkillStore, parse_frontmatter

SKILL_MD = """---
name: deploy-checklist
description: "Steps to deploy the service safely"
version: 1.0.0
metadata:
  tags: [ops, deploy]
---

# Deploy Checklist
1. run tests
2. build image
"""


@pytest.fixture
def store(tmp_path):
    return SkillStore(tmp_path / "skills")


def test_parse_frontmatter():
    meta, body = parse_frontmatter(SKILL_MD)
    assert meta["name"] == "deploy-checklist"
    assert meta["metadata"]["tags"] == ["ops", "deploy"]
    assert "# Deploy Checklist" in body


def test_create_view_index(store):
    store.create("deploy-checklist", SKILL_MD)
    assert store.version == 1
    idx = store.index()
    assert idx[0]["name"] == "deploy-checklist"
    assert idx[0]["tags"] == ["ops", "deploy"]
    assert "run tests" in store.view("deploy-checklist")


def test_index_filter(store):
    store.create("deploy-checklist", SKILL_MD)
    assert store.index(["deploy-checklist"])
    assert store.index(["other"]) == []


def test_create_validations(store):
    with pytest.raises(SkillError):
        store.create("Bad Name!", SKILL_MD)
    with pytest.raises(SkillError):
        store.create("no-frontmatter", "# just markdown")
    with pytest.raises(SkillError):  # frontmatter name mismatch
        store.create("other-name", SKILL_MD)
    store.create("deploy-checklist", SKILL_MD)
    with pytest.raises(SkillError):  # duplicate
        store.create("deploy-checklist", SKILL_MD)


def test_patch_and_edit(store):
    store.create("deploy-checklist", SKILL_MD)
    store.patch("deploy-checklist", "run tests", "run unit tests")
    assert "run unit tests" in store.view("deploy-checklist")
    with pytest.raises(SkillError):
        store.patch("deploy-checklist", "not there", "x")
    # edit must keep valid frontmatter
    with pytest.raises(SkillError):
        store.edit("deploy-checklist", "no frontmatter anymore")


def test_support_files_confined(store):
    store.create("deploy-checklist", SKILL_MD)
    store.write_support_file("deploy-checklist", "scripts/run.sh", "echo hi")
    assert (store.root / "deploy-checklist/scripts/run.sh").read_text() == "echo hi"
    with pytest.raises(SkillError):
        store.write_support_file("deploy-checklist", "../../evil.sh", "x")
    with pytest.raises(SkillError):
        store.write_support_file("deploy-checklist", "random/file.txt", "x")


def test_delete(store):
    store.create("deploy-checklist", SKILL_MD)
    store.delete("deploy-checklist")
    assert store.index() == []
    with pytest.raises(SkillError):
        store.view("deploy-checklist")


async def test_skill_manage_tool_self_evolution(ctx, tmp_path):
    """Agent-side flow: create via tool -> appears in index -> visible in prompt."""
    from sophclaw.agent.prompt import build_system_prompt
    from sophclaw.tools import load_all
    from sophclaw.tools import registry

    load_all()
    ctx.skill_store = SkillStore(tmp_path / "skills2")
    out = await registry.dispatch(
        "skill_manage", {"action": "create", "name": "deploy-checklist", "content": SKILL_MD}, ctx
    )
    assert '"ok": true' in out
    out = await registry.dispatch("skills_list", {}, ctx)
    assert "deploy-checklist" in out
    prompt = build_system_prompt(ctx.agent, ctx.skill_store.index(None), None, ctx.workspace)
    assert "deploy-checklist: Steps to deploy" in prompt
    assert "skill_manage" in prompt  # self-evolution guide present
