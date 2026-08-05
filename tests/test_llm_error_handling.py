"""LLM 错误处理机制测试。"""
from agent.generic.llmcore import MockResponse


def test_mock_response_stream_error_defaults():
    """MockResponse 新增 stream_error/error_type/error_msg 字段，默认值为 False/None/None。"""
    resp = MockResponse(
        thinking="", content="hello", tool_calls=[], raw="hello"
    )
    assert resp.stream_error is False
    assert resp.error_type is None
    assert resp.error_msg is None


def test_mock_response_stream_error_set():
    """MockResponse 可设置 stream_error=True + error_type + error_msg。"""
    resp = MockResponse(
        thinking="", content="", tool_calls=[], raw="",
        stream_error=True, error_type="fatal",
        error_msg="AuthenticationError: invalid key"
    )
    assert resp.stream_error is True
    assert resp.error_type == "fatal"
    assert resp.error_msg == "AuthenticationError: invalid key"



from unittest.mock import MagicMock
import litellm


def test_classify_retryable_error():
    """APIConnectionError 归入 retryable。"""
    from agent.generic.litellm_adapter import _classify_stream_error
    e = litellm.APIConnectionError(message="conn error", model="test", llm_provider="test")
    assert _classify_stream_error(e) == "retryable"


def test_classify_fatal_error():
    """AuthenticationError 归入 fatal。"""
    from agent.generic.litellm_adapter import _classify_stream_error
    e = litellm.AuthenticationError(message="bad key", model="test", llm_provider="test")
    assert _classify_stream_error(e) == "fatal"


def test_classify_uncertain_error():
    """InternalServerError 归入 uncertain。"""
    from agent.generic.litellm_adapter import _classify_stream_error
    e = litellm.InternalServerError(message="server error", model="test", llm_provider="test")
    assert _classify_stream_error(e) == "uncertain"


def test_classify_midstream_fallback_string_match():
    """MidStreamFallbackError 字符串匹配归入 retryable（即使不是 litellm 标准异常）。"""
    from agent.generic.litellm_adapter import _classify_stream_error
    # 构造一个名字含 MidStreamFallback 的异常
    class MidStreamFallbackError(Exception):
        pass
    e = MidStreamFallbackError("burst protection")
    assert _classify_stream_error(e) == "retryable"


def test_classify_unknown_error_defaults_retryable():
    """未知异常默认归入 retryable。"""
    from agent.generic.litellm_adapter import _classify_stream_error
    e = RuntimeError("unknown error")
    assert _classify_stream_error(e) == "retryable"