"""Tests for sub-agent context overflow protection."""
import pytest


class TestCountTokensForText:
    """Test the token counting utility for sub-agent prompts."""

    def test_empty_string_returns_zero(self):
        from agent.subagent import count_tokens_for_text
        assert count_tokens_for_text("") == 0

    def test_short_text_returns_positive(self):
        from agent.subagent import count_tokens_for_text
        tokens = count_tokens_for_text("Hello world")
        assert tokens > 0

    def test_chinese_text_counts_correctly(self):
        from agent.subagent import count_tokens_for_text
        text = "这是一段中文测试文本"
        tokens = count_tokens_for_text(text)
        assert tokens > 0
        assert 3 <= tokens <= 15

    def test_long_text_counts_more(self):
        from agent.subagent import count_tokens_for_text
        short = "Hello world"
        long = "Hello world " * 100
        assert count_tokens_for_text(long) > count_tokens_for_text(short)


class TestSplitPromptByTokens:
    """Test prompt splitting for sub-agent overflow protection."""

    def test_short_prompt_no_split(self):
        from agent.subagent import split_prompt_by_tokens
        chunks = split_prompt_by_tokens("Hello world", max_tokens_per_chunk=50000)
        assert len(chunks) == 1
        assert chunks[0] == "Hello world"

    def test_long_prompt_splits(self):
        from agent.subagent import split_prompt_by_tokens
        lines = [f"消息 {i}: 这是一段测试内容用于验证分片功能" for i in range(200)]
        prompt = "\n".join(lines)
        chunks = split_prompt_by_tokens(prompt, max_tokens_per_chunk=200)
        assert len(chunks) >= 2

    def test_empty_prompt_returns_empty_list(self):
        from agent.subagent import split_prompt_by_tokens
        chunks = split_prompt_by_tokens("", max_tokens_per_chunk=50000)
        assert chunks == []

    def test_single_long_line_not_split(self):
        from agent.subagent import split_prompt_by_tokens
        long_line = "测试" * 10000
        chunks = split_prompt_by_tokens(long_line, max_tokens_per_chunk=100)
        assert len(chunks) == 1
        assert chunks[0] == long_line

    def test_chunks_preserve_content(self):
        from agent.subagent import split_prompt_by_tokens
        lines = [f"消息 {i}: 内容" for i in range(50)]
        prompt = "\n".join(lines)
        chunks = split_prompt_by_tokens(prompt, max_tokens_per_chunk=200)
        rejoined = "\n".join(chunks)
        assert rejoined == prompt


class TestAgentLoopTokenThreshold:
    """Test that agent_runner_loop exits at 85% token usage."""

    def test_overflow_returns_structured_report(self, monkeypatch):
        """When token usage exceeds 85%, agent_runner_loop should return CONTEXT_OVERFLOW."""
        from agent.generic.agent_loop import agent_runner_loop
        from agent.generic.llmcore import MockResponse

        class MockClient:
            name = "mock"
            last_tools = ""
            total_cd_tokens = 0
            _call_count = 0

            def chat(self, messages, tools=None):
                self._call_count += 1
                resp = MockResponse(
                    thinking=None,
                    content="work result",
                    tool_calls=None,
                    raw=None,
                )
                def gen():
                    yield resp
                    return resp
                return gen()

        class MockHandler:
            _done_hooks = []
            max_turns = 40
            current_turn = 0

            def dispatch(self, tool_name, args, response, index=0):
                from agent.generic.agent_loop import StepOutcome
                def gen():
                    yield ""
                    return StepOutcome(None, next_prompt="continue", should_exit=False)
                return gen()

            def next_prompt_patcher(self, next_prompt, outcome, turn):
                return next_prompt

        client = MockClient()
        handler = MockHandler()

        # Use a very small context window to force overflow quickly
        gen = agent_runner_loop(
            client=client,
            system_prompt="system",
            user_input="x" * 10000,  # Large input
            handler=handler,
            tools_schema=[],
            max_turns=40,
            verbose=False,
            context_window_tokens=100,  # Very small → immediate overflow
        )

        result_text = ""
        return_value = None
        while True:
            try:
                chunk = next(gen)
                if isinstance(chunk, str):
                    result_text += chunk
            except StopIteration as e:
                return_value = e.value
                break

        assert return_value is not None
        assert isinstance(return_value, dict)
        assert return_value.get("result") == "CONTEXT_OVERFLOW"
        assert return_value["data"]["overflow"] is True
        assert return_value["data"]["tokens_limit"] == 100

    def test_no_overflow_with_large_window(self, monkeypatch):
        """When context window is large enough, no overflow should occur."""
        from agent.generic.agent_loop import agent_runner_loop
        from agent.generic.llmcore import MockResponse

        class MockClient:
            name = "mock"
            last_tools = ""
            total_cd_tokens = 0

            def chat(self, messages, tools=None):
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

        class MockHandler:
            _done_hooks = []
            max_turns = 40
            current_turn = 0

            def dispatch(self, tool_name, args, response, index=0):
                from agent.generic.agent_loop import StepOutcome
                def gen():
                    yield ""
                    return StepOutcome(None, next_prompt="continue", should_exit=False)
                return gen()

            def next_prompt_patcher(self, next_prompt, outcome, turn):
                return next_prompt

        client = MockClient()
        handler = MockHandler()

        gen = agent_runner_loop(
            client=client,
            system_prompt="system",
            user_input="small task",
            handler=handler,
            tools_schema=[],
            max_turns=40,
            verbose=False,
            context_window_tokens=200000,  # Large → no overflow
        )

        result_text = ""
        return_value = None
        while True:
            try:
                chunk = next(gen)
                if isinstance(chunk, str):
                    result_text += chunk
            except StopIteration as e:
                return_value = e.value
                break

        # Should NOT be overflow
        if isinstance(return_value, dict):
            assert return_value.get("result") != "CONTEXT_OVERFLOW"

    def test_zero_context_window_disables_check(self, monkeypatch):
        """When context_window_tokens=0, no overflow check should occur."""
        from agent.generic.agent_loop import agent_runner_loop
        from agent.generic.llmcore import MockResponse

        class MockClient:
            name = "mock"
            last_tools = ""
            total_cd_tokens = 0

            def chat(self, messages, tools=None):
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

        class MockHandler:
            _done_hooks = []
            max_turns = 40
            current_turn = 0

            def dispatch(self, tool_name, args, response, index=0):
                from agent.generic.agent_loop import StepOutcome
                def gen():
                    yield ""
                    return StepOutcome(None, next_prompt="continue", should_exit=False)
                return gen()

            def next_prompt_patcher(self, next_prompt, outcome, turn):
                return next_prompt

        client = MockClient()
        handler = MockHandler()

        gen = agent_runner_loop(
            client=client,
            system_prompt="system",
            user_input="x" * 10000,
            handler=handler,
            tools_schema=[],
            max_turns=40,
            verbose=False,
            context_window_tokens=0,  # Disabled
        )

        result_text = ""
        return_value = None
        while True:
            try:
                chunk = next(gen)
                if isinstance(chunk, str):
                    result_text += chunk
            except StopIteration as e:
                return_value = e.value
                break

        # Should NOT be overflow even with large input
        if isinstance(return_value, dict):
            assert return_value.get("result") != "CONTEXT_OVERFLOW"


class TestOverflowResultPropagation:
    """Test that call_subagent properly handles CONTEXT_OVERFLOW from agent_runner_loop."""

    def test_overflow_result_includes_progress(self, monkeypatch):
        from agent import subagent

        def mock_run_agent_loop(agent_name, client, system_prompt, user_input, handler, tools_schema, max_turns=20, initial_user_content=None, context_window_tokens=0):
            return (
                "partial work done",
                {
                    "result": "CONTEXT_OVERFLOW",
                    "data": {
                        "overflow": True,
                        "turns_completed": 5,
                        "tokens_used": 170000,
                        "tokens_limit": 200000,
                    },
                },
            )

        monkeypatch.setattr(subagent, "_run_agent_loop", mock_run_agent_loop)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])

        # Patch the lazy imports inside call_subagent
        import agent.runner as runner_mod
        monkeypatch.setattr(runner_mod, "create_client", lambda cfg: None)
        monkeypatch.setattr(runner_mod, "get_tools_schema", lambda: [])

        result = subagent.call_subagent(
            agent_name="test-agent",
            task="task that overflows",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
        )

        assert "overflow" in result.lower()
        assert "170000" in result or "turns_completed" in result


class TestSubagentContextWindowConfig:
    """Test that sub-agent receives context_window_tokens from preferences."""

    def test_context_window_tokens_passed_to_loop(self, monkeypatch):
        from agent import subagent

        captured_kwargs = {}

        def mock_run(agent_name, client, system_prompt, user_input, handler, tools_schema, max_turns=20, initial_user_content=None, context_window_tokens=0):
            captured_kwargs["context_window_tokens"] = context_window_tokens
            return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"})

        monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])

        # Patch the lazy imports inside call_subagent
        import agent.runner as runner_mod
        monkeypatch.setattr(runner_mod, "create_client", lambda cfg: None)
        monkeypatch.setattr(runner_mod, "get_tools_schema", lambda: [])

        monkeypatch.setattr(subagent, "_read_context_window_tokens", lambda: 128000)

        result = subagent.call_subagent(
            agent_name="test-agent",
            task="test",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
        )

        assert captured_kwargs.get("context_window_tokens") == 128000


class TestCompatOverflowHandling:
    """Test that compat.py handles sub-agent overflow results."""

    def test_detects_overflow_in_subagent_result(self):
        from niu_api.compat import _is_subagent_overflow
        overflow_json = '{"overflow": true, "agent": "context-manager", "turns_completed": 5, "tokens_used": 170000, "tokens_limit": 200000}'
        assert _is_subagent_overflow(overflow_json) is True

    def test_normal_result_not_overflow(self):
        from niu_api.compat import _is_subagent_overflow
        assert _is_subagent_overflow("normal result text") is False
        assert _is_subagent_overflow('{"status": "ok"}') is False

    def test_extract_overflow_info(self):
        from niu_api.compat import _extract_overflow_info
        overflow_json = '{"overflow": true, "agent": "context-manager", "turns_completed": 5, "tokens_used": 170000, "tokens_limit": 200000, "partial_result": "some work"}'
        info = _extract_overflow_info(overflow_json)
        assert info["overflow"] is True
        assert info["agent"] == "context-manager"
        assert info["turns_completed"] == 5
