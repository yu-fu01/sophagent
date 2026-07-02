#!/usr/bin/env python3
"""跨 skill 冒烟：库存初始化、服务/员工/会员/预约/完成服务、低库存查询、分群。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent  # .../skills


def resolve_test_dir() -> Path:
    # Prefer explicit env (so shell verification uses same DBs)
    if os.environ.get("TEST_DIR"):
        return Path(os.environ["TEST_DIR"]).expanduser()
    # Default to OpenClaw workspace location on Linux/macOS
    home = Path.home()
    return home / ".openclaw" / "workspace" / "sophclaw-test"


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    env = {**os.environ, **kw.pop("env", {})}
    return subprocess.run(args, capture_output=True, text=True, env=env, **kw)


def main() -> int:
    test_dir = resolve_test_dir()
    test_dir.mkdir(parents=True, exist_ok=True)

    beauty_db = os.environ.get("BEAUTY_DB_PATH") or str(test_dir / "beauty.sqlite3")

    # Ensure fresh DBs for repeatable E2E.
    try:
        Path(beauty_db).unlink(missing_ok=True)
    except OSError:
        pass

    os.environ["BEAUTY_DB_PATH"] = beauty_db

    py = sys.executable
    init_py = str(ROOT / "beauty-salon-inventory" / "src" / "scripts" / "init_salon_inventory.py")
    inv_cli = str(ROOT / "beauty-salon-inventory" / "src" / "scripts" / "salon_inventory_cli.py")
    prod_pkg = str(ROOT / "beauty-salon-product-service" / "src" / "src")
    staff_pkg = str(ROOT / "beauty-salon-staff" / "src" / "src")
    mem_pkg = str(ROOT / "beauty-salon-member-appointment" / "src" / "src")
    cwd = str(ROOT)

    assert run([py, init_py]).returncode == 0
    pdata = json.dumps(
        {"产品名": "面膜", "单位": "盒", "成本": 30, "售价": 98, "库存数量": 50, "预警阈值": 10},
        ensure_ascii=False,
    )
    r = run([py, inv_cli, "item-upsert", "--data", pdata], cwd=cwd)
    assert r.returncode == 0, r.stderr + r.stdout

    r = run(
        [py, "-m", "product_service_cli", "service-add", "--name", "面部护理", "--price", "298", "--duration", "60"],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": prod_pkg},
    )
    assert r.returncode == 0, r.stderr + r.stdout
    mat = json.dumps({"面膜": 1}, ensure_ascii=False)
    r = run(
        [py, "-m", "product_service_cli", "service-material", "--service", "面部护理", "--materials", mat],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": prod_pkg},
    )
    assert r.returncode == 0, r.stderr + r.stdout

    r = run(
        [py, "-m", "staff_cli", "staff-add", "--name", "张技师", "--staff-id", "S001"],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": staff_pkg},
    )
    assert r.returncode == 0, r.stderr + r.stdout
    sc = json.dumps({"面部护理": 0.15}, ensure_ascii=False)
    r = run(
        [py, "-m", "staff_cli", "staff-commission", "--staff-id", "S001", "--service-commission", sc],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": staff_pkg},
    )
    assert r.returncode == 0, r.stderr + r.stdout
    r = run(
        [py, "-m", "staff_cli", "staff-schedule", "--staff-id", "S001", "--date", "2024-03-21", "--status", "work"],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": staff_pkg},
    )
    assert r.returncode == 0, r.stderr + r.stdout

    r = run(
        [py, "-m", "member_appt_cli", "member-add", "--name", "王芳", "--member-id", "M001"],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": mem_pkg},
    )
    assert r.returncode == 0, r.stderr + r.stdout
    r = run(
        [py, "-m", "member_appt_cli", "balance-recharge", "--member-id", "M001", "--amount", "5000"],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": mem_pkg},
    )
    assert r.returncode == 0, r.stderr + r.stdout
    r = run(
        [
            py,
            "-m",
            "member_appt_cli",
            "appt-create",
            "--member-id",
            "M001",
            "--project",
            "面部护理",
            "--technician-id",
            "S001",
            "--datetime",
            "2024-03-21 15:00",
            "--appt-id",
            "A001",
        ],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": mem_pkg},
    )
    assert r.returncode == 0, r.stderr + r.stdout
    r = run(
        [py, "-m", "member_appt_cli", "appt-complete", "--appt-id", "A001", "--pay", "stored_value"],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": mem_pkg},
    )
    print("=== appt-complete ===")
    print(r.stdout)
    if r.returncode != 0:
        print(r.stderr)
        return 1

    r = run([py, inv_cli, "stock-query", "--key", "面膜"], cwd=cwd)
    print("=== stock-query 面膜 ===")
    print(r.stdout)
    r = run([py, inv_cli, "stock-query", "--low-stock"], cwd=cwd)
    print("=== low-stock ===")
    print(r.stdout)

    r = run(
        [py, "-m", "staff_cli", "staff-salary-calc", "--staff-id", "S001", "--month", "2024-03"],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": staff_pkg},
    )
    print("=== salary-calc ===")
    print(r.stdout)

    seg = str(ROOT / "beauty-salon-marketing" / "src" / "scripts" / "member_segment.py")
    r = run([py, seg], cwd=cwd)
    print("=== member-segment ===")
    print(r.stdout[:2000])
    return 0 if r.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
