# 设计：导入 hermes 内置 skill（bundle + 启动播种）

日期：2026-06-17
状态：已批准（待规格复审）

## 背景

本项目（sophagent）已具备**完整且已接入运行**的 skill 能力：

- `sophagent/skills/store.py` — `SkillStore`，扁平布局 `<skills_dir>/<name>/SKILL.md`，增删改查、原子写、索引缓存。
- `sophagent/tools/skills.py` — agent 工具 `skills_list` / `skill_view` / `skill_manage`。
- `sophagent/api/skill_routes.py` — HTTP 列表/查看/管理员写入/删除。
- `sophagent/agent/prompt.py` + `loop.py` — 把 skill 索引注入 system prompt。
- `config.py` — `skills_dir = data_dir/skills`，大小上限。

因此本需求**不新增 skill 能力**，只解决一件事：把 hermes 的内置 skill 内容灌进本项目，作为开箱即用的预置技能库。

## 目标

1. 从 `hermes-agent/skills/` 精选**通用+开发类、可在 Linux 服务端运行**的技能，逐字拷贝进本项目随 git 提交的 bundle 目录。
2. 服务启动时把缺失的内置技能**播种**进运行时 `data_dir/skills`，与用户/agent 自建技能共存。

## 非目标（YAGNI）

- 内置技能的版本/哈希升级检测（v1 用"缺则补、存在则跳过"）。
- SaaS 凭据注入（notion/airtable/google-workspace 等需要的 token 不在本需求范围）。
- 打包进 wheel 的 package-data（本项目从源码经 uv 运行，文件直接在磁盘上）。
- 改写任何 hermes SKILL.md 内容（保持原文与 provenance）。

## 障碍与决策

### 障碍 1：目录布局不一致
- hermes：`skills/<分类>/<技能>/SKILL.md`（两级）。
- sophagent `SkillStore`：`skills/<技能>/SKILL.md`（一级扁平，硬约束）。
- **决策**：压平。`skills/<分类>/<技能>/` → `builtin/<技能>/`，去掉分类层。叶子技能名全局唯一，无碰撞。整目录拷贝（含 `references/`、`scripts/`、`templates/` 等支撑文件——经核对全部落在 `SkillStore.ALLOWED_SUBDIRS` 白名单内）。

### 障碍 2：存储位置是运行时数据，不进 git
- `skills_dir = data_dir/skills`，而 `data/` 在 `.gitignore`。
- **决策**：内置技能放在随项目提交的 `sophagent/skills/builtin/`；启动时播种进 `data_dir/skills`。内置与用户自建分离，可干净升级。

### 障碍 3：tags 字段位置不同
- hermes 把 tags 放 `metadata.hermes.tags`，而 `SkillStore._scan` 只读 `metadata.tags`。
- **决策**：`_scan` 加一行 fallback，读不到 `metadata.tags` 时回退到 `metadata.hermes.tags`。不改任何 SKILL.md。

## 导入清单（28 个）

逐字拷贝，按叶子名压平：

| 分类 | 数量 | 技能 |
|------|------|------|
| software-development | 9 | systematic-debugging, test-driven-development, plan, spike, simplify-code, requesting-code-review, python-debugpy, node-inspect-debugger, hermes-agent-skill-authoring |
| github | 6 | github-auth, github-issues, github-pr-workflow, github-code-review, github-repo-management, codebase-inspection |
| productivity | 7 | nano-pdf, ocr-and-documents, powerpoint, notion, airtable, google-workspace, maps |
| research | 3 | arxiv, llm-wiki, research-paper-writing |
| devops | 2 | kanban-orchestrator, kanban-worker |
| data-science | 1 | jupyter-live-kernel |

**剔除**：apple(macOS 专属)、smart-home、social-media、media、mlops(重型 ML)、creative、email、autonomous-ai-agents、dogfood、yuanbao、note-taking/obsidian，以及 `teams-meeting-pipeline`(依赖 hermes CLI)、`blogwatcher`(冷门 CLI)、`polymarket`(博彩)。

> 后续想加任何剔除项：把对应技能目录丢进 `builtin/` 即可，无需改代码。

## 架构

```
sophagent/skills/
├── store.py            # 现有，改 _scan 一处（tags fallback）
├── seed.py             # 新增：seed_builtin_skills(dest)
└── builtin/            # 新增：随 git 提交的内置技能 bundle（压平后 28 个）
    ├── systematic-debugging/SKILL.md
    ├── github-auth/SKILL.md
    │   └── scripts/...
    └── ...
```

### 播种组件 `seed.py`

```python
def seed_builtin_skills(dest: Path) -> int:
    """把 builtin/ 下缺失的技能整目录拷入 dest（data_dir/skills）。
    已存在的技能目录跳过，不覆盖。返回新播种的技能数。幂等。"""
```

- 源：`Path(__file__).parent / "builtin"`。
- 对每个 `builtin/<name>/`：若 `dest/<name>/` 不存在 → `shutil.copytree` 拷入；存在 → 跳过。
- 不读写 `__pycache__` 等无关项（只遍历含 `SKILL.md` 的目录）。

### 接入点 `main.py`

在 lifespan 中、构造 `SkillStore(cfg.skills_dir)` **之前**调用：

```python
cfg.skills_dir.mkdir(parents=True, exist_ok=True)
seed_builtin_skills(cfg.skills_dir)
app.state.skill_store = SkillStore(cfg.skills_dir)
```

`SkillStore` 索引惰性构建，播种先于构造即可保证首次 `index()` 看到内置技能。

### `store.py` 改动

`_scan` 中取 tags 处：

```python
meta_block = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
tags = meta_block.get("tags")
if not tags and isinstance(meta_block.get("hermes"), dict):
    tags = meta_block["hermes"].get("tags", [])
tags = tags or []
```

## 数据流

1. 启动 → `seed_builtin_skills` 把 28 个内置技能补进 `data_dir/skills`。
2. `SkillStore` 扫描该目录 → 索引含内置 + 用户自建。
3. agent 回合：`build_system_prompt` 注入索引 → agent 用 `skill_view(name)` 加载全文。
4. agent 仍可用 `skill_manage` 自建/改写技能；改写内置技能后，下次启动播种跳过该目录（保留用户改动）。

## 错误处理

- 播种时单个技能拷贝失败：记录日志、跳过该技能，不阻断启动（内置技能是增强项，不应让服务起不来）。
- bundle 目录不存在（异常情况）：`seed_builtin_skills` 返回 0，正常继续。
- 内置 SKILL.md 若 frontmatter 异常：`_scan` 现有逻辑已会跳过（`continue`），不影响其他技能。

## 测试（扩展 `tests/test_skills.py`）

1. 调 `seed_builtin_skills(tmp)` 后，`tmp` 含全部内置技能目录，且每个有 `SKILL.md`。
2. 二次调用幂等：返回 0、不报错、不改动已有目录。
3. 不覆盖：预先在 `tmp/<某内置名>/SKILL.md` 写入哨兵内容，播种后内容不变。
4. 抽查 1-2 个内置 SKILL.md 能被 `parse_frontmatter` 解析出 `name`/`description`。
5. tags fallback：构造一个 `metadata.hermes.tags` 的技能，`index()` 能读到 tags。

## 验收标准

- 全新 `data_dir` 启动后，`GET /api/skills` 返回 ≥28 个内置技能。
- `skill_view("systematic-debugging")` 返回完整 SKILL.md。
- agent system prompt 的 Available Skills 列出内置技能。
- 用户改写某内置技能后重启，改动保留。
- `pytest tests/test_skills.py` 全绿。
