"""_probe_llm 截断检测测试：finish_reason=length 必须报错（不再静默判通过）。"""
import asyncio
from unittest.mock import MagicMock, patch

from niu_api.compat import _probe_llm  # noqa: E402


class _MockGen:
    """普通类迭代器：__next__ 里 raise StopIteration(value) 合法（PEP 479 只禁生成器函数内手动 raise）。

    参照 tests/test_llm_probe.py 既有记录：生成器内手动 raise StopIteration 在 Python 3.7+
    变成 RuntimeError——必须用普通类实现。next(gen) 手动迭代时 StopIteration.value 可取。
    """

    def __init__(self, resp):
        self._resp = resp
        self._done = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._done:
            self._done = True
            return ""
        raise StopIteration(self._resp)


def _mock_session_with(finish_reason: str, content: str = "Hi"):
    """构造 session.chat 返回普通类迭代器（先 yield ""，再 StopIteration(MockResponse)）。"""
    mock_resp = MagicMock()
    mock_resp.stream_error = False
    mock_resp.finish_reason = finish_reason
    mock_resp.content = content
    mock_resp.thinking = ""

    session = MagicMock()
    session.chat.return_value = _MockGen(mock_resp)
    return session


@patch("agent.generic.litellm_adapter.LiteLLMSession")
def test_truncated_probe_reports_error(mock_session_cls):
    """finish_reason=length + 无 content/thinking（真截断）→ 返回 (False, 含'截断'错误消息)。"""
    mock_session_cls.return_value = _mock_session_with("length", content="")
    ok, msg = asyncio.run(_probe_llm({
        "apiKey": "k", "apiBase": "https://api.example.com",
        "model": "m", "type": "openai",
    }))
    assert ok is False
    assert "截断" in msg


@patch("agent.generic.litellm_adapter.LiteLLMSession")
def test_normal_probe_still_passes(mock_session_cls):
    """finish_reason=stop → 正常通过（回归）。"""
    mock_session_cls.return_value = _mock_session_with("stop", content="Hi～😊")
    ok, msg = asyncio.run(_probe_llm({
        "apiKey": "k", "apiBase": "https://api.example.com",
        "model": "m", "type": "openai",
    }))
    assert ok is True
    assert "通过" in msg


@patch("agent.generic.litellm_adapter.LiteLLMSession")
def test_probe_max_tokens_default(mock_session_cls):
    """探测 max_tokens 默认 256（探测提速；256 对 thinking 模型截断但有 thinking 输出即判通过）。"""
    mock_session_cls.return_value = _mock_session_with("stop")
    asyncio.run(_probe_llm({
        "apiKey": "k", "apiBase": "https://api.example.com",
        "model": "m", "type": "openai",
    }))
    cfg = mock_session_cls.call_args.kwargs["cfg"]
    assert cfg["litellm_kwargs"]["max_tokens"] == 256


@patch("agent.generic.litellm_adapter.LiteLLMSession")
def test_probe_max_tokens_user_config_wins(mock_session_cls):
    """用户配置 max_tokens 时探测用用户值（testAndSave 顺带校验合法性）；无配置保持 256。"""
    mock_session_cls.return_value = _mock_session_with("stop")
    asyncio.run(_probe_llm({
        "apiKey": "k", "apiBase": "https://api.example.com",
        "model": "m", "type": "openai",
        "max_tokens": 8192,
    }))
    cfg = mock_session_cls.call_args.kwargs["cfg"]
    assert cfg["litellm_kwargs"]["max_tokens"] == 8192


@patch("agent.generic.litellm_adapter.LiteLLMSession")
def test_thinking_truncated_still_passes(mock_session_cls):
    """thinking 模型 length 截断但有思考输出 → 判通过（2026-09-06 glm-5.3-flash 启动失败实证：
    探测 max_tokens=256 只够思考链开头，服务端强制思考关不掉，模型能回 hi 却因截断被误判不可用）。"""
    mock_resp = MagicMock()
    mock_resp.stream_error = False
    mock_resp.finish_reason = "length"
    mock_resp.content = ""
    mock_resp.thinking = "Let me consider how to respond to this greeting effectively..."
    session = MagicMock()
    session.chat.return_value = _MockGen(mock_resp)
    mock_session_cls.return_value = session
    ok, msg = asyncio.run(_probe_llm({
        "apiKey": "k", "apiBase": "https://api.example.com",
        "model": "m", "type": "openai",
    }))
    assert ok is True
    assert "通过" in msg


@patch("agent.generic.litellm_adapter.LiteLLMSession")
def test_true_truncation_still_reports_error(mock_session_cls):
    """真截断（length + 无 thinking 无 content，模型没反应）→ 仍报错（d444b098 语义保留）。"""
    mock_session_cls.return_value = _mock_session_with("length", content="")
    ok, msg = asyncio.run(_probe_llm({
        "apiKey": "k", "apiBase": "https://api.example.com",
        "model": "m", "type": "openai",
    }))
    assert ok is False
    assert "截断" in msg
