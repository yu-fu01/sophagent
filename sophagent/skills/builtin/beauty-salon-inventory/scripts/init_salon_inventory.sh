#!/usr/bin/env sh
# 初始化美容院库存项目（委托 Python 脚本以兼容 Windows / 路径）
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/init_salon_inventory.py" "$@"
