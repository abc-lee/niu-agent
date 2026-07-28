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


class TestNoPromptChunking:
    """Verify prompt chunking has been removed — call_subagent always executes in one pass."""

    def test_split_prompt_by_tokens_not_exported(self):
        """split_prompt_by_tokens should no longer be importable."""
        import agent.subagent as subagent_mod
        assert not hasattr(subagent_mod, "split_prompt_by_tokens")

    def test_prompt_chunk_limit_not_exported(self):
        """PROMPT_CHUNK_TOKEN_LIMIT should no longer exist."""
        import agent.subagent as subagent_mod
        assert not hasattr(subagent_mod, "PROMPT_CHUNK_TOKEN_LIMIT")

    def test_call_subagent_executes_long_task_in_one_pass(self, monkeypatch):
        """Even with a very long task, call_subagent should call _run_agent_loop exactly once."""
        from agent import subagent

        call_count = 0

        def mock_run(client, system_prompt, user_input, handler, tools_schema,
                      max_turns=20, initial_user_content=None, context_window_tokens=0,
                      context_fifo_threshold=0, history=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"})

        monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])

        import agent.runner as runner_mod
        monkeypatch.setattr(runner_mod, "create_client", lambda cfg: None)
        monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])

        monkeypatch.setattr(subagent, "_read_context_window_tokens", lambda: 200000)

        # Very long task that would have been chunked before
        long_task = "消息内容 " * 50000  # ~100K chars, would exceed old 50K limit
        result = subagent.call_subagent(
            agent_name="test-agent",
            task=long_task,
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
        )

        assert call_count == 1  # Single pass, no chunking


class TestAgentLoopTokenThreshold:
    """Test that agent_runner_loop exits at warningThreshold token usage."""

    def test_high_usage_does_not_proactively_exit(self, monkeypatch):
        """When token usage exceeds warningThreshold, agent_runner_loop should NOT
        proactively return CONTEXT_OVERFLOW. It should only log a warning and continue.
        CONTEXT_OVERFLOW is now triggered only by LLM API context_length_exceeded errors."""
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

        # Use a very small context window — would have triggered proactive exit before
        gen = agent_runner_loop(
            client=client,
            system_prompt="system",
            user_input="x" * 10000,  # Large input
            handler=handler,
            tools_schema=[],
            max_turns=40,
            verbose=False,
            context_window_tokens=100,  # Very small → high usage ratio
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
        # Should NOT be CONTEXT_OVERFLOW — only a warning was logged
        assert return_value.get("result") != "CONTEXT_OVERFLOW"

    def test_context_overflow_on_llm_error(self, monkeypatch):
        """When LLM API returns context_overflow=True, agent_runner_loop should
        return CONTEXT_OVERFLOW."""
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
                    content="",
                    tool_calls=None,
                    raw=None,
                    context_overflow=True,  # LLM API returned context_length_exceeded
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
            user_input="test",
            handler=handler,
            tools_schema=[],
            max_turns=40,
            verbose=False,
            context_window_tokens=100,
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

        def mock_run_agent_loop(client, system_prompt, user_input, handler, tools_schema, max_turns=20, initial_user_content=None, context_window_tokens=0, context_fifo_threshold=0, history=None, **kwargs):
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
        monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])

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

        def mock_run(client, system_prompt, user_input, handler, tools_schema, max_turns=20, initial_user_content=None, context_window_tokens=0, context_fifo_threshold=0, history=None, **kwargs):
            captured_kwargs["context_window_tokens"] = context_window_tokens
            return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"})

        monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])

        # Patch the lazy imports inside call_subagent
        import agent.runner as runner_mod
        monkeypatch.setattr(runner_mod, "create_client", lambda cfg: None)
        monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])

        monkeypatch.setattr(subagent, "_read_context_window_tokens", lambda: 128000)

        result = subagent.call_subagent(
            agent_name="test-agent",
            task="test",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
        )

        assert captured_kwargs.get("context_window_tokens") == 128000


class TestExtractResultFromReturnValue:
    """Test _extract_result_from_return_value handles control flow dicts correctly."""

    def test_control_flow_context_overflow_returns_none(self):
        from agent.subagent import _extract_result_from_return_value
        result = _extract_result_from_return_value({
            "result": "CONTEXT_OVERFLOW",
            "data": {"overflow": True, "turns_completed": 5},
        })
        assert result is None

    def test_control_flow_exited_returns_none(self):
        from agent.subagent import _extract_result_from_return_value
        result = _extract_result_from_return_value({
            "result": "EXITED",
            "data": None,
        })
        assert result is None

    def test_control_flow_max_turns_returns_none(self):
        from agent.subagent import _extract_result_from_return_value
        result = _extract_result_from_return_value({
            "result": "MAX_TURNS_EXCEEDED",
            "data": None,
        })
        assert result is None

    def test_control_flow_current_task_done_returns_none(self):
        from agent.subagent import _extract_result_from_return_value
        result = _extract_result_from_return_value({
            "result": "CURRENT_TASK_DONE",
            "data": "task completed",
        })
        assert result is None

    def test_data_dict_returns_json(self):
        from agent.subagent import _extract_result_from_return_value
        result = _extract_result_from_return_value({
            "data": {"key": "value", "count": 42},
        })
        assert result is not None
        import json
        parsed = json.loads(result)
        assert parsed["key"] == "value"
        assert parsed["count"] == 42

    def test_none_return_value_returns_none(self):
        from agent.subagent import _extract_result_from_return_value
        assert _extract_result_from_return_value(None) is None

    def test_non_dict_return_value_returns_none(self):
        from agent.subagent import _extract_result_from_return_value
        assert _extract_result_from_return_value("just a string") is None
        assert _extract_result_from_return_value(42) is None


class TestTidyFlowOrder:
    """Verify tidy_context calls entity-extractor → dream-evolver → context-manager in order."""

    def test_sleep_mode_calls_three_agents_in_order(self):
        """Sleep mode should call entity-extractor, then dream-evolver, then context-manager."""
        from niu_api.compat import _is_subagent_overflow, _extract_overflow_info
        # This is a structural test: verify the code path exists
        # by checking the source code contains entity-extractor calls
        import inspect
        from niu_api import compat
        source = inspect.getsource(compat._tidy_context_impl)
        # entity-extractor must appear before dream-evolver in sleep mode
        entity_pos = source.find("entity-extractor")
        dream_pos = source.find("dream-evolver")
        context_pos = source.find("context-manager")
        # All three should be present
        assert entity_pos > 0, "entity-extractor not found in _tidy_context_impl"
        assert dream_pos > 0, "dream-evolver not found in _tidy_context_impl"
        assert context_pos > 0, "context-manager not found in _tidy_context_impl"
        # entity-extractor must come before dream-evolver
        assert entity_pos < dream_pos, "entity-extractor must be called before dream-evolver"
        # dream-evolver must come before context-manager
        assert dream_pos < context_pos, "dream-evolver must be called before context-manager"


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


class TestTruncateMessageContent:
    """Test truncate_message_content for snowball compression."""

    def test_short_content_not_truncated(self):
        from niu_api.compat import truncate_message_content
        content = "这是一条短消息"
        result = truncate_message_content(content, max_chars=500)
        assert result == content

    def test_long_content_truncated(self):
        from niu_api.compat import truncate_message_content
        content = "x" * 1000
        result = truncate_message_content(content, max_chars=500)
        assert len(result) < len(content)
        assert result.startswith(content[:500])
        assert "截断" in result

    def test_empty_content_returns_empty(self):
        from niu_api.compat import truncate_message_content
        assert truncate_message_content("", max_chars=500) == ""

    def test_truncation_includes_original_length_info(self):
        from niu_api.compat import truncate_message_content
        content = "a" * 2000
        result = truncate_message_content(content, max_chars=500)
        assert "2000" in result  # 原始长度信息


class TestBuildTruncatedMsgListText:
    """Test build_truncated_msg_list_text for force-mode snowball compression."""

    def test_truncated_list_shorter_than_full(self):
        from niu_api.compat import build_truncated_msg_list_text
        # 构造长消息列表
        messages = []
        for i in range(20):
            msg = type("Msg", (), {
                "id": f"msg-{i}",
                "role": "user",
                "content": "内容" * 500,  # 每条 1000 字符
            })()
            messages.append(msg)
        full = build_truncated_msg_list_text(messages, truncate=False)
        truncated = build_truncated_msg_list_text(messages, truncate=True, max_chars=500)
        assert len(truncated) < len(full)

    def test_truncated_preserves_uuid_and_metadata(self):
        from niu_api.compat import build_truncated_msg_list_text
        msg = type("Msg", (), {
            "id": "test-uuid-123",
            "role": "user",
            "content": "x" * 2000,
        })()
        result = build_truncated_msg_list_text([msg], truncate=True, max_chars=500)
        assert "test-uuid-123" in result
        assert "user" in result

    def test_no_truncate_returns_full_content(self):
        from niu_api.compat import build_truncated_msg_list_text
        msg = type("Msg", (), {
            "id": "msg-1",
            "role": "assistant",
            "content": "完整内容",
        })()
        result = build_truncated_msg_list_text([msg], truncate=False)
        assert "完整内容" in result

