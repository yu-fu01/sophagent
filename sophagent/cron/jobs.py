"""定时任务：调度解析 + next_run 计算 + 人话描述。

存储在 sophagent.db 的 cron_jobs 表（CRUD 见 Database 的 cron_* 方法）；
这里只放与存储无关的纯逻辑，方便单测。

时间模型：全程服务器本地 naive 时间（datetime.now()）。next_run_at / last_run_at
存本地 naive ISO——"0 9 * * *" 即本地 9 点，符合用户直觉。不引入 TZ/DST 迁移
（单机部署足够；多时区/分布式是后续的事，避免重蹈 hermes 的 TZ hack）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Optional

ONESHOT_GRACE = timedelta(seconds=120)   # 一次性任务过点后的宽限（仍触发一次）

_INTERVAL_RE = re.compile(r"^every\s+(\d+)\s*(m(?:in)?|h(?:our)?|d(?:ay)?)s?$", re.IGNORECASE)
_RELATIVE_RE = re.compile(r"^(\d+)\s*(m|h|d)$", re.IGNORECASE)

_UNIT_MINUTES = {"m": 1, "min": 1, "h": 60, "hour": 60, "d": 1440, "day": 1440}


def parse_schedule(spec: str) -> dict[str, Any]:
    """把用户/agent 给的字符串解析成结构化 schedule dict。

    支持三种：

    * interval —— ``every 30m`` / ``every 2h`` / ``every 1d`` →
      ``{"kind": "interval", "minutes": N}``
    * once（相对）—— ``30m`` / ``2h`` / ``1d``（从现在起算）→
      ``{"kind": "once", "run_at": <iso>}``
    * once（绝对）—— ``2026-06-25T14:00`` 或 ``2026-06-25 14:00`` →
      ``{"kind": "once", "run_at": <iso>}``
    * cron —— 5 字段标准 cron ``0 9 * * 1-5`` → ``{"kind": "cron", "expr": ...}``

    解析失败抛 ValueError。
    """
    s = (spec or "").strip()
    if not s:
        raise ValueError("空调度表达式")

    m = _INTERVAL_RE.match(s)
    if m:
        unit = m.group(2).lower()
        unit = unit[:-1] if unit.endswith("s") else unit
        minutes = int(m.group(1)) * _UNIT_MINUTES[unit]
        if minutes <= 0:
            raise ValueError("间隔必须为正")
        return {"kind": "interval", "minutes": minutes}

    m = _RELATIVE_RE.match(s)
    if m:
        unit = m.group(2).lower()
        minutes = int(m.group(1)) * _UNIT_MINUTES[unit]
        run_at = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        return {"kind": "once", "run_at": run_at}

    # 绝对时间（fromisoformat 支持 "2026-06-25T14:00" 与 "2026-06-25 14:00"）
    try:
        dt = datetime.fromisoformat(s.replace("T", " ") if " " not in s and "T" in s else s)
    except ValueError:
        dt = None
    if dt is not None:
        return {"kind": "once", "run_at": dt.isoformat(timespec="seconds")}

    # 否则当 cron 表达式（5 字段），用 croniter 校验
    fields = s.split()
    if len(fields) not in (5, 6):
        raise ValueError(f"无法识别的调度表达式: {spec!r}")
    try:
        from croniter import croniter
        croniter(s, datetime.now())  # 不取值，只为校验
    except Exception as e:  # croniter 抛 KeyError/ValueError
        raise ValueError(f"无效 cron 表达式 {spec!r}: {e}") from e
    return {"kind": "cron", "expr": s}


def compute_next_run(schedule: dict[str, Any], after: datetime) -> Optional[datetime]:
    """给定 schedule 和基准时刻 after，算下一次运行时间。

    * once —— 返回 run_at（仅创建时用；触发后 next_run 置空、state=completed）
    * interval —— after + period
    * cron —— croniter(expr, after).get_next(datetime)

    返回 naive 本地 datetime（与 after 同性质）。
    """
    kind = schedule.get("kind")
    if kind == "once":
        run_at = datetime.fromisoformat(schedule["run_at"])
        return run_at
    if kind == "interval":
        return after + timedelta(minutes=schedule["minutes"])
    if kind == "cron":
        from croniter import croniter
        return croniter(schedule["expr"], after).get_next(datetime)
    raise ValueError(f"未知 schedule kind: {kind!r}")


def describe_schedule(schedule: dict[str, Any]) -> str:
    """人话描述，给前端/agent 回显用。"""
    kind = schedule.get("kind")
    if kind == "once":
        dt = datetime.fromisoformat(schedule["run_at"])
        return f"一次性 · {dt:%Y-%m-%d %H:%M}"
    if kind == "interval":
        mins = schedule["minutes"]
        if mins >= 1440 and mins % 1440 == 0:
            return f"每 {mins // 1440} 天"
        if mins >= 60 and mins % 60 == 0:
            return f"每 {mins // 60} 小时"
        return f"每 {mins} 分钟"
    if kind == "cron":
        return _humanize_cron(schedule["expr"])
    return str(schedule)


def _humanize_cron(expr: str) -> str:
    """对常见 cron 模式给中文描述，其余原样返回。只覆盖高频形态，不求全。"""
    parts = expr.split()
    if len(parts) != 5:
        return f"cron: {expr}"
    minute, hour, dom, month, dow = parts
    daily = dom == "*" and month == "*" and dow == "*"

    def _is_int(s: str) -> bool:
        return s.lstrip("-").isdigit()

    def _at(h: str, m: str) -> str:
        return f"{int(h):02d}:{int(m):02d}"

    # 特例先判（避免 int('*') 崩）
    if daily and minute == "0" and hour == "*":
        return "每个整点"
    if daily and _is_int(minute) and hour == "*":
        return f"每小时第 {int(minute)} 分"
    if daily and minute in ("0,30", "*/30") and hour == "*":
        return "每半小时"
    if daily and minute.startswith("*/") and hour == "*":
        return f"每 {minute[2:]} 分钟"
    # 每天 H:M —— 仅当时分都是具体整数（如 30 * * * * = 每小时第30分，不算此列）
    if daily and _is_int(minute) and _is_int(hour):
        return f"每天 {_at(hour, minute)}"
    # 工作日 H:M
    if dom == "*" and month == "*" and dow in ("1-5", "MON-FRI") and _is_int(minute) and _is_int(hour):
        return f"工作日 {_at(hour, minute)}"
    return f"cron: {expr}"


def row_to_job(row: Any) -> dict[str, Any]:
    """把 DB 行转成可读 dict（schedule 反序列化）。"""
    d = dict(row)
    try:
        d["schedule"] = json.loads(d.get("schedule") or "{}")
    except (ValueError, TypeError):
        d["schedule"] = {}
    return d


def fmt_local(iso: Optional[str]) -> str:
    """本地 naive ISO → 'MM-DD HH:MM'，给确认消息用。"""
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso
