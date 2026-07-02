import pytest

from sophagent.models import AgentDef
from sophagent.skills.seed import builtin_dir, seed_builtin_skills
from sophagent.skills.store import SkillError, SkillStore, filter_index_for_agent, parse_frontmatter

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
    assert store.view("deploy-checklist", "scripts/run.sh") == "echo hi"
    with pytest.raises(SkillError):
        store.view("deploy-checklist", "../SKILL.md")
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
    from sophagent.agent.prompt import build_system_prompt
    from sophagent.tools import load_all
    from sophagent.tools import registry

    load_all()
    ctx.skill_store = SkillStore(tmp_path / "skills2")
    out = await registry.dispatch(
        "skill_manage", {"action": "create", "name": "deploy-checklist", "content": SKILL_MD}, ctx
    )
    assert '"ok": true' in out
    out = await registry.dispatch("skills_list", {}, ctx)
    assert "deploy-checklist" in out
    ctx.skill_store.write_support_file("deploy-checklist", "references/checklist.md", "support notes")
    out = await registry.dispatch(
        "skill_view", {"name": "deploy-checklist", "file_path": "references/checklist.md"}, ctx
    )
    assert out == "support notes"
    prompt = build_system_prompt(ctx.agent, ctx.skill_store.index(None), None, ctx.workspace)
    assert "deploy-checklist: Steps to deploy" in prompt
    assert "skill_manage" in prompt  # self-evolution guide present


# ---------------------------------------------------------------------------
# Built-in skill seeding (hermes import)
# ---------------------------------------------------------------------------

HERMES_TAGS_MD = """---
name: hermes-tagged
description: "Skill using hermes-style nested tags"
metadata:
  hermes:
    tags: [GitHub, Git]
---

# Hermes Tagged
body
"""

BEAUTY_SKILL_MD = """---
name: beauty-salon-marketing
description: "美容院营销"
---

# Beauty Salon Marketing
"""


def _fake_source(tmp_path, names):
    src = tmp_path / "src"
    for n in names:
        d = src / n
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f'---\nname: {n}\ndescription: "fake {n}"\n---\n\n# {n}\n', encoding="utf-8"
        )
    return src


def test_seed_copies_missing(tmp_path):
    src = _fake_source(tmp_path, ["alpha", "beta"])
    dest = tmp_path / "skills"
    n = seed_builtin_skills(dest, source=src)
    assert n == 2
    assert (dest / "alpha/SKILL.md").is_file()
    assert (dest / "beta/SKILL.md").is_file()


def test_seed_is_idempotent(tmp_path):
    src = _fake_source(tmp_path, ["alpha"])
    dest = tmp_path / "skills"
    assert seed_builtin_skills(dest, source=src) == 1
    assert seed_builtin_skills(dest, source=src) == 0


def test_seed_does_not_overwrite(tmp_path):
    src = _fake_source(tmp_path, ["alpha"])
    dest = tmp_path / "skills"
    (dest / "alpha").mkdir(parents=True)
    (dest / "alpha/SKILL.md").write_text("SENTINEL", encoding="utf-8")
    assert seed_builtin_skills(dest, source=src) == 0
    assert (dest / "alpha/SKILL.md").read_text() == "SENTINEL"


def test_seed_copies_support_subdirs(tmp_path):
    src = _fake_source(tmp_path, ["alpha"])
    (src / "alpha/scripts").mkdir()
    (src / "alpha/scripts/run.sh").write_text("echo hi", encoding="utf-8")
    dest = tmp_path / "skills"
    seed_builtin_skills(dest, source=src)
    assert (dest / "alpha/scripts/run.sh").read_text() == "echo hi"


def test_seed_missing_source_returns_zero(tmp_path):
    dest = tmp_path / "skills"
    assert seed_builtin_skills(dest, source=tmp_path / "nope") == 0


def test_real_bundle_seeds_and_parses(tmp_path):
    """The shipped builtin/ bundle has the curated hermes skills, all parseable."""
    dest = tmp_path / "skills"
    n = seed_builtin_skills(dest)
    assert n >= 28
    assert (dest / "systematic-debugging/SKILL.md").is_file()
    assert (dest / "github-auth/SKILL.md").is_file()
    meta, _ = parse_frontmatter((dest / "systematic-debugging/SKILL.md").read_text())
    assert meta["name"] == "systematic-debugging"
    assert meta["description"]
    # store can index the whole curated set without choking
    store = SkillStore(dest)
    assert len(store.index()) >= 28


def test_builtin_dir_exists():
    assert builtin_dir().is_dir()


def test_index_reads_hermes_nested_tags(store):
    store.create("hermes-tagged", HERMES_TAGS_MD)
    idx = store.index()
    assert idx[0]["tags"] == ["GitHub", "Git"]


def _agent(**overrides):
    base = {
        "id": 1,
        "name": "helper",
        "description": "general assistant",
        "system_prompt": "You help users.",
        "provider": "deepseek",
        "model": "DeepSeek-V4-Flash",
        "tools": ["skills_list", "skill_view"],
        "skills": None,
    }
    base.update(overrides)
    return AgentDef(**base)


def test_contextual_beauty_skills_hidden_by_default(store):
    store.create("deploy-checklist", SKILL_MD)
    store.create("beauty-salon-marketing", BEAUTY_SKILL_MD)
    names = {s["name"] for s in filter_index_for_agent(store.index(), _agent())}
    assert "deploy-checklist" in names
    assert "beauty-salon-marketing" not in names


def test_contextual_beauty_skills_shown_for_beauty_agent(store):
    store.create("beauty-salon-marketing", BEAUTY_SKILL_MD)
    names = {s["name"] for s in filter_index_for_agent(store.index(), _agent(description="美容院管理"))}
    assert "beauty-salon-marketing" in names


def test_contextual_beauty_skills_respect_explicit_allowlist(store):
    store.create("beauty-salon-marketing", BEAUTY_SKILL_MD)
    idx = store.index(["beauty-salon-marketing"])
    names = {s["name"] for s in filter_index_for_agent(idx, _agent(skills=["beauty-salon-marketing"]))}
    assert names == {"beauty-salon-marketing"}
