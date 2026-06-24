"""Slash commands for sophclaw-agent.

Provides a command registry + dispatch layer inspired by hermes-agent's
``hermes_cli/commands.py``. Commands are registered centrally and dispatched
by ``dispatch_command()``, which is called from the chat API endpoint before
running the LLM.

Usage::

    from .commands import dispatch_command
    result = await dispatch_command(user_input, db, session, user, manager)
    if result["handled"]:
        return JSONResponse(result)
"""

from __future__ import annotations

from .registry import CommandDef, Suggestion, dispatch, get_commands, register, resolve
from .builtin import (compact as cmd_compact, help as cmd_help, clear as cmd_clear,
                      model as cmd_model, title as cmd_title, stop as cmd_stop,
                      undo as cmd_undo, retry as cmd_retry, cron as cmd_cron)


def _init() -> None:
    """Register all builtin commands. Called once at import time."""
    register(CommandDef("help", "查看可用命令", "通用",
                        handler=cmd_help.handle))
    register(CommandDef("clear", "清空当前会话并重新开始", "会话",
                        aliases=("new",), handler=cmd_clear.handle))
    register(CommandDef("model", "切换当前会话使用的模型", "配置",
                        args_hint="<模型名>", handler=cmd_model.handle,
                        suggestion_resolver=cmd_model.suggest_models))
    register(CommandDef("title", "设置当前会话标题", "会话",
                        args_hint="<标题>", handler=cmd_title.handle))
    register(CommandDef("stop", "停止当前正在生成的回复", "会话",
                        handler=cmd_stop.handle))
    register(CommandDef("undo", "撤销最后一条用户消息及其回复", "会话",
                        handler=cmd_undo.handle, aliases=("u",)))
    register(CommandDef("retry", "重新生成最后一条助手回复", "会话",
                        handler=cmd_retry.handle))
    register(CommandDef("compact", "手动压缩对话以节省上下文", "会话",
                        handler=cmd_compact.handle))
    register(CommandDef("cron", "管理定时任务（列表/创建/暂停/恢复/删除/触发/修改）", "定时任务",
                        args_hint="<子命令> [参数]", handler=cmd_cron.handle,
                        subcommands=(
                            Suggestion("list", "列出所有定时任务"),
                            Suggestion("create", "创建定时任务",
                                       args_hint="<agent_id> <schedule> <prompt> [--name <name>] [--repeat <N>]"),
                            Suggestion("pause", "暂停任务", args_hint="<job_id>"),
                            Suggestion("resume", "恢复任务", args_hint="<job_id>"),
                            Suggestion("delete", "删除任务", args_hint="<job_id>"),
                            Suggestion("trigger", "手动触发执行", args_hint="<job_id>"),
                            Suggestion("set", "修改字段（name/prompt/schedule/repeat_times）",
                                       args_hint="<job_id> <field> <value>"),
                        )))


_init()

# Convenience re-exports
dispatch_command = dispatch
get_commands_list = get_commands

__all__ = ["dispatch_command", "get_commands_list", "register", "CommandDef"]