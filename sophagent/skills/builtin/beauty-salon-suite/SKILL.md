---
name: beauty-salon-suite
description: "美容院套件入口：内置共享单库 beauty.sqlite3 的首次自动初始化。"
---

# 美容院套件入口

## SophAgent 适配说明

- 本 skill 已从 SophClaw 迁移到 SophAgent 内置 skill 目录。
- 支持文件按 SophAgent 规范存放：脚本在 `scripts/`，资料和 playbook 在 `references/`。
- 运行脚本前，优先在用户 workspace 中设置 `BEAUTY_DB_PATH`，例如 `export BEAUTY_DB_PATH="$PWD/beauty-salon-suite/beauty.sqlite3"`。
- 如果需要执行 Python 包脚本，先设置 `PYTHONPATH="$PWD/scripts:$PYTHONPATH"`，或将本 skill 的 `scripts/` 内容复制到 workspace 后执行。

安装本 skill 后，`beauty-salon-*` 系列会在首次运行时自动初始化单库 `beauty.sqlite3`（建库建表），用户无需配置环境变量，也不需要迁移历史数据。

## 默认数据库位置

- Windows：`%USERPROFILE%\\.config\\beauty-salon-suite\\beauty.sqlite3`
- Linux/macOS：`$HOME/.config/beauty-salon-suite/beauty.sqlite3`

## 可选环境变量（一般用户无需配置）

- `BEAUTY_DB_PATH`：覆盖默认数据库文件路径。

