---
name: beauty-salon-marketing
description: "美容院营销：会员分群 JSON、节日/精准/方案路由；深度文案与海报走 sophnet-customized-marketing。触发：做活动/营销方案、节日营销、精准唤醒、朋友圈内容、小红书/团购运营、老带新。"
---

# 美容院营销助手

## SophAgent 适配说明

- 本 skill 已从 SophClaw 迁移到 SophAgent 内置 skill 目录。
- 支持文件按 SophAgent 规范存放：脚本在 `scripts/`，资料和 playbook 在 `references/`。
- 运行脚本前，优先在用户 workspace 中设置 `BEAUTY_DB_PATH`，例如 `export BEAUTY_DB_PATH="$PWD/beauty-salon-suite/beauty.sqlite3"`。
- 如果需要执行 Python 包脚本，先设置 `PYTHONPATH="$PWD/scripts:$PYTHONPATH"`，或将本 skill 的 `scripts/` 内容复制到 workspace 后执行。

## 脚本路径

- 分群：`./scripts/member_segment.py`
- 统一 CLI：`./scripts/beauty_marketing_cli.py`

```bash
python "./scripts/beauty_marketing_cli.py" member-segment
python "./scripts/beauty_marketing_cli.py" marketing-targeted --segment "高净值客户" --goal "唤醒"
python "./scripts/beauty_marketing_cli.py" marketing-festival --festival "母亲节" --year 2026
python "./scripts/beauty_marketing_cli.py" marketing-plan --goal "拓客" --description "夏季补水"
```

## Playbook（按需读取）

| 文件 | 用途 |
|------|------|
| [references/playbooks/beauty-salon-segment.md](references/playbooks/beauty-salon-segment.md) | 分群含义与选题 |
| [references/playbooks/beauty-salon-festival.md](references/playbooks/beauty-salon-festival.md) | 节日 14 天结构 |

## 复用 skill（agent 编排）

本仓库已升级为 **单库 `beauty.sqlite3`**。营销侧仍建议通过 CLI 拉取结构化数据（避免直接读 SQLite）：

1. **会员数据**：`beauty-salon-member-appointment` → `member-query --with-wallet --with-profile`
2. **项目与价格**：`beauty-salon-product-service` → `service-query`
3. **库存事实**：`beauty-salon-inventory` → `stock-query`（低库存可用于「清库存」主题）
4. **文案/海报**：**sophnet-customized-marketing**（`references/playbooks/campaign-planning.md`、`references/playbooks/content-generation.md`、`references/playbooks/poster-generation.md`）

不要在本 skill 内直接读其他 skill 的 SQLite；一律通过 CLI 或已由 agent 拉取的结构化结果。
