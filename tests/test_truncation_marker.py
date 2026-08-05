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
