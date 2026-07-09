---
name: beauty-salon-staff
description: "美容院员工管理：档案、自定义字段、底薪、提成、技师排班/排班、业绩与工资。触发：添加技师、技师排班、技师业绩/排名/工资核算。"
---

# 员工管理

## SophAgent 适配说明

- 本 skill 已从 SophClaw 迁移到 SophAgent 内置 skill 目录。
- 支持文件按 SophAgent 规范存放：脚本在 `scripts/`，资料和 playbook 在 `references/`。
- 运行脚本前，优先在用户 workspace 中设置 `BEAUTY_DB_PATH`，例如 `export BEAUTY_DB_PATH="$PWD/beauty-salon-suite/beauty.sqlite3"`。
- 如果需要执行 Python 包脚本，先设置 `PYTHONPATH="$PWD/scripts:$PYTHONPATH"`，或将本 skill 的 `scripts/` 内容复制到 workspace 后执行。

```bash
cd "{baseDir}"
export PYTHONPATH="$PWD/scripts:$PYTHONPATH"
python -m staff_cli <子命令> ...
```

数据库（单库）：`$BEAUTY_DB_PATH`；未设置时建议使用当前用户 workspace 下的 `beauty-salon-suite/beauty.sqlite3`。

## 主要子命令

- `staff-add` / `staff-query` / `staff-update` / `staff-delete`
- `staff-field-add` / `staff-field-query`
- `staff-salary`（`--base-salary` 或 `--query`）
- `staff-commission`（`--service-commission` JSON、`--product-commission`、或 `--query`）
- `staff-schedule`（`--status off`、`--slots` JSON、`--query`、`--query-all`）
- `staff-performance` / `staff-performance-add` / `staff-salary-calc` / `staff-ranking`

## 示例

```bash
python -m staff_cli staff-add --name "张技师" --phone "13800138000" --join-date "2024-01-01" --fields "{\"擅长项目\":\"面部护理,抗衰\"}"
python -m staff_cli staff-commission --staff-id S001 --service-commission "{\"面部护理\":0.15}"
python -m staff_cli staff-schedule --staff-id S001 --date "2024-03-21" --status off
python -m staff_cli staff-salary-calc --staff-id S001 --month "2024-03"
```

## 协作

- **beauty-salon-member-appointment**：预约冲突、完成服务后 `staff-performance-add`。
- 删除员工前：在单库内直接检查是否存在未完成预约（`member_appointments`）。
