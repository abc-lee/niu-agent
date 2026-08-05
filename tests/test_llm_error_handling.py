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
from types import SimpleNamespace
from agent.generic.litellm_adapter import LiteLLMSession
from unittest.mock import patch


def _make_chunk(content=None, finish_reason=None):
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=None,
    )


def test_do_streaming_completion_consumes_chunks():
    """_do_streaming_completion 消费流式 response，yield delta.content，return tuple。"""
    cfg = {"apikey": "test", "apibase": "http://test", "model": "test-model", "read_timeout": 30}
    session = LiteLLMSession(cfg)

    fake_chunks = [_make_chunk(content="hello"), _make_chunk(content=" world"), _make_chunk(finish_reason="stop")]
    response = iter(fake_chunks)

    with patch("agent.generic.litellm_adapter.is_stop_requested", return_value=False):
        gen = session._do_streaming_completion(response)
        chunks = []
        result = None
        try:
            while True:
                chunk = next(gen)
                if isinstance(chunk, str):
                    chunks.append(chunk)
        except StopIteration as e:
            result = e.value

    assert "".join(chunks) == "hello world"
    assert result is not None
    content, thinking, tool_calls, finish_reason, usage, was_stopped = result
    assert content == "hello world"
    assert finish_reason == "stop"
    assert was_stopped is False
    assert tool_calls == []


def test_stream_error_retry_succeeds():
    """流式错误后重试成功 → stream_error=False，content 为重试内容。"""
    cfg = {"apikey": "test", "apibase": "http://test", "model": "test-model", "read_timeout": 30}
    session = LiteLLMSession(cfg)

    # 第一次流式抛 APIConnectionError，第二次返回完整内容
    good_chunks = [_make_chunk(content="retried"), _make_chunk(finish_reason="stop")]
    call_count = {"n": 0}

    def mock_completion(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            def gen():
                yield _make_chunk(content="partial")
                raise litellm.APIConnectionError(message="burst protection", model="test", llm_provider="test")
            return gen()
        return iter(good_chunks)

    with patch("litellm.completion", side_effect=mock_completion), \
         patch("agent.generic.litellm_adapter.is_stop_requested", return_value=False):
        gen = session.chat(messages=[{"role": "user", "content": "test"}], tools=None)
        result = None
        try:
            while True:
                next(gen)
        except StopIteration as e:
            result = e.value

    assert result is not None
    assert result.stream_error is False
    assert result.content == "retried"
    assert call_count["n"] == 2


def test_stream_error_retry_exhausted():
    """流式错误重试 3 次都失败 → stream_error=True, error_type='retry_exhausted'。"""
    cfg = {"apikey": "test", "apibase": "http://test", "model": "test-model", "read_timeout": 30}
    session = LiteLLMSession(cfg)

    def mock_completion(**kwargs):
        def gen():
            yield _make_chunk(content="partial")
            raise litellm.APIConnectionError(message="burst protection", model="test", llm_provider="test")
        return gen()

    with patch("litellm.completion", side_effect=mock_completion), \
         patch("agent.generic.litellm_adapter.is_stop_requested", return_value=False):
        gen = session.chat(messages=[{"role": "user", "content": "test"}], tools=None)
        result = None
        try:
            while True:
                next(gen)
        except StopIteration as e:
            result = e.value

    assert result.stream_error is True
    assert result.error_type == "retry_exhausted"
    assert result.content == ""


def test_stream_error_fatal_no_retry():
    """不可重试错误（AuthenticationError）→ 不重试，stream_error=True, error_type='fatal'。"""
    cfg = {"apikey": "test", "apibase": "http://test", "model": "test-model", "read_timeout": 30}
    session = LiteLLMSession(cfg)

    call_count = {"n": 0}
    def mock_completion(**kwargs):
        call_count["n"] += 1
        def gen():
            yield _make_chunk(content="partial")
            raise litellm.AuthenticationError(message="bad key", model="test", llm_provider="test")
        return gen()

    with patch("litellm.completion", side_effect=mock_completion), \
         patch("agent.generic.litellm_adapter.is_stop_requested", return_value=False):
        gen = session.chat(messages=[{"role": "user", "content": "test"}], tools=None)
        result = None
        try:
            while True:
                next(gen)
        except StopIteration as e:
            result = e.value

    assert result.stream_error is True
    assert result.error_type == "fatal"
    assert call_count["n"] == 1
    assert result.content == ""