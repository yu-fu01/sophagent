#!/usr/bin/env python3
"""美容院营销入口：member-segment / 打印 playbook 指引。"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def skills_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent


def cmd_member_segment(_a: argparse.Namespace) -> int:
    script = Path(__file__).resolve().parent / "member_segment.py"
    r = subprocess.run([sys.executable, str(script)], cwd=str(script.parent))
    return r.returncode


def cmd_marketing_targeted(args: argparse.Namespace) -> int:
    print(
        json.dumps(
            {
                "ok": True,
                "segment": args.segment,
                "goal": args.goal or "转化",
                "next_step": "读取 sophnet-customized-marketing references/playbooks/campaign-planning.md",
                "beauty_context": str(skills_root() / "beauty-salon-marketing" / "src" / "playbooks" / "beauty-salon-segment.md"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_marketing_festival(args: argparse.Namespace) -> int:
    print(
        json.dumps(
            {
                "ok": True,
                "festival": args.festival,
                "year": args.year,
                "playbook": str(skills_root() / "beauty-salon-marketing" / "src" / "playbooks" / "beauty-salon-festival.md"),
                "next_step": "按 playbook 结构生成方案，再用 sophnet-customized-marketing 出文案/海报",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_marketing_plan(args: argparse.Namespace) -> int:
    print(
        json.dumps(
            {
                "ok": True,
                "goal": args.goal,
                "description": args.description,
                "next_step": "读取 sophnet-customized-marketing references/playbooks/campaign-planning.md + references/campaign-mechanics.md",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="beauty-salon-marketing CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("member-segment", help="输出 JSON 分群")
    s.set_defaults(func=cmd_member_segment)

    s = sub.add_parser("marketing-targeted", help="精准营销路由信息")
    s.add_argument("--segment", required=True)
    s.add_argument("--goal", default=None)
    s.set_defaults(func=cmd_marketing_targeted)

    s = sub.add_parser("marketing-festival", help="节日营销路由信息")
    s.add_argument("--festival", required=True)
    s.add_argument("--year", type=int, default=None)
    s.set_defaults(func=cmd_marketing_festival)

    s = sub.add_parser("marketing-plan", help="整体方案路由信息")
    s.add_argument("--goal", required=True)
    s.add_argument("--description", default=None)
    s.set_defaults(func=cmd_marketing_plan)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
