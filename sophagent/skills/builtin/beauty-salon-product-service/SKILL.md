---
name: beauty-salon-product-service
description: "美容院产品与服务项目管理。管理服务项目、产品、价格、时长、耗材关联。触发：添加服务项目/录入服务项目、导入产品/新增产品、改价、查列表、设置耗材。"
---

# 产品与服务项目管理

## SophAgent 适配说明

- 本 skill 已从 SophClaw 迁移到 SophAgent 内置 skill 目录。
- 支持文件按 SophAgent 规范存放：脚本在 `scripts/`，资料和 playbook 在 `references/`。
- 运行脚本前，优先在用户 workspace 中设置 `BEAUTY_DB_PATH`，例如 `export BEAUTY_DB_PATH="$PWD/beauty-salon-suite/beauty.sqlite3"`。
- 如果需要执行 Python 包脚本，先设置 `PYTHONPATH="$PWD/scripts:$PYTHONPATH"`，或将本 skill 的 `scripts/` 内容复制到 workspace 后执行。

## 运行方式

```bash
cd "{baseDir}"
export PYTHONPATH="$PWD/scripts:$PYTHONPATH"
python -m product_service_cli <子命令> ...
```

数据库（单库）：`$BEAUTY_DB_PATH`；未设置时建议使用当前用户 workspace 下的 `beauty-salon-suite/beauty.sqlite3`。

## 子命令

| 子命令 | 说明 |
|--------|------|
| `service-add` | `--name` `--price` `--duration` 可选 `--materials` JSON |
| `service-query` | 可选 `--name` |
| `service-update` | `--name` `--set` JSON（`price` / `duration_min` / `name`） |
| `service-delete` | `--name` 需 `--yes`；在单库内直接检查次卡引用 |
| `product-add` | `--name` `--cost` `--price` 可选 `--unit` `--threshold` |
| `product-query` | 可选 `--name` |
| `product-update` | `--name` `--set` JSON |
| `product-delete` | `--name` 需 `--yes` |
| `service-material` | `--service` + `--materials` JSON 或 `--query` |

## 示例

```bash
python -m product_service_cli service-add --name "面部护理" --price 298 --duration 60 --materials "{\"面膜\":1,\"精华液\":0.5}"
python -m product_service_cli service-material --service "面部护理" --query
python -m product_service_cli product-add --name "面膜" --cost 30 --price 98 --unit "盒" --threshold 20
```

## 与其他 Skill

- 删除服务前：在单库内查询 `member_cards` 检查次卡引用。\n+- 新增产品：可选同步到 `inv_items`（设 `BEAUTY_SKIP_INVENTORY_SYNC=1` 可关闭）。
