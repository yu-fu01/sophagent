"""DingTalk Phase 2 gating tests."""

from types import SimpleNamespace

import pytest

from sophagent.im.platforms.dingtalk.gating import (
    compile_mention_patterns,
    parse_id_list,
    parse_mention_patterns,
    should_process_group,
    text_matches_mention_patterns,
)


class TestParsing:
    def test_parse_id_list(self):
        assert parse_id_list("a, b ,c") == ("a", "b", "c")
        assert parse_id_list("") == ()

    def test_parse_mention_patterns_lines(self):
        assert parse_mention_patterns("bot\n助手") == ("bot", "助手")

    def test_parse_mention_patterns_json(self):
        assert parse_mention_patterns('["bot", "助手"]') == ("bot", "助手")


class TestGroupGating:
    def test_dm_always_passes(self):
        assert should_process_group(
            is_group=False, chat_id="c1", text="hi", message=SimpleNamespace(),
            require_mention=True, allowed_chat_ids=set(), free_response_chats=set(),
            mention_patterns=[],
        ) is True

    def test_allowed_chats_blocks_unknown_group(self):
        assert should_process_group(
            is_group=True, chat_id="group-b", text="hi", message=SimpleNamespace(),
            require_mention=False, allowed_chat_ids={"group-a"}, free_response_chats=set(),
            mention_patterns=[],
        ) is False

    def test_free_response_skips_mention(self):
        assert should_process_group(
            is_group=True, chat_id="group-free", text="hi",
            message=SimpleNamespace(is_in_at_list=False),
            require_mention=True, allowed_chat_ids=set(), free_response_chats={"group-free"},
            mention_patterns=[],
        ) is True

    def test_mention_pattern_wake_word(self):
        patterns = compile_mention_patterns(("sophagent",))
        assert should_process_group(
            is_group=True, chat_id="group-1", text="hey sophagent help",
            message=SimpleNamespace(is_in_at_list=False),
            require_mention=True, allowed_chat_ids=set(), free_response_chats=set(),
            mention_patterns=patterns,
        ) is True

    def test_slash_command_bypasses_group_gate(self):
        assert should_process_group(
            is_group=True, chat_id="group-1", text="/pair abc",
            message=SimpleNamespace(is_in_at_list=False),
            require_mention=True, allowed_chat_ids=set(), free_response_chats=set(),
            mention_patterns=[], is_slash_command=True,
        ) is True

    def test_text_matches_patterns(self):
        patterns = compile_mention_patterns(("bot",))
        assert text_matches_mention_patterns("hello bot", patterns) is True
        assert text_matches_mention_patterns("hello", patterns) is False
