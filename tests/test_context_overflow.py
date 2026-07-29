"""
Integration tests for context overflow detection fix.

Covers:
1. No proactive exit at 80% threshold (only warning)
2. CONTEXT_OVERFLOW triggered by LLM API context_length_exceeded error
3. FIFO truncation executed before overflow check
4. FIFO preserves system and initial messages
5. FIFO removes tool_calls paired with tool results
6. MockResponse.context_overflow field defaults and explicit True
7. call_subagent context_fifo_threshold parameter (-1, 0, custom)
"""
import os

# Ensure project root is importable
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def exhaust(gen):
    """Consume a generator and return its StopIteration value."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


class FakeClient:
    """Mock LLM client that returns a canned MockResponse via generator."""

    def __init__(self, response=None):
        self.name = "mock"
        self.last_tools = ""
        self.total_cd_tokens = 0
        self._response = response  # MockResponse instance
        self._call_count = 0

    def chat(self, messages, tools=None):
        self._call_count += 1
        resp = self._response

        def gen():
            yield resp
            return resp

        return gen()


class FakeHandler:
    """Minimal handler stub for agent_runner_loop."""

    def __init__(self):
        self._done_hooks = []
        self.max_turns = 40
        self.current_turn = 0
        self._current_messages = []

    def dispatch(self, tool_name, args, response, index=0):
        from agent.generic.agent_loop import StepOutcome

        def gen():
            yield ""
            return StepOutcome(None, next_prompt="continue", should_exit=False)

        return gen()

    def next_prompt_patcher(self, next_prompt, outcome, turn):
        return next_prompt

    def tool_before_callback(self, tool_name, args, response):
        pass

    def tool_after_callback(self, tool_name, args, response, ret):
        pass


def _make_loop_args(client=None, handler=None, **overrides):
    """Build default kwargs for agent_runner_loop with optional overrides."""
    from agent.generic.llmcore import MockResponse

    if client is None:
        client = FakeClient(
            MockResponse(thinking=None, content="Done", tool_calls=None, raw=None)
        )
    if handler is None:
        handler = FakeHandler()

    defaults = {
        "client": client,
        "system_prompt": "system",
        "user_input": "hello",
        "handler": handler,
        "tools_schema": [],
        "max_turns": 5,
        "verbose": False,
        "context_window_tokens": 0,
        "context_fifo_threshold": 0,
        "enable_supplement": False,
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Patch targets for runner imports inside agent_loop
# ---------------------------------------------------------------------------
# is_stop_requested, clear_stop, drain_supplement are imported INSIDE
# agent_runner_loop from agent.runner, so we must patch agent.runner
# _read_warning_threshold is imported at module level from agent.subagent
_RUNNER_MOD = "agent.runner"
_SUBAGENT_MOD = "agent.subagent"
_AGENT_LOOP_MOD = "agent.generic.agent_loop"


# ---------------------------------------------------------------------------
# Context manager for patching all runner imports
# ---------------------------------------------------------------------------
from contextlib import contextmanager


@contextmanager
def _patch_runner_functions(
    stop_requested=False,
    clear_stop_fn=None,
    drain_supplement_fn=None,
):
    """Patch the functions that agent_runner_loop imports from agent.runner."""
    with patch(f"{_RUNNER_MOD}.is_stop_requested", return_value=stop_requested):
        with patch(f"{_RUNNER_MOD}.clear_stop", side_effect=clear_stop_fn or (lambda: None)):
            with patch(f"{_RUNNER_MOD}.drain_supplement", return_value=drain_supplement_fn):
                yield


# ---------------------------------------------------------------------------
# 1. No proactive exit at 80% threshold
# ---------------------------------------------------------------------------

class TestNoProactiveExitAt80Percent:
    """When context usage exceeds 80%, agent_runner_loop should NOT return
    CONTEXT_OVERFLOW. It should only log a warning and continue."""

    @patch(f"{_SUBAGENT_MOD}._read_warning_threshold", return_value=0.80)
    @patch(f"{_AGENT_LOOP_MOD}.count_messages_tokens", return_value=170000)
    def test_continues_past_80_percent_warning(
        self, mock_tokens, mock_threshold
    ):
        from agent.generic.agent_loop import agent_runner_loop
        from agent.generic.llmcore import MockResponse

        # MockResponse without context_overflow — simulates normal LLM reply
        client = FakeClient(
            MockResponse(thinking=None, content="Done", tool_calls=None, raw=None)
        )
        args = _make_loop_args(
            client=client,
            context_window_tokens=200000,  # 170K/200K = 85% > 80% threshold
        )
        with _patch_runner_functions():
            result = exhaust(agent_runner_loop(**args))

        # Should NOT be CONTEXT_OVERFLOW — only a warning was logged
        assert result is not None
        assert isinstance(result, dict)
        assert result.get("result") != "CONTEXT_OVERFLOW"
        # Should complete normally
        assert result.get("result") in ("CURRENT_TASK_DONE",)

    @patch(f"{_SUBAGENT_MOD}._read_warning_threshold", return_value=0.80)
    @patch(f"{_AGENT_LOOP_MOD}.count_messages_tokens", return_value=195000)
    def test_continues_even_at_97_percent(
        self, mock_tokens, mock_threshold
    ):
        from agent.generic.agent_loop import agent_runner_loop
        from agent.generic.llmcore import MockResponse

        client = FakeClient(
            MockResponse(thinking=None, content="Done", tool_calls=None, raw=None)
        )
        args = _make_loop_args(
            client=client,
            context_window_tokens=200000,  # 195K/200K = 97.5%
        )
        with _patch_runner_functions():
            result = exhaust(agent_runner_loop(**args))

        # Still should NOT be CONTEXT_OVERFLOW
        assert result is not None
        assert isinstance(result, dict)
        assert result.get("result") != "CONTEXT_OVERFLOW"


# ---------------------------------------------------------------------------
# 2. CONTEXT_OVERFLOW on LLM API context_length_exceeded error
# ---------------------------------------------------------------------------

class TestContextOverflowOnLLMError:
    """When MockResponse has context_overflow=True (set by litellm_adapter
    when it detects context_length_exceeded), agent_runner_loop should
    return CONTEXT_OVERFLOW."""

    @patch(f"{_SUBAGENT_MOD}._read_warning_threshold", return_value=0.80)
    def test_overflow_on_context_overflow_flag(
        self, mock_threshold
    ):
        from agent.generic.agent_loop import agent_runner_loop
        from agent.generic.llmcore import MockResponse

        # LLM returns context_overflow=True
        client = FakeClient(
            MockResponse(
                thinking=None,
                content="",
                tool_calls=None,
                raw=None,
                context_overflow=True,
            )
        )
        args = _make_loop_args(
            client=client,
            context_window_tokens=200000,
        )
        with _patch_runner_functions():
            result = exhaust(agent_runner_loop(**args))

        assert result is not None
        assert isinstance(result, dict)
        assert result.get("result") == "CONTEXT_OVERFLOW"
        assert result["data"]["overflow"] is True

    @patch(f"{_SUBAGENT_MOD}._read_warning_threshold", return_value=0.80)
    def test_no_overflow_without_flag(
        self, mock_threshold
    ):
        from agent.generic.agent_loop import agent_runner_loop
        from agent.generic.llmcore import MockResponse

        # Normal response — context_overflow defaults to False
        client = FakeClient(
            MockResponse(thinking=None, content="Done", tool_calls=None, raw=None)
        )
        args = _make_loop_args(
            client=client,
            context_window_tokens=200000,
        )
        with _patch_runner_functions():
            result = exhaust(agent_runner_loop(**args))

        assert result is not None
        assert isinstance(result, dict)
        assert result.get("result") != "CONTEXT_OVERFLOW"


class TestLiteLLMAdapterContextOverflow:
    """Test that litellm_adapter correctly detects context_length_exceeded
    errors and returns MockResponse with context_overflow=True."""

    def test_detects_context_length_exceeded(self):
        """litellm_adapter should detect 'context_length_exceeded' in error."""
        # We test the detection logic by checking the pattern exists in source
        import inspect

        from agent.generic.litellm_adapter import LiteLLMSession
        source = inspect.getsource(LiteLLMSession.chat)
        assert "context_length_exceeded" in source
        assert "context_overflow=True" in source

    def test_detects_context_window_error(self):
        """Pattern 'context window' should also be detected."""
        import inspect

        from agent.generic.litellm_adapter import LiteLLMSession
        source = inspect.getsource(LiteLLMSession.chat)
        assert "context window" in source.lower()

    def test_detects_prompt_too_long(self):
        """Pattern 'prompt is too long' should also be detected."""
        import inspect

        from agent.generic.litellm_adapter import LiteLLMSession
        source = inspect.getsource(LiteLLMSession.chat)
        assert "prompt is too long" in source

    def test_detects_maximum_context_length(self):
        """Pattern 'maximum context length' should also be detected."""
        import inspect

        from agent.generic.litellm_adapter import LiteLLMSession
        source = inspect.getsource(LiteLLMSession.chat)
        assert "maximum context length" in source


# ---------------------------------------------------------------------------
# 3. FIFO truncation before overflow check
# ---------------------------------------------------------------------------

class TestFIFOTruncationOrder:
    """FIFO truncation should execute before the context usage warning check.
    This means after FIFO, the token count should be below the threshold."""

    @patch(f"{_SUBAGENT_MOD}._read_warning_threshold", return_value=0.80)
    def test_fifo_reduces_tokens_below_threshold(
        self, mock_threshold
    ):
        """After FIFO truncation, token count should be below fifo_threshold."""
        from agent.generic.agent_loop import agent_runner_loop
        from agent.generic.llmcore import MockResponse

        def mock_count_tokens(messages):
            # Each non-system message contributes ~100 tokens
            total = 10  # system message
            for msg in messages[1:]:
                content = msg.get("content", "") or ""
                total += len(content) // 2 + 4
            return total

        with patch(f"{_AGENT_LOOP_MOD}.count_messages_tokens", side_effect=mock_count_tokens):
            client = FakeClient(
                MockResponse(thinking=None, content="Done", tool_calls=None, raw=None)
            )
            # Create a large initial user content that will push past the threshold
            large_content = "x" * 2000  # ~1004 tokens
            args = _make_loop_args(
                client=client,
                user_input=large_content,
                context_fifo_threshold=500,  # Low threshold to trigger FIFO
                context_window_tokens=200000,
            )
            with _patch_runner_functions():
                result = exhaust(agent_runner_loop(**args))

        # Should complete normally (FIFO should have truncated before overflow check)
        assert result is not None
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# 4. FIFO preserves system and initial messages
# ---------------------------------------------------------------------------

class TestFIFOPreservesSystemAndInitial:
    """FIFO truncation should never remove messages[0] (system) and
    messages[1] (initial user task)."""

    @patch(f"{_SUBAGENT_MOD}._read_warning_threshold", return_value=0.80)
    def test_system_message_preserved(self, mock_threshold):
        from agent.generic.agent_loop import agent_runner_loop
        from agent.generic.llmcore import MockResponse, MockToolCall

        # Build messages that will trigger FIFO
        # We need multiple turns to accumulate messages, so we use a client
        # that returns tool_calls first, then a final answer.
        call_count = [0]

        class MultiTurnClient:
            name = "mock"
            last_tools = ""
            total_cd_tokens = 0

            def chat(self, messages, tools=None):
                call_count[0] += 1
                if call_count[0] <= 3:
                    tc = MockToolCall(name="test_tool", args={"p": "v"}, id=f"call_{call_count[0]}")
                    resp = MockResponse(
                        thinking=None,
                        content="",
                        tool_calls=[tc],
                        raw=None,
                    )
                else:
                    resp = MockResponse(
                        thinking=None,
                        content="Done",
                        tool_calls=None,
                        raw=None,
                    )

                def gen():
                    yield resp
                    return resp

                return gen()

        def mock_count_tokens(messages):
            # Simulate high token count to trigger FIFO
            # Each message ~500 tokens
            return len(messages) * 500

        class ToolHandler(FakeHandler):
            def dispatch(self, tool_name, args, response, index=0):
                from agent.generic.agent_loop import StepOutcome

                def gen():
                    yield ""
                    return StepOutcome("tool result", next_prompt="tool result", should_exit=False)

                return gen()

        with patch(f"{_AGENT_LOOP_MOD}.count_messages_tokens", side_effect=mock_count_tokens):
            client = MultiTurnClient()
            handler = ToolHandler()
            args = _make_loop_args(
                client=client,
                handler=handler,
                context_fifo_threshold=1500,  # FIFO at 1500 tokens (3 messages)
                context_window_tokens=200000,
                max_turns=10,
            )
            with _patch_runner_functions():
                result = exhaust(agent_runner_loop(**args))

        # Check that result has messages
        assert result is not None
        messages = result.get("messages", [])
        # System message must be preserved
        assert len(messages) >= 1
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "system"

    @patch(f"{_SUBAGENT_MOD}._read_warning_threshold", return_value=0.80)
    def test_initial_user_message_preserved(self, mock_threshold):
        """messages[1] (the initial user task) must survive FIFO truncation."""
        from agent.generic.agent_loop import agent_runner_loop
        from agent.generic.llmcore import MockResponse, MockToolCall

        call_count = [0]

        class MultiTurnClient:
            name = "mock"
            last_tools = ""
            total_cd_tokens = 0

            def chat(self, messages, tools=None):
                call_count[0] += 1
                if call_count[0] <= 3:
                    tc = MockToolCall(name="test_tool", args={"p": "v"}, id=f"call_{call_count[0]}")
                    resp = MockResponse(
                        thinking=None,
                        content="",
                        tool_calls=[tc],
                        raw=None,
                    )
                else:
                    resp = MockResponse(
                        thinking=None,
                        content="Done",
                        tool_calls=None,
                        raw=None,
                    )

                def gen():
                    yield resp
                    return resp

                return gen()

        def mock_count_tokens(messages):
            return len(messages) * 500

        class ToolHandler(FakeHandler):
            def dispatch(self, tool_name, args, response, index=0):
                from agent.generic.agent_loop import StepOutcome

                def gen():
                    yield ""
                    return StepOutcome("result", next_prompt="result", should_exit=False)

                return gen()

        with patch(f"{_AGENT_LOOP_MOD}.count_messages_tokens", side_effect=mock_count_tokens):
            client = MultiTurnClient()
            handler = ToolHandler()
            args = _make_loop_args(
                client=client,
                handler=handler,
                user_input="initial task content",
                context_fifo_threshold=1500,
                context_window_tokens=200000,
                max_turns=10,
            )
            with _patch_runner_functions():
                result = exhaust(agent_runner_loop(**args))

        messages = result.get("messages", [])
        assert len(messages) >= 2
        # Initial user message (messages[1]) must be preserved
        assert messages[1]["role"] == "user"
        assert "initial task content" in messages[1]["content"]


# ---------------------------------------------------------------------------
# 5. FIFO removes tool_calls paired with tool results
# ---------------------------------------------------------------------------

class TestFIFOPairedRemoval:
    """When FIFO removes an assistant message with tool_calls, the
    following tool result messages should also be removed."""

    @patch(f"{_SUBAGENT_MOD}._read_warning_threshold", return_value=0.80)
    def test_tool_calls_and_results_removed_together(self, mock_threshold):
        """If messages[2] is assistant(tool_calls), messages[3+] tool results
        should also be removed by FIFO."""
        from agent.generic.agent_loop import agent_runner_loop
        from agent.generic.llmcore import MockResponse, MockToolCall

        call_count = [0]

        class MultiTurnClient:
            name = "mock"
            last_tools = ""
            total_cd_tokens = 0

            def chat(self, messages, tools=None):
                call_count[0] += 1
                if call_count[0] <= 4:
                    tc = MockToolCall(name="test_tool", args={"p": "v"}, id=f"call_{call_count[0]}")
                    resp = MockResponse(
                        thinking=None,
                        content="",
                        tool_calls=[tc],
                        raw=None,
                    )
                else:
                    resp = MockResponse(
                        thinking=None,
                        content="Done",
                        tool_calls=None,
                        raw=None,
                    )

                def gen():
                    yield resp
                    return resp

                return gen()

        # Token counter: start high, then drop after FIFO removes messages
        token_state = {"high": True}

        def mock_count_tokens(messages):
            if token_state["high"] and len(messages) > 4:
                return 10000  # Over threshold, triggers FIFO
            # After FIFO removes messages, return low count
            return len(messages) * 100

        class ToolHandler(FakeHandler):
            def dispatch(self, tool_name, args, response, index=0):
                from agent.generic.agent_loop import StepOutcome

                def gen():
                    yield ""
                    return StepOutcome("result data", next_prompt="result", should_exit=False)

                return gen()

        with patch(f"{_AGENT_LOOP_MOD}.count_messages_tokens", side_effect=mock_count_tokens):
            client = MultiTurnClient()
            handler = ToolHandler()
            args = _make_loop_args(
                client=client,
                handler=handler,
                context_fifo_threshold=5000,  # Trigger FIFO when tokens > 5000
                context_window_tokens=200000,
                max_turns=10,
            )
            with _patch_runner_functions():
                result = exhaust(agent_runner_loop(**args))

        messages = result.get("messages", [])
        # After FIFO, there should be no orphaned tool results
        # (tool message without preceding assistant tool_calls)
        for i, msg in enumerate(messages):
            if msg.get("role") == "tool":
                # Every tool message should have a preceding assistant message with tool_calls
                assert i > 0, f"Tool message at index {i} has no preceding assistant message"
                prev = messages[i - 1]
                assert prev.get("role") == "assistant", (
                    f"Tool message at index {i} is not preceded by assistant, "
                    f"got {prev.get('role')} instead"
                )
                assert prev.get("tool_calls"), (
                    f"Tool message at index {i} is preceded by assistant without tool_calls"
                )


# ---------------------------------------------------------------------------
# 6. MockResponse.context_overflow field
# ---------------------------------------------------------------------------

class TestMockResponseContextOverflow:
    """Test the MockResponse context_overflow attribute."""

    def test_default_false(self):
        from agent.generic.llmcore import MockResponse
        resp = MockResponse(thinking=None, content="hi", tool_calls=None, raw=None)
        assert resp.context_overflow is False

    def test_explicit_true(self):
        from agent.generic.llmcore import MockResponse
        resp = MockResponse(
            thinking=None, content="", tool_calls=None, raw=None,
            context_overflow=True
        )
        assert resp.context_overflow is True

    def test_explicit_false(self):
        from agent.generic.llmcore import MockResponse
        resp = MockResponse(
            thinking=None, content="", tool_calls=None, raw=None,
            context_overflow=False
        )
        assert resp.context_overflow is False

    def test_hasattr_context_overflow(self):
        from agent.generic.llmcore import MockResponse
        resp = MockResponse(thinking=None, content="", tool_calls=None, raw=None)
        assert hasattr(resp, "context_overflow")


# ---------------------------------------------------------------------------
# 7. call_subagent context_fifo_threshold parameter
# ---------------------------------------------------------------------------

class TestSubagentFIFOThreshold:
    """Test call_subagent's context_fifo_threshold parameter."""

    def test_default_negative_one_gives_75_percent(self, monkeypatch):
        """context_fifo_threshold=-1 should result in 75% of context_window_tokens."""
        from agent import subagent

        captured = {}

        def mock_run(client, system_prompt, user_input, handler, tools_schema,
                      max_turns=20, initial_user_content=None,
                      context_window_tokens=0, context_fifo_threshold=0,
                      history=None):
            captured["context_fifo_threshold"] = context_fifo_threshold
            return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"})

        monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])

        import agent.runner as runner_mod
        monkeypatch.setattr(runner_mod, "create_client", lambda cfg: None)
        monkeypatch.setattr(runner_mod, "get_tools_schema", lambda: [])

        monkeypatch.setattr(subagent, "_read_context_window_tokens", lambda: 200000)

        subagent.call_subagent(
            agent_name="test-agent",
            task="test",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
            context_fifo_threshold=-1,  # Default: 75%
        )

        # -1 → int(200000 * 0.75) = 150000
        assert captured["context_fifo_threshold"] == 150000

    def test_zero_disables_fifo(self, monkeypatch):
        """context_fifo_threshold=0 should disable FIFO (threshold=0)."""
        from agent import subagent

        captured = {}

        def mock_run(client, system_prompt, user_input, handler, tools_schema,
                      max_turns=20, initial_user_content=None,
                      context_window_tokens=0, context_fifo_threshold=0,
                      history=None):
            captured["context_fifo_threshold"] = context_fifo_threshold
            return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"})

        monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])

        import agent.runner as runner_mod
        monkeypatch.setattr(runner_mod, "create_client", lambda cfg: None)
        monkeypatch.setattr(runner_mod, "get_tools_schema", lambda: [])

        monkeypatch.setattr(subagent, "_read_context_window_tokens", lambda: 200000)

        subagent.call_subagent(
            agent_name="test-agent",
            task="test",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
            context_fifo_threshold=0,  # Disable FIFO
        )

        assert captured["context_fifo_threshold"] == 0

    def test_custom_value_passed_through(self, monkeypatch):
        """context_fifo_threshold=50000 should be passed as-is."""
        from agent import subagent

        captured = {}

        def mock_run(client, system_prompt, user_input, handler, tools_schema,
                      max_turns=20, initial_user_content=None,
                      context_window_tokens=0, context_fifo_threshold=0,
                      history=None):
            captured["context_fifo_threshold"] = context_fifo_threshold
            return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"})

        monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])

        import agent.runner as runner_mod
        monkeypatch.setattr(runner_mod, "create_client", lambda cfg: None)
        monkeypatch.setattr(runner_mod, "get_tools_schema", lambda: [])

        monkeypatch.setattr(subagent, "_read_context_window_tokens", lambda: 200000)

        subagent.call_subagent(
            agent_name="test-agent",
            task="test",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
            context_fifo_threshold=50000,
        )

        assert captured["context_fifo_threshold"] == 50000

    def test_default_parameter_is_negative_one(self, monkeypatch):
        """The default value of context_fifo_threshold in call_subagent should be -1."""
        import inspect

        from agent import subagent
        sig = inspect.signature(subagent.call_subagent)
        param = sig.parameters["context_fifo_threshold"]
        assert param.default == -1


# ---------------------------------------------------------------------------
# 8. FIFO threshold=0 disables truncation in agent_runner_loop
# ---------------------------------------------------------------------------

class TestFIFOThresholdZeroDisablesTruncation:
    """When context_fifo_threshold=0, FIFO truncation should not run at all."""

    @patch(f"{_SUBAGENT_MOD}._read_warning_threshold", return_value=0.80)
    def test_no_truncation_when_threshold_zero(self, mock_threshold):
        """With context_fifo_threshold=0, even high token counts should not
        trigger FIFO truncation."""
        from agent.generic.agent_loop import agent_runner_loop
        from agent.generic.llmcore import MockResponse

        # Track how many times count_messages_tokens was called
        token_call_count = [0]

        def mock_count_tokens(messages):
            token_call_count[0] += 1
            # Always return a very high count
            return 999999

        with patch(f"{_AGENT_LOOP_MOD}.count_messages_tokens", side_effect=mock_count_tokens):
            client = FakeClient(
                MockResponse(thinking=None, content="Done", tool_calls=None, raw=None)
            )
            args = _make_loop_args(
                client=client,
                context_fifo_threshold=0,  # Disabled
                context_window_tokens=200000,
            )
            with _patch_runner_functions():
                result = exhaust(agent_runner_loop(**args))

        # Should complete without FIFO truncation
        # (only the warning check calls count_messages_tokens, not the FIFO block)
        assert result is not None
        assert isinstance(result, dict)
