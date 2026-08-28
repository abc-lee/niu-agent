"""Tests for sub-agent context overflow protection."""


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
            return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"}, "")

        monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])

        from unittest.mock import Mock

        import agent.runner as runner_mod
        monkeypatch.setattr(runner_mod, "create_client", lambda cfg: Mock())
        monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])

        monkeypatch.setattr(subagent, "_read_context_window_tokens", lambda: 200000)

        # Very long task that would have been chunked before
        long_task = "消息内容 " * 50000  # ~100K chars, would exceed old 50K limit
        subagent.call_subagent(
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
                "",
            )

        monkeypatch.setattr(subagent, "_run_agent_loop", mock_run_agent_loop)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])

        # Patch the lazy imports inside call_subagent
        from unittest.mock import Mock

        import agent.runner as runner_mod
        monkeypatch.setattr(runner_mod, "create_client", lambda cfg: Mock())
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
            return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"}, "")

        monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])

        # Patch the lazy imports inside call_subagent
        from unittest.mock import Mock

        import agent.runner as runner_mod
        monkeypatch.setattr(runner_mod, "create_client", lambda cfg: Mock())
        monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])

        monkeypatch.setattr(subagent, "_read_context_window_tokens", lambda: 128000)

        subagent.call_subagent(
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
    """T6 压缩退役后睡眠管道序：journal(≥50%) → entity-extractor → dream-evolver。"""

    def test_sleep_mode_calls_three_agents_in_order(self):
        """Sleep mode should call journal-agent first, then entity-extractor, then dream-evolver."""
        # This is a structural test: verify the code path exists
        # by checking the source code contains agent call sites.
        # 注意：分支头注释已按新序书写（旧序文字残留会使 find 首现命中落在注释内致断言必败）。
        import inspect

        from niu_api import compat
        source = inspect.getsource(compat._tidy_context_impl)
        # 按源码文本位置断言新序：journal-agent → entity-extractor → dream-evolver
        journal_pos = source.find("journal-agent")
        entity_pos = source.find("entity-extractor")
        dream_pos = source.find("dream-evolver")
        # All three should be present（context-manager 已退役，反向钉零出现）
        assert journal_pos > 0, "journal-agent not found in _tidy_context_impl"
        assert entity_pos > 0, "entity-extractor not found in _tidy_context_impl"
        assert dream_pos > 0, "dream-evolver not found in _tidy_context_impl"
        assert "context-manager" not in source, "context-manager 已退役，不应再出现在 _tidy_context_impl"
        # journal-agent must come before entity-extractor (sleep pipeline first leg)
        assert journal_pos < entity_pos, "journal-agent must be called before entity-extractor"
        # entity-extractor must come before dream-evolver
        assert entity_pos < dream_pos, "entity-extractor must be called before dream-evolver"


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



class TestSubagentMaxTurnsPassthrough:
    """call_subagent 的 max_turns 参数透传到 _run_agent_loop 三路径（Task 2）。

    默认 None（无上限）、显式传值透传、resume（answer 非 None）透传。
    create_client → Mock()（自动带 .backend，防 L866 stop_check AttributeError）。
    """

    def _setup(self, monkeypatch, captured):
        from unittest.mock import Mock

        from agent import subagent
        import agent.runner as runner_mod

        def mock_run(client, system_prompt, user_input, handler, tools_schema, **kwargs):
            captured.update(kwargs)
            return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"}, "")

        monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
        monkeypatch.setattr(runner_mod, "create_client", lambda cfg: Mock())
        monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
        return subagent

    def test_default_max_turns_none_passed_to_loop(self, monkeypatch):
        captured = {}
        subagent = self._setup(monkeypatch, captured)
        subagent.call_subagent(
            agent_name="test-agent",
            task="test",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
        )
        assert "max_turns" in captured, f"_run_agent_loop 未收到 max_turns: {captured}"
        assert captured["max_turns"] is None

    def test_explicit_max_turns_passed_through(self, monkeypatch):
        captured = {}
        subagent = self._setup(monkeypatch, captured)
        subagent.call_subagent(
            agent_name="test-agent",
            task="test",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
            max_turns=5,
        )
        assert captured.get("max_turns") == 5

    def test_resume_path_passes_max_turns_none(self, monkeypatch):
        """resume 路径（answer 非 None）也要把 max_turns 透传给 _run_agent_loop。"""
        from unittest.mock import Mock

        from agent.subagent_registry import SubagentRegistry, RunningSubagent
        from agent.subagent_supplement import SubagentSupplementQueue

        captured = {}
        subagent = self._setup(monkeypatch, captured)

        inst = RunningSubagent(
            unique_name="resume-test",
            agent_type="test-agent",
            supplement_queue=SubagentSupplementQueue(unique_name="resume-test"),
            state="waiting_for_answer",
            suspended_messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "orig"}],
            suspended_handler=Mock(),
            suspended_client=Mock(),
            suspended_system_message={"role": "system", "content": "sys"},
            suspended_tools_schema=[],
            source="user",
        )
        SubagentRegistry._instances["resume-test"] = inst
        try:
            subagent.call_subagent(
                agent_name="test-agent",
                task="ignored (resume path)",
                llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
                answer="继续任务",
                answer_unique_name="resume-test",
            )
        finally:
            SubagentRegistry._instances.pop("resume-test", None)

        assert "max_turns" in captured, f"resume 路径未收到 max_turns: {captured}"
        assert captured["max_turns"] is None


class TestSubagentIncompleteResult:
    """call_subagent 后处理 incomplete JSON 分支（Task 3）。

    未完成终止（MAX_TURNS_EXCEEDED/STOPPED/TERMINATED_BY_SUPPLEMENT）返回结构化
    {"incomplete": true, ...} JSON，避免中间文本被调用方误判为成功（游标误推进）。
    分支必须优先于 finish_reason=length 判断（终止总结截断时仍带 incomplete 标记）。
    """

    def _call_with_return(self, monkeypatch, result_text, return_value, last_reply):
        import json

        from agent import subagent
        import agent.runner as runner_mod
        from unittest.mock import Mock

        def mock_run(client, system_prompt, user_input, handler, tools_schema, **kwargs):
            return (result_text, return_value, last_reply)

        monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
        monkeypatch.setattr(runner_mod, "create_client", lambda cfg: Mock())
        monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
        return subagent.call_subagent(
            agent_name="test-agent",
            task="task",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
        )

    def test_max_turns_exceeded_returns_incomplete_json(self, monkeypatch):
        import json

        result = self._call_with_return(
            monkeypatch,
            "中间累加文本",
            {"result": "MAX_TURNS_EXCEEDED", "messages": []},
            "再精简几个小工具输出：idx:33",
        )
        data = json.loads(result)
        assert data.get("incomplete") is True
        assert data.get("agent") == "test-agent"
        assert data.get("reason") == "MAX_TURNS_EXCEEDED"
        assert data.get("partial_result") == "再精简几个小工具输出：idx:33"

    def test_stopped_returns_incomplete_json(self, monkeypatch):
        import json

        result = self._call_with_return(
            monkeypatch,
            "部分进度",
            {"result": "STOPPED", "messages": []},
            "被用户停止前的最后回复",
        )
        data = json.loads(result)
        assert data.get("incomplete") is True
        assert data.get("reason") == "STOPPED"
        assert data.get("partial_result") == "被用户停止前的最后回复"

    def test_terminated_by_supplement_returns_incomplete_json(self, monkeypatch):
        import json

        result = self._call_with_return(
            monkeypatch,
            "部分进度",
            {"result": "TERMINATED_BY_SUPPLEMENT", "messages": []},
            "/stop 终止总结",
        )
        data = json.loads(result)
        assert data.get("incomplete") is True
        assert data.get("reason") == "TERMINATED_BY_SUPPLEMENT"
        assert data.get("partial_result") == "/stop 终止总结"

    def test_terminated_by_supplement_with_length_prefers_incomplete(self, monkeypatch):
        """边界：TERMINATED_BY_SUPPLEMENT + finish_reason=length 时必须返 incomplete JSON，
        不能被 length 分支抢先拦截成 COMPACT_TRUNCATED（R2-3）。"""
        import json

        result = self._call_with_return(
            monkeypatch,
            "部分进度",
            {"result": "TERMINATED_BY_SUPPLEMENT", "messages": [], "finish_reason": "length"},
            "终止总结被截断的内容",
        )
        assert not result.startswith("COMPACT_TRUNCATED:"), f"length 分支抢先拦截: {result}"
        data = json.loads(result)
        assert data.get("incomplete") is True
        assert data.get("reason") == "TERMINATED_BY_SUPPLEMENT"

    def test_current_task_done_not_incomplete(self, monkeypatch):
        """负例：正常完成 CURRENT_TASK_DONE 不得误中 incomplete 分支（R4-3）。"""
        import json

        result = self._call_with_return(
            monkeypatch,
            "全部处理完成",
            {"result": "CURRENT_TASK_DONE", "data": None},
            "最终报告",
        )
        assert result == "最终报告", f"正常完成应回退 last_reply: {result}"
        try:
            data = json.loads(result)
            assert data.get("incomplete") is not True
        except (json.JSONDecodeError, TypeError):
            pass  # 非 JSON（正常回退文本）也满足

    def test_stopped_empty_last_reply_partial_empty(self, monkeypatch):
        """STOPPED 首轮空 last_reply → partial_result=''（R4-4）。"""
        import json

        result = self._call_with_return(
            monkeypatch,
            "",
            {"result": "STOPPED", "messages": []},
            "",
        )
        data = json.loads(result)
        assert data.get("incomplete") is True
        assert data.get("partial_result") == ""

    def test_partial_result_truncated_to_2000(self, monkeypatch):
        """partial_result 截断 ≤2000 字符（R1-4）。"""
        import json

        long_reply = "x" * 3000
        result = self._call_with_return(
            monkeypatch,
            "",
            {"result": "MAX_TURNS_EXCEEDED", "messages": []},
            long_reply,
        )
        data = json.loads(result)
        assert data.get("incomplete") is True
        assert len(data.get("partial_result", "")) == 2000


class TestFifoPruneMarker:
    """_fifo_prune 真删后在切割位置（protect_end）插入可见标记消息（user 角色）。"""

    @staticmethod
    def _mk_msgs(n_turns, with_initial_user=True):
        msgs = [{"role": "system", "content": "sys"}]
        if with_initial_user:
            msgs.append({"role": "user", "content": "任务"})
        for i in range(n_turns):
            msgs.append({"role": "assistant", "content": f"reply{i}"})
            msgs.append({"role": "user", "content": f"next{i}"})
        return msgs

    def test_marker_inserted_after_prune(self, monkeypatch):
        """真删后：标记存在、位置恰在受保护头部之后（protect_end=2）、内容含删除条数。"""
        from agent.generic import agent_loop as al

        msgs = self._mk_msgs(5)  # len=12，protect_end=2 → 可删 10 条
        monkeypatch.setattr(al, "count_messages_tokens", lambda m: 10**9)  # 恒超 target
        removed = al._fifo_prune(msgs, 100)
        assert removed == 10
        # 受保护头部原样 + 标记恰在其后（index 2）
        assert msgs[0] == {"role": "system", "content": "sys"}
        assert msgs[1] == {"role": "user", "content": "任务"}
        assert len(msgs) == 3  # 12 - 10 + 1（标记自身不计入返回值）
        marker = msgs[2]
        assert marker["role"] == "user"
        assert marker["content"] == "[上下文提示：更早的 10 条消息已因上下文超限被移除]"

    def test_no_marker_when_nothing_removed(self, monkeypatch):
        """removed==0（token 未超 target）→ 不插标记，messages 原样。"""
        from agent.generic import agent_loop as al

        msgs = self._mk_msgs(5)
        original = [dict(m) for m in msgs]
        monkeypatch.setattr(al, "count_messages_tokens", lambda m: 10)  # 恒低于 target
        removed = al._fifo_prune(msgs, 100)
        assert removed == 0
        assert len(msgs) == 12
        assert msgs == original
        assert not any(
            isinstance(m.get("content"), str) and m["content"].startswith("[上下文提示：")
            for m in msgs
        )

    def test_resumed_path_marker_at_protect_end(self, monkeypatch):
        """is_resumed=True：protect_end = len - protect_recent_count，标记恰在该位置，受保护区原样。"""
        from agent.generic import agent_loop as al

        msgs = self._mk_msgs(8, with_initial_user=False)  # [system] + 8 轮 → len=17
        head = [dict(m) for m in msgs[:12]]  # protect_end = max(2, 17-5) = 12
        monkeypatch.setattr(al, "count_messages_tokens", lambda m: 10**9)
        removed = al._fifo_prune(msgs, 100, protect_recent_count=5, is_resumed=True)
        assert removed == 5  # 17 - 12，不含标记自身
        assert msgs[:12] == head  # 受保护区原样
        assert len(msgs) == 13  # 12 + 标记
        marker = msgs[12]
        assert marker["role"] == "user"
        assert marker["content"] == "[上下文提示：更早的 5 条消息已因上下文超限被移除]"
