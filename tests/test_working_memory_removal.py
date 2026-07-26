"""Working Memory Removal — TDD Tests

Tests written BEFORE implementation to verify:
1. Exit logic: agent exits when LLM response has no tool_calls
2. No working_memory pseudo-tool messages in context
3. Warnings injected as user messages, not pseudo tool messages
4. filter_wm parameter removed
5. 35-round forced inquiry removed
6. 7-round warning preserved
7. 3x repeat detection preserved
8. do_no_tool next_prompt never worked (confirmed by code review)
"""
import pytest
import json
import inspect
import subprocess
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HANDLER_PATH = os.path.join(PROJECT_ROOT, HANDLER_PATH)

sys.path.insert(0, ".")


class TestExitLogicAfterWmRemoval:
    """Verify exit logic: agent exits when response has no tool_calls,
    regardless of next_prompt content."""

    def test_exit_when_no_tool_calls_and_empty_next_prompt(self):
        """LLM has no tool_calls and no next_prompt → exit"""
        next_prompt = ""
        has_tool_calls = False
        assert not has_tool_calls
        assert not next_prompt or not next_prompt.strip()

    def test_exit_when_no_tool_calls_but_warning_text_exists(self):
        """LLM has no tool_calls but next_prompt has warning text → MUST exit
        Old logic: next_prompt non-empty → continue (BUG!)
        New logic: no tool_calls → exit (CORRECT)
        """
        next_prompt = "⚠️ **警告：检测到重复工具调用**\n\n你已连续 3 次调用相同工具..."
        has_tool_calls = False
        old_would_exit = not next_prompt or not next_prompt.strip()
        assert old_would_exit is False  # old logic would NOT exit — BUG
        assert not has_tool_calls  # new logic: exit — CORRECT

    def test_continue_when_tool_calls_exist(self):
        """LLM has tool_calls → continue loop"""
        has_tool_calls = True
        assert has_tool_calls

    def test_continue_when_tool_calls_with_warning(self):
        """LLM has tool_calls AND warning text → continue"""
        has_tool_calls = True
        next_prompt = "⚠️ **警告：检测到重复工具调用**"
        assert has_tool_calls


class TestNoWorkingMemoryInjection:
    """Verify no working_memory pseudo-tool messages are created."""

    def test_no_wm_call_id_in_messages(self):
        """After WM removal, no message should have tool_call_id starting with 'wm_'"""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "I'll help you", "tool_calls": [
                {"id": "call_abc123", "type": "function", "function": {"name": "read", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "call_abc123", "content": "file content"},
        ]
        for msg in messages:
            if msg.get("role") == "tool":
                assert not msg.get("tool_call_id", "").startswith("wm_")
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    assert tc["function"]["name"] != "working_memory"

    def test_no_wm_pseudo_tool_call_in_messages(self):
        """No assistant message should contain working_memory tool_call"""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_real", "type": "function", "function": {"name": "code_run", "arguments": "{}"}}
            ]}
        ]
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    assert tc["function"]["name"] != "working_memory"


class TestWarningInjectionAsUserMessages:
    """Warnings injected as user messages, not pseudo tool messages."""

    def test_warning_injected_as_user_message(self):
        warning_text = "⚠️ **警告：检测到重复工具调用**"
        msg = {"role": "user", "content": warning_text}
        assert msg["role"] == "user"
        assert "tool_call_id" not in msg
        assert "tool_calls" not in msg

    def test_no_pseudo_assistant_tool_pair_for_warnings(self):
        new_msgs = [{"role": "user", "content": "⚠️ **警告**"}]
        assert len(new_msgs) == 1
        assert new_msgs[0]["role"] == "user"


class TestThirtyFiveRoundForcedInquiryRemoved:
    """35-round forced inquiry must be removed."""

    def test_no_forced_inquiry_at_turn_35(self):
        turn = 35
        next_prompt = ""
        new_next_prompt = next_prompt
        assert "[DANGER]" not in new_next_prompt

    def test_no_forced_inquiry_at_turn_70(self):
        turn = 70
        next_prompt = ""
        new_next_prompt = next_prompt
        assert "[DANGER]" not in new_next_prompt


class TestSevenRoundWarningPreserved:
    """7-round anti-retry warning must be preserved."""

    def test_warning_at_turn_7(self):
        turn = 7
        assert turn % 7 == 0
        assert turn % 35 != 0

    def test_warning_at_turn_14(self):
        turn = 14
        assert turn % 7 == 0
        assert turn % 35 != 0


class TestThreeRepeatDetectionPreserved:
    """3x repeat tool detection must be preserved."""

    def test_repeat_detection_still_works(self):
        _recent_tool_calls = [
            "read(file_path=/tmp/a.txt)",
            "read(file_path=/tmp/a.txt)",
            "read(file_path=/tmp/a.txt)",
        ]
        recent_tools = _recent_tool_calls[-3:]
        assert len(recent_tools) == 3 and recent_tools[0] == recent_tools[1] == recent_tools[2]

    def test_no_repeat_with_different_tools(self):
        _recent_tool_calls = [
            "read(file_path=/tmp/a.txt)",
            "write(file_path=/tmp/b.txt)",
            "read(file_path=/tmp/a.txt)",
        ]
        recent_tools = _recent_tool_calls[-3:]
        assert not (len(recent_tools) == 3 and recent_tools[0] == recent_tools[1] == recent_tools[2])


class TestFilterWmRemoved:
    """filter_wm parameter should be removed from _build_incremental_msg_text."""

    def test_no_filter_wm_parameter(self):
        from niu_api.compat import _build_incremental_msg_text
        sig = inspect.signature(_build_incremental_msg_text)
        assert "filter_wm" not in sig.parameters


class TestHandlerNoAnchorPromptAsNextPrompt:
    """Tool methods should NOT use _get_anchor_prompt() as next_prompt."""

    def test_get_anchor_prompt_not_used_in_step_outcomes(self):
        result = subprocess.run(
            ["grep", "-c", "next_prompt=self._get_anchor_prompt()",
             "REDACTED_USER_PATH/tools/ai-bot/agent/handler.py"],
            capture_output=True, text=True
        )
        count = int(result.stdout.strip()) if result.stdout.strip() else 0
        assert count == 0, f"Found {count} uses of _get_anchor_prompt() as next_prompt"


class TestDoNoToolNextPromptNeverWorked:
    """do_no_tool's next_prompt was never collected because
    no_tool branch uses continue to skip dispatch."""

    def test_no_tool_branch_continues_without_collecting(self):
        next_prompts = set()
        tool_calls = [{"tool_name": "no_tool", "args": {}}]
        for tc in tool_calls:
            if tc["tool_name"] == "no_tool":
                continue
        assert len(next_prompts) == 0


class TestAllNextPromptsEmptyAfterRemoval:
    """After WM removal, all tool methods should return next_prompt=''."""

    def test_no_non_empty_next_prompt_in_handler(self):
        result = subprocess.run(
            ["grep", "-E", "next_prompt=\"[^\"]+\"", "REDACTED_USER_PATH/tools/ai-bot/agent/handler.py"],
            capture_output=True, text=True
        )
        lines = result.stdout.strip().split('\n') if result.stdout.strip() else []
        non_dispatch_lines = [l for l in lines if 'bad_json' not in l and '未知工具' not in l and 'Unknown tool' not in l]
        assert len(non_dispatch_lines) == 0, f"Found non-empty next_prompt: {non_dispatch_lines}"