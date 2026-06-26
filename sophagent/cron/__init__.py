"""定时任务（cron）子系统。

* ``CronScheduler`` —— 60s tick 循环 + 到期 fire（见 scheduler.py）；
* ``jobs`` —— 调度解析 / next_run 计算 / 人话描述（纯逻辑）；
* DB CRUD 在 ``Database`` 的 ``cron_*`` 方法上（db.py）。
"""

from . import jobs
from .jobs import (
    compute_next_run,
    describe_schedule,
    fmt_local,
    parse_schedule,
    row_to_job,
)
from .scheduler import CronScheduler

__all__ = [
    "CronScheduler",
    "jobs",
    "parse_schedule",
    "compute_next_run",
    "describe_schedule",
    "row_to_job",
    "fmt_local",
]
