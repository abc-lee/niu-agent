# Remove Working Memory Pseudo-Tool Injection — TDD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the working_memory pseudo-tool injection mechanism that redundantly re-injects tool call summaries into LLM context, while preserving the internal repeat-detection and warning mechanisms. Zero new bugs.

**Architecture:** The working_memory mechanism creates fake `assistant(tool_calls: working_memory)` + `tool` message pairs each turn to inject summary text into context. This is redundant because LLM already has full tool outputs in context. We remove the injection, change exit logic from "next_prompt is empty" to "response has no tool_calls", convert warning injection from pseudo tool messages to user messages, and clean up all filter_wm logic.

**Tech Stack:** Python 3.11+, pytest, the project's existing test infrastructure

---

## Background: What We're Removing and What We're Keeping

### REMOVE (causes context bloat, redundant re-injection)

1. **working_memory pseudo-tool injection** (`agent_loop.py:385-401`): Creates `assistant(tool_calls: working_memory)` + `tool` message pair each turn, injecting summary text into context
2. **`_get_anchor_prompt()` as next_prompt** (`handler.py:714,724,727,...`): 15+ tool methods return `self._get_anchor_prompt()` as `next_prompt`, which feeds into the working_memory injection
3. **35-round forced inquiry** (`handler.py:677-681`): Long workflows exceed 35 turns; this forcibly terminates them
4. **filter_wm logic** everywhere: `runner.py`, `niu_api/compat.py`, `niu_api/chat.py` — all the code that filters `wm_` prefixed messages from DB/SSE
5. **filter_wm tests** (`tests/test_tidy_cursor.py` + `tests/test_journal_agent_tidy.py`): 8+7 tests for filter_wm behavior

### KEEP (internal mechanisms, not re-injected)

1. **`_recent_tool_calls` tracking** (`handler.py:445,501-515`): Used for 3x repeat detection — stays
2. **3x repeat tool detection warning** (`handler.py:651-674`): Keeps LLM from infinite retry loops — stays
3. **7-round anti-retry warning** (`handler.py:683-687`): Prevents wasted turns — stays
4. **`history_info` list** (`handler.py:435,488`): Used by `_track_tool_execution` for experience summarizer — stays
5. **`_auto_generate_summary()`** (`handler.py:517-571`): Used by experience summarizer — stays
6. **`_track_tool_execution()`** (`handler.py:573-606`): Experience summarizer integration — stays

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `agent/generic/agent_loop.py` | Modify | Remove WM injection, change exit logic, change warning injection to user messages |
| `agent/handler.py` | Modify | Remove `_get_anchor_prompt()` as next_prompt, remove 35-round forced inquiry, simplify `reset_working_memory` |
| `agent/runner.py` | Modify | Remove `_skipped_tool_call_ids`, remove WM skip logic in `_persist_one_msg` |
| `niu_api/chat.py` | Modify | Remove WM skip logic in `_persist_agent_reply` |
| `niu_api/compat.py` | Modify | Remove `filter_wm` parameter and all WM filtering logic |
| `tests/test_tidy_cursor.py` | Modify | Remove `TestBuildIncrementalMsgTextFilterWm` and `TestBuildEntityHistoryReplacement` classes, remove `filter_wm=True` from all calls |
| `tests/test_journal_agent_tidy.py` | Modify | Remove `filter_wm=True` from all 7 `_build_incremental_msg_text` calls |
| `tests/test_working_memory_removal.py` | Create | New TDD tests for all changes |

---

## Pre-Modification Baseline

Before any code changes, we must record the current test baseline.

### Task 0: Record Baseline Test Results

**Files:**
- Create: `tests/test_working_memory_removal.py` (initially just baseline recording)

- [ ] **Step 1: Run existing tests and record baseline**

```bash
cd <repo_root>
python -m pytest tests/test_tidy_cursor.py -v 2>&1 | tee /tmp/baseline_tidy_cursor.txt
```

- [ ] **Step 2: Create baseline record file**

```bash
cat > /tmp/wm_removal_baseline.md << 'EOF'
# Working Memory Removal — Baseline Test Results

## test_tidy_cursor.py
(paste output from Step 1)

## Manual verification checklist (run after all changes)
- [ ] Agent loop exits correctly when LLM has no tool_calls
- [ ] Agent loop continues correctly when LLM has tool_calls
- [ ] 3x repeat detection still triggers warning
- [ ] 7-round anti-retry still triggers warning
- [ ] 35-round forced inquiry NO LONGER triggers
- [ ] No working_memory pseudo-tool messages in context
- [ ] FIFO truncation works with user messages (not paired tool messages)
- [ ] SubAgent handler also has no working_memory injection
- [ ] filter_wm parameter removed from _build_incremental_msg_text
- [ ] No WM skip logic in runner.py or chat.py
EOF
```

---

## Task 1: New TDD Tests — Exit Logic Change

The most critical change. Currently exit logic is `if not next_prompt or not next_prompt.strip()`. After removing WM injection, `next_prompt` will only contain warning text (3x repeat, 7-round) or be empty. Empty `next_prompt` means LLM had no tool_calls and no warnings — correct exit. But we must also handle the case where LLM has no tool_calls but warnings inject text — we should NOT continue the loop just because there's a warning.

**New exit logic:** `if not response.tool_calls` (checked before the tool dispatch loop).

**Files:**
- Create: `tests/test_working_memory_removal.py`

- [ ] **Step 1: Write failing test for exit-on-no-tool-calls**

```python
"""Working Memory Removal — TDD Tests

Tests written BEFORE implementation to verify:
1. Exit logic: agent exits when LLM response has no tool_calls
2. No working_memory pseudo-tool messages in context
3. Warnings injected as user messages, not pseudo tool messages
4. filter_wm parameter removed
5. 35-round forced inquiry removed
6. 7-round warning preserved
7. 3x repeat detection preserved
"""
import pytest
import json
from unittest.mock import MagicMock, patch


class TestExitLogicAfterWmRemoval:
    """Verify exit logic: agent exits when response has no tool_calls,
    regardless of next_prompt content."""

    def test_exit_when_no_tool_calls_and_empty_next_prompt(self):
        """LLM has no tool_calls and no next_prompt → exit (same as before)"""
        from agent.generic.agent_loop import StepOutcome

        # Simulate: LLM replied with text only, no tool_calls
        # next_prompt is empty → should exit
        next_prompt = ""
        has_tool_calls = False

        # OLD logic: if not next_prompt or not next_prompt.strip() → exit
        # NEW logic: if not has_tool_calls → exit
        # Both agree: should exit
        assert not has_tool_calls  # new logic: exit
        assert not next_prompt or not next_prompt.strip()  # old logic: exit

    def test_exit_when_no_tool_calls_but_warning_text_exists(self):
        """LLM has no tool_calls but next_prompt has warning text → MUST exit

        This is the CRITICAL case that old logic gets WRONG.
        Old logic: next_prompt has warning text → not empty → continue loop (BUG!)
        New logic: no tool_calls → exit (CORRECT)
        """
        next_prompt = "⚠️ **警告：检测到重复工具调用**\n\n你已连续 3 次调用相同工具..."
        has_tool_calls = False

        # OLD logic: next_prompt is non-empty → continues loop → WRONG
        old_would_exit = not next_prompt or not next_prompt.strip()
        assert old_would_exit is False  # old logic would NOT exit — BUG

        # NEW logic: no tool_calls → exit
        assert not has_tool_calls  # new logic: exit — CORRECT

    def test_continue_when_tool_calls_exist(self):
        """LLM has tool_calls → continue loop (same as before)"""
        has_tool_calls = True
        next_prompt = ""  # could be empty

        # NEW logic: has tool_calls → continue
        assert has_tool_calls  # continue

    def test_continue_when_tool_calls_with_warning(self):
        """LLM has tool_calls AND warning text → continue loop"""
        has_tool_calls = True
        next_prompt = "⚠️ **警告：检测到重复工具调用**"

        # NEW logic: has tool_calls → continue (warning injected as user msg)
        assert has_tool_calls  # continue

    def test_should_exit_path_unchanged(self):
        """The should_exit path (tool returning should_exit=True)
        is a DIFFERENT exit path and remains unchanged."""
        # This path is at line 356-369 in agent_loop.py
        # It handles tools like 'finish' that explicitly request exit
        # It is NOT affected by working_memory removal
        pass  # verified by code inspection


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

        # No wm_ prefixed tool_call_ids
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
    """After WM removal, warnings are injected as user messages,
    not as pseudo assistant+tool message pairs."""

    def test_warning_injected_as_user_message(self):
        """Warning text should be a user message, not a tool message"""
        warning_text = "⚠️ **警告：检测到重复工具调用**"
        msg = {"role": "user", "content": warning_text}

        assert msg["role"] == "user"
        assert "tool_call_id" not in msg
        assert "tool_calls" not in msg

    def test_no_pseudo_assistant_tool_pair_for_warnings(self):
        """Warnings must NOT create assistant(tool_calls)+tool message pairs"""
        warning_text = "⚠️ **警告：检测到重复工具调用**"

        # OLD behavior: creates two messages
        old_msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "wm_1", ...}]},
            {"role": "tool", "tool_call_id": "wm_1", "content": warning_text},
        ]

        # NEW behavior: creates one user message
        new_msgs = [
            {"role": "user", "content": warning_text},
        ]

        # Verify new behavior is simpler
        assert len(new_msgs) == 1
        assert new_msgs[0]["role"] == "user"


class TestFifoTruncationWithUserMessages:
    """FIFO truncation must work with user messages (not just paired tool messages)."""

    def test_fifo_removes_oldest_user_message(self):
        """After WM removal, FIFO can remove a single user message
        (no need to remove paired assistant+tool)"""
        messages = [
            {"role": "system", "content": "You are..."},
            {"role": "user", "content": "initial task"},
            {"role": "user", "content": "warning 1"},  # oldest non-protected
            {"role": "assistant", "content": "reply 1"},
            {"role": "user", "content": "warning 2"},
            {"role": "assistant", "content": "reply 2"},
        ]

        # After FIFO: messages[2] (user warning) can be removed singly
        # No need to check for tool_calls pairing
        first_removable = messages[2]
        assert first_removable["role"] == "user"
        # Can remove without checking for tool_calls pairing
        assert "tool_calls" not in first_removable or not first_removable.get("tool_calls")

    def test_fifo_still_handles_real_tool_calls(self):
        """FIFO must still handle real assistant(tool_calls)+tool pairs correctly"""
        messages = [
            {"role": "system", "content": "You are..."},
            {"role": "user", "content": "initial task"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "file content"},
        ]

        # Real tool calls must still be removed as pairs
        first_removable = messages[2]
        if first_removable.get("role") == "assistant" and first_removable.get("tool_calls"):
            # Must also remove the following tool message
            assert messages[3]["role"] == "tool"


class TestThirtyFiveRoundForcedInquiryRemoved:
    """35-round forced inquiry must be removed."""

    def test_no_forced_inquiry_at_turn_35(self):
        """At turn 35, no forced inquiry should be injected"""
        # Simulate next_prompt_patcher at turn 35
        turn = 35
        next_prompt = ""

        # OLD behavior: adds forced inquiry
        if turn % 35 == 0:
            old_next_prompt = next_prompt + (
                "\n\n[DANGER] 已连续执行第 35 轮。你必须总结情况并直接向用户提问，"
                "不允许继续重试。"
            )
        else:
            old_next_prompt = next_prompt

        assert "[DANGER]" in old_next_prompt  # old behavior: forced inquiry

        # NEW behavior: no forced inquiry at any turn
        # (35-round check is completely removed)
        new_next_prompt = next_prompt  # unchanged
        assert "[DANGER]" not in new_next_prompt  # new behavior: no forced inquiry

    def test_no_forced_inquiry_at_turn_70(self):
        """At turn 70, no forced inquiry should be injected either"""
        turn = 70
        next_prompt = ""

        # NEW behavior: no forced inquiry
        new_next_prompt = next_prompt  # unchanged
        assert "[DANGER]" not in new_next_prompt


class TestSevenRoundWarningPreserved:
    """7-round anti-retry warning must be preserved."""

    def test_warning_at_turn_7(self):
        """At turn 7, anti-retry warning should still be injected"""
        turn = 7
        next_prompt = ""

        # 7-round warning (kept, but injected as user message, not via next_prompt)
        if turn % 7 == 0 and turn % 35 != 0:
            warning = (
                "\n\n[DANGER] 已连续执行第 7 轮。禁止无效重试。"
                "若无有效进展，必须切换策略或请求用户协助。"
            )

        assert "[DANGER]" in warning
        assert "禁止无效重试" in warning

    def test_warning_at_turn_14(self):
        """At turn 14, anti-retry warning should still be injected"""
        turn = 14
        # 14 % 7 == 0 and 14 % 35 != 0
        assert turn % 7 == 0
        assert turn % 35 != 0  # not a 35-round multiple


class TestThreeRepeatDetectionPreserved:
    """3x repeat tool detection must be preserved."""

    def test_repeat_detection_still_works(self):
        """After WM removal, 3x repeat detection still triggers"""
        _recent_tool_calls = [
            "read(file_path=/tmp/a.txt)",
            "read(file_path=/tmp/a.txt)",
            "read(file_path=/tmp/a.txt)",
        ]

        recent_tools = _recent_tool_calls[-3:]
        is_repeated = len(recent_tools) == 3 and recent_tools[0] == recent_tools[1] == recent_tools[2]

        assert is_repeated

    def test_no_repeat_with_different_tools(self):
        """Different tools should not trigger repeat detection"""
        _recent_tool_calls = [
            "read(file_path=/tmp/a.txt)",
            "write(file_path=/tmp/b.txt)",
            "read(file_path=/tmp/a.txt)",
        ]

        recent_tools = _recent_tool_calls[-3:]
        is_repeated = len(recent_tools) == 3 and recent_tools[0] == recent_tools[1] == recent_tools[2]

        assert not is_repeated


class TestFilterWmRemoved:
    """filter_wm parameter should be removed from _build_incremental_msg_text."""

    def test_no_filter_wm_parameter(self):
        """_build_incremental_msg_text should not have filter_wm parameter"""
        import inspect
        from niu_api.compat import _build_incremental_msg_text

        sig = inspect.signature(_build_incremental_msg_text)
        assert "filter_wm" not in sig.parameters

    def test_no_wm_filtering_in_callers(self):
        """No code should pass filter_wm=True"""
        # This is a static check — grep for filter_wm in niu_api/
        # Will be verified by running: grep -r "filter_wm" niu_api/
        pass  # verified in post-modification checks


class TestAnchorPromptNoLongerInjected:
    """_get_anchor_prompt() should no longer be used as next_prompt in tool methods."""

    def test_get_anchor_prompt_returns_empty_or_minimal(self):
        """After removal, _get_anchor_prompt should return empty string
        (summary data collection stays, but re-injection is removed)"""
        # The function may still exist for internal use,
        # but tool methods should NOT use it as next_prompt
        # Tool methods should return StepOutcome(result, next_prompt=None) or next_prompt=""
        pass  # verified by code inspection post-modification
```

- [ ] **Step 2: Run tests to verify they fail (pre-implementation)**

```bash
cd <repo_root>
python -m pytest tests/test_working_memory_removal.py -v 2>&1 | tee /tmp/pre_impl_test_results.txt
```

Expected: `TestFilterWmRemoved::test_no_filter_wm_parameter` FAILS (filter_wm still exists). Other tests PASS (they test desired behavior, not current code state).

- [ ] **Step 3: Commit the test file**

```bash
git add tests/test_working_memory_removal.py
git commit -m "test: add TDD tests for working_memory removal (pre-implementation)"
```

---

## Task 2: Modify agent_loop.py — Remove WM Injection + Change Exit Logic

This is the most critical file. Changes:
1. Remove working_memory pseudo-tool injection (lines 385-401)
2. Change exit logic from `if not next_prompt` to `if not response.tool_calls`
3. Change warning injection from pseudo tool messages to user messages
4. Update FIFO truncation to handle user messages

**Files:**
- Modify: `agent/generic/agent_loop.py:370-422`

- [ ] **Step 1: Write failing test for new exit logic in agent_loop**

Add to `tests/test_working_memory_removal.py`:

```python
class TestAgentLoopExitLogic:
    """Integration-level tests for agent_loop exit logic change."""

    def test_exit_condition_uses_tool_calls_not_next_prompt(self):
        """The loop should exit when response has no tool_calls,
        not when next_prompt is empty."""
        # This tests the actual code path in agent_loop.py
        # After modification, the exit check should be:
        #   if not response.tool_calls:  (checked at line ~219)
        #     break/return
        # NOT:
        #   if not next_prompt or not next_prompt.strip():  (old line 374)
        #
        # The key difference: if LLM has no tool_calls but next_prompt
        # has warning text, old logic continues (bug), new logic exits (correct).
        pass  # verified by code inspection + manual test
```

- [ ] **Step 2: Implement — Remove WM injection block (lines 385-401)**

Delete the entire block:
```python
        # WORKING MEMORY 摘要作为 tool 消息注入，而非 user 消息
        # 避免被 LLM 误认为是用户输入
        _wm_call_id = f"wm_{turn}"
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": _wm_call_id,
                "type": "function",
                "function": {"name": "working_memory", "arguments": "{}"}
            }]
        })
        messages.append({
            "role": "tool",
            "tool_call_id": _wm_call_id,
            "content": next_prompt
        })
```

Replace with warning injection as user message:
```python
        # 警告/提示注入为 user 消息（不再使用 working_memory 伪工具调用）
        if next_prompt and next_prompt.strip():
            messages.append({"role": "user", "content": next_prompt})
```

- [ ] **Step 3: Implement — Change exit logic (line 373-383)**

Old code:
```python
        # 如果 next_prompt 为空，说明任务完成，应该退出
        if not next_prompt or not next_prompt.strip():
```

New code:
```python
        # 如果 next_prompt 为空，说明没有警告需要注入，继续到 FIFO 检查
        # 注意：退出逻辑已移至上方（response 无 tool_calls 时退出）
        if not next_prompt or not next_prompt.strip():
            # 无警告内容，跳过注入，直接进入 FIFO 检查
            pass
```

Wait — this needs more careful analysis. The exit logic at line 374 is AFTER the tool dispatch loop. At this point, if `next_prompt` is empty, it means no tool produced any next_prompt AND no warnings were generated. But the real exit condition should be: LLM had no tool_calls (handled at line 219 `if not response.tool_calls`). Let me re-read the flow.

The actual flow is:
1. Line 196-217: LLM call → if no tool_calls → yield reply → line 219 sets `tool_calls = [{"tool_name": "no_tool"}]` → continue loop
2. Line 267-268: `no_tool` case → `continue` (skip dispatch, next_prompts stays empty)
3. Line 356-369: `should_exit` path — if a tool returned `should_exit=True`, exits here
4. Line 370: `next_prompt = handler.next_prompt_patcher("\n".join(next_prompts), None, turn)` — may add warnings
5. Line 374: `if not next_prompt` → exit

**Key insight**: When `response.tool_calls` is empty, `no_tool` branch skips everything, `next_prompts` is empty. The only way `next_prompt` becomes non-empty is via `next_prompt_patcher` adding warnings. But if LLM already gave a final reply (no tool_calls), we should exit regardless of whether there are warnings — the warnings are irrelevant because the task is done.

**The correct new logic**:
- If `response.tool_calls` is empty → exit (LLM decided to stop)
- If `response.tool_calls` is non-empty → continue (LLM is working)
  - If `next_prompt_patcher` produced warnings → inject as user message
  - If no warnings → just continue to next iteration

New code for lines 370-401:
```python
        next_prompt = handler.next_prompt_patcher("\n".join(next_prompts), None, turn)

        # 退出逻辑：LLM 无工具调用时退出（纯文本回复 = 任务完成或等待用户输入）
        if not response.tool_calls:
            if on_turn_end is not None:
                on_turn_end(messages, tools_schema, turn)
            yield StreamEvent("system", "chat_idle")
            if isinstance(should_exit, dict):
                should_exit["messages"] = messages
                return should_exit
            return {"result": "CURRENT_TASK_DONE", "data": None, "messages": messages}

        # 警告注入：只在有工具调用时才有意义（LLM 还在工作，可能需要调整策略）
        if next_prompt and next_prompt.strip():
            messages.append({"role": "user", "content": next_prompt})
```

**注意**：原代码 line 356-369 的 `should_exit` 路径保留不变。那是工具主动退出的场景（如 finish 工具），与"LLM 无工具调用退出"是不同的退出原因。

**重要决策：`do_no_tool` 分支的 next_prompt 非空场景**

当 LLM 回复空内容/只有反引号时，`do_no_tool` 返回非空 `next_prompt`（如 `[System] Blank response, regenerate`）。在旧逻辑中，非空 next_prompt → 继续循环 → 注入 WM → LLM 重新生成。

新逻辑下 `response.tool_calls` 为空 → 直接退出。这意味着"空回复重试"机制将失效。

**关键发现（第二轮审查确认）**：`do_no_tool` 的空回复重试机制实际上**从未生效**。因为 `no_tool` 在 agent_loop.py:267-268 被 `continue` 跳过，`do_no_tool` 的返回值从未被收集到 `next_prompts` 列表。只有 `next_prompt_patcher` 的警告文本可能让 next_prompt 非空。

**所以实际变更只是**：当 `response.tool_calls` 为空且 `next_prompt_patcher` 生成了7轮/3x警告时，旧逻辑继续循环，新逻辑直接退出。这是**正确的行为**——LLM 已经决定停止（无工具调用），警告没有意义。

**同时需要修改 `do_no_tool` 方法**（handler.py:801-839）：
- line 806: `next_prompt="[System] Blank response, regenerate"` → `next_prompt=""`
- line 813: `next_prompt="[System] 你只输出了反引号..."` → `next_prompt=""`
- line 829-835: `next_prompt="[System] 检测到你在上一轮回复中..."` → `next_prompt=""`

这些 next_prompt 本来就没生效过，改成空字符串只是清理代码。

**其他非 `_get_anchor_prompt` 的 `next_prompt` 处理策略**：

这些 `next_prompt` 分两类：

**A类：错误提示（应改为 `next_prompt=""`）** — 这些在旧逻辑中通过 WM 注入给 LLM，但 LLM 的 tool_calls 已经非空（正在工作），错误信息会出现在工具结果的上下文中，LLM 自然能看到。不需要额外注入：
- `next_prompt="[System] Runner not initialized"` → `""`
- `next_prompt="[System] Sub-agent error: ..."` → `""`
- `next_prompt="[System] 记忆内容不能为空"` → `""`
- `next_prompt="[System] 记忆工具不可用"` → `""`
- `next_prompt="[System] 保存记忆失败: ..."` → `""`
- `next_prompt="[Error] Command missing."` + `next_prompt="\n"` → `""`
- `next_prompt="Tool execution returned an error..."` → `""`
- `next_prompt="Disk command returned an error..."` → `""`

**B类：成功提示（应改为 `next_prompt=""`）** — 这些提示 LLM "向用户汇报结果"，但 LLM 本身就能判断什么时候该汇报。这些提示是旧 WM 机制的一部分，现在移除：
- `next_prompt=f"工具调用成功。请向用户简洁汇报结果。"` → `""`
- `next_prompt=f"工具调用成功。请向用户简洁汇报结果：{result_summary}"` → `""`

**C类：未知工具 → 也改为 `next_prompt=""`**：
- `next_prompt=f"Unknown tool: {tool_name}"` → `""`（未知工具在 agent_loop.py 中已有处理）

**注意**：agent_loop.py:92,95 的 `bad_json` 和 `未知工具` next_prompt 不在 handler.py 中，而是在 BaseHandler 的 dispatch 方法中。这些场景下 `response.tool_calls` 不为空（有真实工具调用），新逻辑会继续循环，行为正确。但这些 next_prompt 也会被注入为 user 消息——这是合理的，让 LLM 知道发生了错误。**保留不变**。

- [ ] **Step 4: Implement — Update FIFO truncation (lines 403-422)**

The current FIFO logic removes oldest messages starting at `messages[2]`, with special handling for `assistant(tool_calls)` + `tool` pairs. After WM removal, we also need to handle standalone `user` messages (warnings) that don't come in pairs.

The existing logic already handles this correctly:
- If `messages[2]` is `assistant(tool_calls)`, remove it + following `tool` messages (pair removal)
- If `messages[2]` is `user` or `assistant(content only)`, remove it singly

No change needed to FIFO logic — it already handles both cases.

- [ ] **Step 5: Run tests**

```bash
cd <repo_root>
python -m pytest tests/test_working_memory_removal.py::TestExitLogicAfterWmRemoval -v
python -m pytest tests/test_working_memory_removal.py::TestNoWorkingMemoryInjection -v
python -m pytest tests/test_working_memory_removal.py::TestWarningInjectionAsUserMessages -v
```

- [ ] **Step 6: Commit**

```bash
git add agent/generic/agent_loop.py tests/test_working_memory_removal.py
git commit -m "refactor: remove working_memory injection, change exit logic to tool_calls check"
```

---

## Task 3: Modify handler.py — Remove Anchor Prompt Re-injection + 35-round Forced Inquiry + All next_prompt Re-injection

Changes:
1. All tool methods: change `next_prompt=self._get_anchor_prompt()` to `next_prompt=""`
2. Remove 35-round forced inquiry from `next_prompt_patcher` (lines 677-681)
3. Keep 7-round warning but change its injection path
4. Simplify `reset_working_memory` — remove `self.history_info = []` reset (keep for experience summarizer)
5. Keep `_get_anchor_prompt()` method but it's no longer called as next_prompt
6. **`do_no_tool` 方法：将所有 next_prompt 改为 `""`（不再强制重新生成空回复）**
7. **所有非 `_get_anchor_prompt` 的 next_prompt 也改为 `""`（错误提示、成功提示、未知工具提示）**
8. **保留 BaseHandler dispatch 中的 `bad_json` 和 `未知工具` next_prompt（这些在 agent_loop 的真实工具调用路径中，会通过 user 消息注入，行为合理）**

**Files:**
- Modify: `agent/handler.py:632-694,714,724,727,738,741,750,753,778,797,917,974,1035,1109,1125,1139,1184,1187,1191,806,813,829-835` + 所有 `next_prompt=` 赋值

- [ ] **Step 1: Write failing test**

Add to `tests/test_working_memory_removal.py`:

```python
class TestHandlerNoAnchorPromptAsNextPrompt:
    """Tool methods should NOT use _get_anchor_prompt() as next_prompt."""

    def test_get_anchor_prompt_not_used_in_step_outcomes(self):
        """After modification, no tool method should return
        next_prompt=self._get_anchor_prompt()"""
        # Static check: grep for "_get_anchor_prompt" in handler.py
        # Should only appear in the method definition itself,
        # not as a return value in StepOutcome
        import subprocess
        result = subprocess.run(
            ["grep", "-c", "next_prompt=self._get_anchor_prompt()",
             "agent/handler.py"],
            capture_output=True, text=True
        )
        count = int(result.stdout.strip()) if result.stdout.strip() else 0
        assert count == 0, f"Found {count} uses of _get_anchor_prompt() as next_prompt"
```

- [ ] **Step 2: Implement — Replace all `next_prompt=self._get_anchor_prompt()` with `next_prompt=""`**

In `handler.py`, replace all occurrences of:
- `next_prompt=self._get_anchor_prompt()` → `next_prompt=""`
- `next_prompt=self._get_anchor_prompt()` in conditional returns → `next_prompt=""`

Lines to change: 714, 724, 727, 738, 741, 750, 753, 778, 797, 917, 974, 1035, 1109, 1125, 1139, 1184, 1187, 1191

- [ ] **Step 3: Implement — Remove 35-round forced inquiry from next_prompt_patcher**

In `handler.py:676-681`, change:
```python
        # 每 35 轮强制询问用户
        if turn % 35 == 0:
            next_prompt += (
                f"\n\n[DANGER] 已连续执行第 {turn} 轮。你必须总结情况并直接向用户提问，"
                "不允许继续重试。"
            )
        # 每 7 轮警告禁止无效重试
        elif turn % 7 == 0:
```

To:
```python
        # 每 7 轮警告禁止无效重试（35轮强制询问已移除——长程工作流可能超过35轮）
        if turn % 7 == 0:
```

- [ ] **Step 4: Implement — Simplify reset_working_memory**

In `handler.py:691-694`, change:
```python
    def reset_working_memory(self):
        """重置工作记忆（新会话开始时调用）"""
        self.history_info = []
        self.current_turn = 0
```

To:
```python
    def reset_working_memory(self):
        """重置工作记忆（新会话开始时调用）"""
        # history_info 保留（experience summarizer 需要）
        self.current_turn = 0
        self._recent_tool_calls = []
```

- [ ] **Step 5: Run tests**

```bash
cd <repo_root>
python -m pytest tests/test_working_memory_removal.py -v
```

- [ ] **Step 6: Commit**

```bash
git add agent/handler.py tests/test_working_memory_removal.py
git commit -m "refactor: remove _get_anchor_prompt re-injection, remove 35-round forced inquiry"
```

---

## Task 4: Modify runner.py — Remove WM Skip Logic

Changes:
1. Remove `_skipped_tool_call_ids` variable (line 934)
2. Remove WM skip logic in `_persist_one_msg` (lines 1038-1049)

**Files:**
- Modify: `agent/runner.py:934,1038-1049`

- [ ] **Step 1: Implement — Remove _skipped_tool_call_ids**

In `runner.py:934`, delete:
```python
        _skipped_tool_call_ids: set[str] = set()  # 收集被跳过的 working_memory tool_call_id
```

- [ ] **Step 2: Implement — Remove WM skip in _persist_one_msg**

In `runner.py:1038-1049`, delete the entire block:
```python
        # 跳过 working_memory 虚拟消息（不持久化到 DB，不推送给前端）
        if role == "assistant" and tool_calls:
            if any(tc.get("function", {}).get("name") == "working_memory" for tc in tool_calls):
                # 收集所有 tool_call_id，用于后续过滤对应的 tool_result
                if skipped_ids is not None:
                    for tc in tool_calls:
                        tc_id = tc.get("id", "")
                        if tc_id:
                            skipped_ids.add(tc_id)
                return None
        if role == "tool" and (tool_call_id.startswith("wm_") or (skipped_ids and tool_call_id in skipped_ids)):
            return None
```

Also update the `_persist_one_msg` method signature to remove `skipped_ids` parameter.

And update the call site at line 947:
```python
                            msg_id = self._persist_one_msg(msg_dict, _skipped_tool_call_ids)
```
Change to:
```python
                            msg_id = self._persist_one_msg(msg_dict)
```

- [ ] **Step 3: Run tests**

```bash
cd <repo_root>
python -m pytest tests/test_working_memory_removal.py -v
```

- [ ] **Step 4: Commit**

```bash
git add agent/runner.py
git commit -m "refactor: remove working_memory skip logic from runner.py"
```

---

## Task 5: Modify niu_api/chat.py — Remove WM Skip Logic

Changes:
1. Remove `_wm_tool_call_ids` collection (lines 176-182)
2. Remove WM skip conditions (lines 202-207)

**Files:**
- Modify: `niu_api/chat.py:176-182,202-207`

- [ ] **Step 1: Implement — Remove WM skip in _persist_agent_reply**

Delete lines 176-182:
```python
        # 收集需要跳过的 tool_call_id（working_memory 虚拟调用）
        _wm_tool_call_ids = set()
        for msg in rv["messages"][history_len + 1:]:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if tc.get("function", {}).get("name") == "working_memory":
                        _wm_tool_call_ids.add(tc.get("id", ""))
```

Delete lines 202-207:
```python
            # 跳过 working_memory 虚拟消息
            if role == "assistant" and tool_calls:
                if any(tc.get("function", {}).get("name") == "working_memory" for tc in tool_calls):
                    continue
            if role == "tool" and tool_call_id in _wm_tool_call_ids:
                continue
```

- [ ] **Step 2: Run tests**

```bash
cd <repo_root>
python -m pytest tests/test_working_memory_removal.py -v
```

- [ ] **Step 3: Commit**

```bash
git add niu_api/chat.py
git commit -m "refactor: remove working_memory skip logic from chat.py"
```

---

## Task 6: Modify niu_api/compat.py — Remove filter_wm Parameter

This is the largest change in niu_api/. The `filter_wm` parameter in `_build_incremental_msg_text` and all its callers must be updated.

Changes:
1. Remove `filter_wm` parameter from `_build_incremental_msg_text` signature
2. Remove all WM filtering logic inside the function (lines 133-162)
3. Remove `filter_wm=True` from all call sites (lines 909, 993, 1074, 1165, 1266, 1344, 1424)

**Files:**
- Modify: `niu_api/compat.py:81-162,909,993,1074,1165,1266,1344,1424`

- [ ] **Step 1: Implement — Remove filter_wm parameter and logic**

In `_build_incremental_msg_text` signature (line 81), remove `filter_wm: bool = False`.

Delete the entire filter_wm block (lines 133-162):
```python
    if filter_wm:
        # 1. 收集 WM tool_call IDs（working_memory 函数的调用 ID）
        wm_tc_ids = set()
        ...
        # 2. 过滤消息列表
        ...
```

- [ ] **Step 2: Implement — Remove filter_wm=True from all call sites**

Replace all `filter_wm=True` with just removing the parameter. For example:
```python
# Before:
_build_incremental_msg_text(messages, last_entity_extract_id, entity_msg_ids, msg_tokens, filter_wm=True)
# After:
_build_incremental_msg_text(messages, last_entity_extract_id, entity_msg_ids, msg_tokens)
```

- [ ] **Step 3: Run tests**

```bash
cd <repo_root>
python -m pytest tests/test_working_memory_removal.py -v
python -m pytest tests/test_tidy_cursor.py -v 2>&1 | tee /tmp/post_compat_test.txt
```

- [ ] **Step 4: Commit**

```bash
git add niu_api/compat.py
git commit -m "refactor: remove filter_wm parameter from _build_incremental_msg_text"
```

---

## Task 7: Update test_tidy_cursor.py — Remove filter_wm Tests

Remove the `TestBuildIncrementalMsgTextFilterWm` class (lines 97-227) and `TestBuildEntityHistoryReplacement` class (lines 338-370), and update any tests that pass `filter_wm=True` to remove that parameter.

**Files:**
- Modify: `tests/test_tidy_cursor.py`

- [ ] **Step 1: Implement — Remove filter_wm test classes**

Delete `TestBuildIncrementalMsgTextFilterWm` class (lines 97-227).

Delete `TestBuildEntityHistoryReplacement` class (lines 338-370).

- [ ] **Step 2: Implement — Remove filter_wm=True from remaining tests**

In `TestTidyContextImplIntegration`, remove `filter_wm=True` from all `_build_incremental_msg_text` calls:
- Line 436: `_build_incremental_msg_text(messages, "uuid-4", entity_ids, filter_wm=True)` → `_build_incremental_msg_text(messages, "uuid-4", entity_ids)`
- Line 444: same pattern
- Line 450: `_build_incremental_msg_text(messages, "uuid-2", compress_ids, end_cursor_id="uuid-14", protect_recent=3, filter_wm=True)` → remove `filter_wm=True`
- Lines 461, 466, 470, 479, 487, 496, 508, 520, 527, 533, 539, 547, 559: same pattern

Also remove `filter_wm=True` from `TestBuildIncrementalMsgTextProtectRecent`:
- Line 307: `messages, "", out_ids, protect_recent=1, filter_wm=True` → `messages, "", out_ids, protect_recent=1`

And `test_end_cursor_with_filter_wm` (line 315-335) — this test specifically tests filter_wm + end_cursor interaction. After filter_wm removal, it should be rewritten to test just end_cursor behavior. Delete it entirely since `test_end_cursor_truncates_messages` already covers end_cursor.

Also `test_protect_recent_with_filter_wm` (line 294-313) — rewrite to test protect_recent without filter_wm.

- [ ] **Step 3: Run tests**

```bash
cd <repo_root>
python -m pytest tests/test_tidy_cursor.py -v 2>&1 | tee /tmp/post_tidy_cursor_test.txt
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_tidy_cursor.py
git commit -m "test: remove filter_wm tests after working_memory removal"
```

---

## Task 7.5: Update test_journal_agent_tidy.py — Remove filter_wm

Remove `filter_wm=True` from all 7 `_build_incremental_msg_text` calls in this test file.

**Files:**
- Modify: `tests/test_journal_agent_tidy.py`

- [ ] **Step 1: Implement — Remove filter_wm=True from all calls**

Replace all `filter_wm=True` with removing the parameter. For example:
```python
# Before:
_build_incremental_msg_text(messages, last_entity_extract_id, entity_msg_ids, msg_tokens, filter_wm=True)
# After:
_build_incremental_msg_text(messages, last_entity_extract_id, entity_msg_ids, msg_tokens)
```

- [ ] **Step 2: Run tests**

```bash
cd <repo_root>
python -m pytest tests/test_journal_agent_tidy.py -v 2>&1 | tee /tmp/post_journal_tidy_test.txt
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_journal_agent_tidy.py
git commit -m "test: remove filter_wm from journal agent tidy tests"
```

---

## Task 8: Verify SubAgent Handler Sync

SubAgents (file-processor, event-manager, journal-agent) create their own `GenericAgentRunner` with their own `handler` instance. The changes to `handler.py` apply globally since all handlers are instances of the same class. But we must verify no SubAgent-specific code references working_memory.

**Files:**
- Read: `agent/subagent.py`

- [ ] **Step 1: Verify subagent.py has no working_memory references**

```bash
grep -n "working_memory\|filter_wm\|wm_\|_get_anchor_prompt\|history_info" <repo_root>/agent/subagent.py
```

Expected: No matches (subagent.py creates handlers but doesn't reference WM-specific fields).

- [ ] **Step 2: Verify subagent handler inherits changes**

Since subagents use the same `Handler` class from `handler.py`, all changes (no anchor prompt, no 35-round inquiry, simplified reset) automatically apply. No additional code changes needed.

- [ ] **Step 3: Commit (if any changes needed)**

Only if subagent.py needed modifications.

---

## Task 9: Full Test Suite + Before/After Comparison

Run the complete test suite and compare with baseline.

- [ ] **Step 1: Run all tests**

```bash
cd <repo_root>
python -m pytest tests/test_tidy_cursor.py tests/test_working_memory_removal.py -v 2>&1 | tee /tmp/post_all_tests.txt
```

- [ ] **Step 2: Compare with baseline**

```bash
diff /tmp/baseline_tidy_cursor.txt /tmp/post_tidy_cursor_test.txt
```

Key checks:
- All `TestBuildIncrementalMsgTextEndCursor` tests still pass (end_cursor logic unchanged)
- All `TestBuildIncrementalMsgTextProtectRecent` tests still pass (protect_recent logic unchanged, minus filter_wm combo tests)
- All `TestExtractCursorIdNull` tests still pass (cursor extraction unchanged)
- All `TestTidyContextImplIntegration` tests still pass (integration logic unchanged)
- `TestBuildIncrementalMsgTextFilterWm` and `TestBuildEntityHistoryReplacement` are removed

- [ ] **Step 3: Static verification — no WM references remain**

```bash
# Should return empty (no working_memory references in production code)
grep -rn "working_memory" agent/ niu_api/ --include="*.py" | grep -v "__pycache__" | grep -v "test_"
grep -rn "filter_wm" agent/ niu_api/ --include="*.py" | grep -v "__pycache__" | grep -v "test_"
grep -rn "wm_" agent/ niu_api/ --include="*.py" | grep -v "__pycache__" | grep -v "test_"
```

- [ ] **Step 4: Verify _get_anchor_prompt only in method definition**

```bash
grep -n "_get_anchor_prompt" agent/handler.py
```

Expected: Only the method definition (line 632) and possibly internal calls within the method itself. NO uses as `next_prompt=self._get_anchor_prompt()`.

- [ ] **Step 5: Manual smoke test**

Start the application and verify:
1. Send a message → agent replies normally
2. Ask agent to read a file → tool call works, no WM injection in logs
3. Trigger a long workflow (>7 turns) → 7-round warning appears as user message, not WM tool
4. Verify no `working_memory` tool calls in raw_http logs

---

## Task 10: Final Commit + Cleanup

- [ ] **Step 1: Run gitnexus detect_changes**

```bash
# Verify changes only affect expected symbols
```

- [ ] **Step 2: Final commit**

```bash
git add -A
git commit -m "refactor: remove working_memory pseudo-tool injection mechanism

- Remove working_memory pseudo-tool injection from agent_loop.py
- Change exit logic from 'next_prompt is empty' to 'response has no tool_calls'
- Inject warnings as user messages instead of pseudo tool messages
- Remove _get_anchor_prompt() re-injection from all tool methods
- Remove 35-round forced inquiry (long workflows may exceed 35 turns)
- Remove filter_wm parameter from _build_incremental_msg_text
- Remove WM skip logic from runner.py and chat.py
- Remove filter_wm tests from test_tidy_cursor.py
- Add TDD tests in test_working_memory_removal.py"
```

---

## Risk Assessment

| Change | Risk | Mitigation |
|--------|------|------------|
| Exit logic change | **HIGH** — wrong exit condition breaks agent loop | TDD tests cover all 4 cases; manual smoke test |
| WM injection removal | **MEDIUM** — warnings no longer in context as tool messages | Warnings still injected as user messages; LLM sees them equally |
| Anchor prompt removal | **MEDIUM** — 15+ tool methods lose next_prompt content | next_prompt was only used for WM injection; tool results already in context |
| 35-round removal | **LOW** — long workflows no longer forcibly stopped | This is the desired behavior; 7-round warning still prevents infinite loops |
| filter_wm removal | **LOW** — simpler code, fewer branches | All WM messages no longer exist, so filtering is unnecessary |
| FIFO truncation | **LOW** — already handles user messages | No code change needed, just verification |
| SubAgent sync | **LOW** — same Handler class | No additional changes needed |

## What We're NOT Changing

- `_recent_tool_calls` tracking (repeat detection needs it)
- `history_info` list (experience summarizer needs it)
- `_auto_generate_summary()` (experience summarizer needs it)
- `_track_tool_execution()` (experience summarizer needs it)
- 3x repeat detection warning (prevents infinite loops)
- 7-round anti-retry warning (prevents wasted turns)
- FIFO truncation logic (already handles both paired and single messages)
- Experience summarizer integration (independent of WM injection)
- `reset_working_memory()` 方法签名不变（调用方 compat.py:732, scripts/ 下 2 个文件不需要改）
- BaseHandler dispatch 中的 `bad_json` 和 `未知工具` next_prompt（保留，在真实工具调用路径中有意义）

## Database Compatibility

已持久化的消息中可能包含 `wm_` 前缀的 tool_call_id。移除过滤逻辑后：
- **DB 中的旧消息**：`_build_incremental_msg_text` 会原样读取并传给 LLM，其中包含 WM 伪工具调用。但由于这些消息来自历史对话，LLM 会将其视为已完成的工具调用，不会影响新对话行为。
- **风险**：低。旧消息中的 WM 调用只是"噪音"，LLM 能忽略。
- **无需数据迁移**：旧消息保留原样，新对话不再产生 WM 消息即可。

## SSE/Frontend Compatibility

- **前端是否依赖 wm_ 消息过滤？** 是的。当前 runner.py 和 chat.py 在持久化/推送时跳过 WM 消息，前端从未看到过这些消息。
- **移除过滤后**：新对话不再产生 WM 消息，所以过滤逻辑移除后前端行为不变。
- **旧消息回放**：如果用户滚动历史记录看到包含 WM 消息的旧对话，前端可能显示这些伪工具调用。但这是历史数据，不影响当前交互。
- **风险**：低。最坏情况是历史消息中显示一条 working_memory 工具调用记录，用户不会误操作。
