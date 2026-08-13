"""_probe_llm 截断检测测试：finish_reason=length 必须报错（不再静默判通过）。"""
import asyncio
import inspect
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
    """finish_reason=length → 返回 (False, 含'截断'错误消息)。"""
    mock_session_cls.return_value = _mock_session_with("length")
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


def test_probe_max_tokens_is_256():
    """探测 max_tokens 5→256：构造的 llm_config 应含 max_tokens=256（thinking 模型 content 有空间）。"""
    src = inspect.getsource(_probe_llm)
    assert '"max_tokens": 256' in src
    assert '"max_tokens": 5,' not in src  # 带逗号，避免与 256 子串重叠恒挂
