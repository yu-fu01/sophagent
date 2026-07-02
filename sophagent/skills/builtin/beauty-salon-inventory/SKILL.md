---
name: beauty-salon-inventory
description: "美容院耗材库存（单库 beauty.sqlite3）。触发：库存不够了/库存预警/低库存、库存查询、入库、出库、耗材扣减、变动记录。"
---

# 美容院库存管理

## SophAgent 适配说明

- 本 skill 已从 SophClaw 迁移到 SophAgent 内置 skill 目录。
- 支持文件按 SophAgent 规范存放：脚本在 `scripts/`，资料和 playbook 在 `references/`。
- 运行脚本前，优先在用户 workspace 中设置 `BEAUTY_DB_PATH`，例如 `export BEAUTY_DB_PATH="$PWD/beauty-salon-suite/beauty.sqlite3"`。
- 如果需要执行 Python 包脚本，先设置 `PYTHONPATH="$PWD/scripts:$PYTHONPATH"`，或将本 skill 的 `scripts/` 内容复制到 workspace 后执行。

本仓库已升级为 **单库 `beauty.sqlite3`**，库存表为 `inv_items/inv_logs`，通过本 skill 自带脚本直接操作，不再依赖 `beauty-salon-inventory`。

## 约定

- **数据库路径**：`BEAUTY_DB_PATH`（默认：`$BEAUTY_DB_PATH`）
- **库存脚本**：`./scripts/salon_inventory_cli.py`

## 首次初始化

```bash
python3 "./scripts/init_salon_inventory.py"
```

## 常用命令（美容院话术）

将 `./scripts/salon_inventory_cli.py` 替换为 `./scripts/salon_inventory_cli.py`。

```bash
# 查询全部库存
python3 "./scripts/salon_inventory_cli.py" stock-query

# 单个产品（产品名即 key）
python3 "./scripts/salon_inventory_cli.py" stock-query --key 面膜

# 低库存预警
python3 "./scripts/salon_inventory_cli.py" stock-query --low-stock

# 采购入库
python3 "./scripts/salon_inventory_cli.py" stock-in --key 面膜 --quantity 100 --type purchase --remark "供应商A"

# 销售出库
python3 "./scripts/salon_inventory_cli.py" stock-out --key 面膜 --quantity 5 --type sale

# 服务消耗出库（完成护理后扣耗材）
python3 "./scripts/salon_inventory_cli.py" stock-out --key 面膜 --quantity 1 --type service --remark "service appt:A001"

# 报损
python3 "./scripts/salon_inventory_cli.py" stock-out --key 面膜 --quantity 2 --type damage --remark "包装破损"

# 变动记录
python3 "./scripts/salon_inventory_cli.py" stock-log --key 面膜

# 新增/更新耗材（兼容中文字段）
python3 "./scripts/salon_inventory_cli.py" item-upsert --data "{\"产品名\":\"精华液\",\"单位\":\"瓶\",\"成本\":80,\"售价\":198,\"库存数量\":0,\"预警阈值\":10}"

# 导出
python3 "./scripts/salon_inventory_cli.py" export --type items --output ./items.csv
```

## 自然语言 → 命令

| 用户说 | 命令 |
|--------|------|
| 面膜还有多少？ | `stock-query --key 面膜` |
| 哪些库存不够？ | `stock-query --low-stock` |
| 入库面膜100盒 | `stock-in --key 面膜 --quantity 100` |
| 出库面膜5盒销售 | `stock-out --key 面膜 --quantity 5 --type sale` |

## 与其他 Skill

- **beauty-salon-product-service**：产品主数据与耗材规则；库存数量在本 skill。
- **beauty-salon-member-appointment**：`appt-complete` 在单库事务内直接扣减 `inv_items` 并写入 `inv_logs`。
