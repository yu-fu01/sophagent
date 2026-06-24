"""Cron job scheduling system for sophclaw-agent.

Provides scheduled task execution, allowing users to:
- Run automated tasks on schedules (cron expressions, intervals, one-shot)
- Create/update/pause/resume/remove cron jobs via API
- Execute tasks through the existing agent LLM pipeline
"""