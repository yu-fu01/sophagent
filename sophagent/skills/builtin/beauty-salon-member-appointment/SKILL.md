---
name: beauty-salon-member-appointment
description: "美容院会员与预约：档案、储值、次卡、预约、技师推荐、完成服务（联动扣耗材与员工业绩）。触发：新客开卡/新客建档、查剩余次数、预约/改期/取消、安排技师（如“明天下午3点做面部护理”）。"
---

# 会员与预约管理

## SophAgent 适配说明

- 本 skill 已从 SophClaw 迁移到 SophAgent 内置 skill 目录。
- 支持文件按 SophAgent 规范存放：脚本在 `scripts/`，资料和 playbook 在 `references/`。
- 运行脚本前，优先在用户 workspace 中设置 `BEAUTY_DB_PATH`，例如 `export BEAUTY_DB_PATH="$PWD/beauty-salon-suite/beauty.sqlite3"`。
- 如果需要执行 Python 包脚本，先设置 `PYTHONPATH="$PWD/scripts:$PYTHONPATH"`，或将本 skill 的 `scripts/` 内容复制到 workspace 后执行。

```bash
cd "{baseDir}"
export PYTHONPATH="$PWD/scripts:$PYTHONPATH"
python -m member_appt_cli <子命令> ...
```

默认库：`$BEAUTY_DB_PATH`；未设置时建议使用当前用户 workspace 下的 `beauty-salon-suite/beauty.sqlite3`。

## 完成服务编排 `appt-complete`

本仓库已升级为 **单库 `beauty.sqlite3`**：`appt-complete` 在同一个 SQLite 事务里完成扣款、扣库存、记业绩与更新预约状态；任一环节失败会整体回滚，不再出现“扣款成功但库存/业绩失败”的半成功状态。

## 内部子命令（供其他 skill）

- `card-count-by-service --service <名> --json`
- `appt-count-by-staff --staff-id <id> --status scheduled --json`

## 示例

```bash
python -m member_appt_cli member-add --name "王芳" --phone "13800138000"
python -m member_appt_cli balance-recharge --member-id M001 --amount 1000 --bonus 150
python -m member_appt_cli appt-create --member-id M001 --project "面部护理" --technician-id S001 --datetime "2024-03-21 15:00"
python -m member_appt_cli appt-complete --appt-id A001 --pay stored_value
```

## 环境变量

- `BEAUTY_DB_PATH`：单库路径（推荐）。默认：`$BEAUTY_DB_PATH`
