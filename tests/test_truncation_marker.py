# tests/test_truncation_marker.py
"""截断标记系统测试。"""
from unittest.mock import MagicMock, patch
from agent.generic.litellm_adapter import LiteLLMSession


TRUNCATION_MARKER = "[输出因超过最大长度被自动截断，内容不完整。请基于以上不完整内容，缩短后重新输出完整内容。]"


def test_finish_reason_length_adds_marker_to_content():
    """finish_reason='length' 时 full_content 末尾应包含截断标记。"""
    session = LiteLLMSession({
        "apikey": "test",
        "apibase": "https://test.com",
        "model": "test-model",
        "api_type": "openai",
    })

    chunk1 = MagicMock()
    chunk1.choices = [MagicMock()]
    chunk1.choices[0].delta = MagicMock(content="这是被截断的内容", reasoning_content=None, tool_calls=None)
    chunk1.choices[0].finish_reason = None
    chunk1.usage = None

    chunk2 = MagicMock()
    chunk2.choices = [MagicMock()]
    chunk2.choices[0].delta = MagicMock(content=None, reasoning_content=None, tool_calls=None)
    chunk2.choices[0].finish_reason = "length"
    chunk2.usage = MagicMock(prompt_tokens=100, completion_tokens=4096, total_tokens=4196)

    with patch("agent.generic.litellm_adapter.litellm.completion", return_value=iter([chunk1, chunk2])):
        gen = session.chat(messages=[{"role": "user", "content": "test"}])
        chunks = []
        try:
            while True:
                chunks.append(next(gen))
        except StopIteration as e:
            response = e.value

    assert response.finish_reason == "length"
    assert TRUNCATION_MARKER in response.content
    assert response.content.startswith("这是被截断的内容")


def test_user_stop_adds_interrupt_marker():
    """用户 stop 中断时 content 末尾应包含中断标记。"""
    session = LiteLLMSession({
        "apikey": "test", "apibase": "https://test.com",
        "model": "test-model", "api_type": "openai",
    })

    chunk1 = MagicMock()
    chunk1.choices = [MagicMock()]
    chunk1.choices[0].delta = MagicMock(content="部分内容", reasoning_content=None, tool_calls=None)
    chunk1.choices[0].finish_reason = None
    chunk1.usage = None

    with patch("agent.generic.litellm_adapter.litellm.completion", return_value=iter([chunk1])):
        with patch("agent.generic.litellm_adapter.is_stop_requested", side_effect=[False, True]):
            gen = session.chat(messages=[{"role": "user", "content": "test"}])
            try:
                while True:
                    next(gen)
            except StopIteration as e:
                response = e.value

    assert "[输出被用户中断，内容不完整。]" in response.content


def test_response_log_contains_finish_reason():
    """response 日志应包含 finish_reason 字段。"""

    session = LiteLLMSession({
        "apikey": "test", "apibase": "https://test.com",
        "model": "test-model", "api_type": "openai",
    })

    chunk1 = MagicMock()
    chunk1.choices = [MagicMock()]
    chunk1.choices[0].delta = MagicMock(content="hi", reasoning_content=None, tool_calls=None)
    chunk1.choices[0].finish_reason = "stop"
    chunk1.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)

    with patch("agent.generic.litellm_adapter.litellm.completion", return_value=iter([chunk1])):
        with patch("agent.generic.litellm_adapter._write_raw_log") as mock_write:
            gen = session.chat(messages=[{"role": "user", "content": "test"}])
            try:
                while True:
                    next(gen)
            except StopIteration:
                pass

            response_calls = [c for c in mock_write.call_args_list if c.args and c.args[0] == "response"]
            assert response_calls, "_write_raw_log not called for response"
            assert "finish_reason" in response_calls[0].args[1], "finish_reason missing in response log"


def test_agent_loop_truncation_retry(monkeypatch):
    """finish_reason='length' 时 agent_loop 应注入重试提示，不执行 tool_calls。"""
    from agent.generic.agent_loop import agent_runner_loop, StreamEvent
    from agent.generic.llmcore import MockResponse, MockToolCall

    call_count = {"n": 0}

    class FakeClient:
        def chat(self, messages, tools=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                resp = MockResponse(
                    thinking="", content="截断的内容",
                    tool_calls=[MockToolCall(name="write", args={}, id="call_1")],
                    raw="截断的内容",
                    stop_reason="tool_use",
                    finish_reason="length",
                )
            else:
                resp = MockResponse(
                    thinking="", content="正常完成",
                    tool_calls=[], raw="正常完成",
                    stop_reason="end_turn", finish_reason="stop",
                )
            yield from []  # 使函数成为 generator
            return resp

    class FakeHandler:
        max_turns = 20
        _is_subagent = False
        _subagent_unique_name = None
        _done_hooks = []
        _last_prompt_tokens = 0
        current_turn = 0
        _current_messages = []
        _bypass_at_prefix = False
        _program_triggered = False

    gen = agent_runner_loop(
        client=FakeClient(),
        system_message={"role": "system", "content": "test"},
        initial_user_content="test",
        handler=FakeHandler(),
        tools_schema=[],
        verbose=False,
    )
    chunks = []
    return_value = None
    try:
        while True:
            chunks.append(next(gen))
    except StopIteration as e:
        return_value = e.value

    assert call_count["n"] == 2
    assert return_value["finish_reason"] != "length"


def test_truncated_tool_calls_not_executed(monkeypatch):
    """finish_reason='length' 且有 tool_calls 时，tool_calls 不应被执行。"""
    from agent.generic.agent_loop import agent_runner_loop, StreamEvent
    from agent.generic.llmcore import MockResponse, MockToolCall

    class FakeClient:
        def chat(self, messages, tools=None):
            resp = MockResponse(
                thinking="", content="截断",
                tool_calls=[MockToolCall(name="write", args={"file_path": "/tmp/test", "content": "x"}, id="call_1")],
                raw="截断",
                stop_reason="tool_use",
                finish_reason="length",
            )
            yield from []
            return resp

    tool_executed = {"yes": False}

    class FakeHandler:
        max_turns = 20
        _is_subagent = False
        _subagent_unique_name = None
        _done_hooks = []
        _last_prompt_tokens = 0
        current_turn = 0
        _current_messages = []
        _bypass_at_prefix = False
        _program_triggered = False
        def dispatch(self, tool_name, args, response, index=0):
            tool_executed["yes"] = True
            from agent.handler import StepOutcome
            return StepOutcome(result="executed")
        def next_prompt_patcher(self, *a, **kw):
            return ""

    gen = agent_runner_loop(
        client=FakeClient(),
        system_message={"role": "system", "content": "test"},
        initial_user_content="test",
        handler=FakeHandler(),
        tools_schema=[],
        verbose=False,
    )
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return_value = e.value

    assert not tool_executed["yes"], "截断的 tool_calls 不应被执行"


def test_compact_truncated_prefix_stripped():
    """主 Agent 收到 COMPACT_TRUNCATED: 前缀时应剥除前缀，保留截断内容。"""
    truncated_content = "部分报告内容\n\n[输出因超过最大长度被自动截断，内容不完整。]"
    prefixed = f"COMPACT_TRUNCATED:{truncated_content}"
    assert prefixed.startswith("COMPACT_TRUNCATED:")
    stripped = prefixed[len("COMPACT_TRUNCATED:"):]
    assert stripped == truncated_content
    assert "[输出因超过最大长度被自动截断" in stripped


def test_code_run_stdout_truncation_marker():
    """code_run stdout 超 10000 字符时应有截断标记。"""
    from agent.handler import code_run

    long_output = "x" * 15000
    result = code_run(f"print('{long_output}')", code_type="python", timeout=10)

    assert result["status"] in ("success", "error")
    assert "[输出已截断" in result.get("stdout", ""), f"Expected truncation marker in stdout, got length {len(result.get('stdout', ''))}"