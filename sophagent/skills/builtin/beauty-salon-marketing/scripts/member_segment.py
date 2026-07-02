#!/usr/bin/env python3
"""拉取会员数据并输出美容院营销分群（JSON）。依赖 member_appt_cli。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path


def skills_root() -> Path:
    # .../skills/beauty-salon-marketing/src/scripts/xxx.py → parents^4 = skills
    return Path(__file__).resolve().parent.parent.parent.parent


def fetch_members() -> list[dict]:
    root = skills_root()
    pkg = root / "beauty-salon-member-appointment" / "src" / "src"
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "member_appt_cli",
            "member-query",
            "--with-wallet",
            "--with-profile",
            "--limit",
            "5000",
        ],
        cwd=str(root),
        env={**os.environ.copy(), "PYTHONPATH": str(pkg)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout or "member-query failed")
    data = json.loads(r.stdout.strip())
    return list(data.get("members") or [])


def segment_members(members: list[dict]) -> dict[str, list[dict]]:
    today = datetime.now().date()
    dormant_cutoff = today - timedelta(days=60)
    segments: dict[str, list[dict]] = {
        "高净值客户": [],
        "稳定复购客户": [],
        "沉睡客户": [],
        "新客户": [],
    }
    for m in members:
        bal = float((m.get("wallet") or {}).get("balance") or 0)
        prof = m.get("profile") or {}
        last_s = prof.get("last_visit_at")
        visits = int(prof.get("visit_count") or 0)
        created = m.get("created_at") or ""

        dormant = True
        if last_s:
            try:
                lv = datetime.strptime(last_s[:10], "%Y-%m-%d").date()
                dormant = lv < dormant_cutoff
            except ValueError:
                dormant = visits == 0

        is_new = False
        if created:
            try:
                cr = datetime.strptime(created[:10], "%Y-%m-%d").date()
                is_new = (today - cr).days <= 90
            except ValueError:
                pass

        brief = {"id": m.get("id"), "name": m.get("name"), "balance": bal, "last_visit_at": last_s}

        if bal > 5000:
            segments["高净值客户"].append(brief)
        elif 1000 <= bal <= 5000:
            segments["稳定复购客户"].append(brief)
        elif dormant:
            segments["沉睡客户"].append(brief)
        elif is_new or visits <= 1:
            segments["新客户"].append(brief)
        else:
            segments["稳定复购客户"].append(brief)

    return segments


def main() -> int:
    ap = argparse.ArgumentParser(description="美容院会员分群")
    ap.add_argument("--query", action="store_true", help="输出上次缓存逻辑等价于默认运行")
    args = ap.parse_args()
    _ = args.query
    try:
        members = fetch_members()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    seg = segment_members(members)
    out = {
        "ok": True,
        "total": len(members),
        "segments": {k: {"count": len(v), "members": v[:50]} for k, v in seg.items()},
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
