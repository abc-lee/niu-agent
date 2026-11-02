"""测试 llm_ready._probe_connectivity：启动期连通性探测（与设置页 _probe_llm 判定分离）。

判定口径：发 hi，拿到任何非空输出（正文块或思考）即通过——不看 finish_reason、
不传 max_tokens、不透传能力参数。覆盖：正文/思考/无输出、参数最小化、
partial stream_error、超时、异常脱敏、配置判空、本地空 key、构造异常、
litellm_kwargs 非 dict。
注意：LiteLLMSession 是 _probe_connectivity_impl 函数内局部导入——patch 目标必须是
源头模块 agent.generic.litellm_adapter.LiteLLMSession。
返回值统一用 types.SimpleNamespace（MockResponse 四参构造非必要）。
"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

BASE_CONFIG = {
    "type": "openai",
    "apikey": "sk-test",
    "apibase": "https://api.example.com/v1",
    "model": "test-model",
}


def _gen(chunks, resp):
    """生成器：产出 chunks 后 return resp（消费侧见 StopIteration.value）。"""
    yield from chunks
    return resp


def _session_returning(chunks, resp):
    """构造 LiteLLMSession 替身：chat() 返回 _gen(chunks, resp)。"""
    session = MagicMock()
    session.chat.return_value = _gen(chunks, resp)
    return session


@pytest.mark.asyncio
async def test_body_chunk_passes():
    """首个非空正文块即通过（不再消费后续流）"""
    from niu_api.llm_ready import _probe_connectivity

    with patch("agent.generic.litellm_adapter.LiteLLMSession") as cls:
        cls.return_value = _session_returning(["Hi!"], SimpleNamespace(content="Hi!", thinking=""))
        success, message = await _probe_connectivity(BASE_CONFIG)
    assert success is True
    assert "正文" in message


@pytest.mark.asyncio
async def test_thinking_only_passes():
    """无正文块但 StopIteration.value.thinking 非空 → 通过"""
    from niu_api.llm_ready import _probe_connectivity

    with patch("agent.generic.litellm_adapter.LiteLLMSession") as cls:
        cls.return_value = _session_returning(["", "   "], SimpleNamespace(content="", thinking="嗯，让我想想"))
        success, message = await _probe_connectivity(BASE_CONFIG)
    assert success is True
    assert "思考" in message


@pytest.mark.asyncio
async def test_no_output_fails():
    """2026-11-01 事故形态：正文与思考均空（finish_reason=length）→ 判失败"""
    from niu_api.llm_ready import _probe_connectivity

    resp = SimpleNamespace(content="", thinking="", finish_reason="length")
    with patch("agent.generic.litellm_adapter.LiteLLMSession") as cls:
        cls.return_value = _session_returning([], resp)
        success, message = await _probe_connectivity(BASE_CONFIG)
    assert success is False
    assert "没有任何输出" in message


@pytest.mark.asyncio
async def test_request_params_minimal():
    """传给 LiteLLMSession 的 cfg 只含连通所需键 + sticky 会话头；能力键一律不带"""
    from niu_api.llm_ready import _probe_connectivity

    config = {
        **BASE_CONFIG,
        "proxy": "http://127.0.0.1:8888",
        "max_tokens": 32768,
        "capabilities": ["response_format"],
        "reasoning_effort": "high",
        "thinking": {"type": "enabled"},
        "temperature": 0.7,
        "litellm_kwargs": {"sticky_session_headers": {"x-session": "abc"}},
    }
    with patch("agent.generic.litellm_adapter.LiteLLMSession") as cls:
        cls.return_value = _session_returning(["hi"], SimpleNamespace(content="hi", thinking=""))
        success, _ = await _probe_connectivity(config)
    assert success is True
    cfg = cls.call_args.kwargs["cfg"]
    for banned in ("max_tokens", "capabilities", "reasoning_effort", "thinking", "temperature"):
        assert banned not in cfg
    assert cfg["sticky_session_id"] == "probe-test-llm"
    assert cfg["proxy"] == "http://127.0.0.1:8888"
    assert cfg["litellm_kwargs"] == {"sticky_session_headers": {"x-session": "abc"}}
    assert cfg["model"] == "test-model"
    assert cfg["api_type"] == "openai"


@pytest.mark.asyncio
async def test_partial_stream_error_passes():
    """流中途 stream_error 但已有 partial 输出 → 判通过（有回话即过）"""
    from niu_api.llm_ready import _probe_connectivity

    resp = SimpleNamespace(content="partial", thinking="", stream_error=True)
    with patch("agent.generic.litellm_adapter.LiteLLMSession") as cls:
        cls.return_value = _session_returning(["partial"], resp)
        success, _ = await _probe_connectivity(BASE_CONFIG)
    assert success is True


@pytest.mark.asyncio
async def test_timeout_returns_failure():
    """chat 阻塞超过 wait_timeout → 超时失败（不外抛）"""
    from niu_api.llm_ready import _probe_connectivity

    def _slow_chat(**_kwargs):
        time.sleep(0.3)
        return iter(["too late"])

    session = MagicMock()
    session.chat.side_effect = _slow_chat
    with patch("agent.generic.litellm_adapter.LiteLLMSession") as cls:
        cls.return_value = session
        success, message = await _probe_connectivity(BASE_CONFIG, wait_timeout=0.05)
    assert success is False
    assert "超时" in message


@pytest.mark.asyncio
async def test_error_sanitized():
    """provider 抛 401 → 固定文案，不泄漏原始响应"""
    from niu_api.llm_ready import _probe_connectivity

    with patch("agent.generic.litellm_adapter.LiteLLMSession") as cls:
        session = MagicMock()
        session.chat.side_effect = Exception("401 Unauthorized")
        cls.return_value = session
        success, message = await _probe_connectivity(BASE_CONFIG)
    assert success is False
    assert message == "API Key 无效或未授权"


@pytest.mark.asyncio
async def test_missing_model_config():
    """get_llm_config 返回 {} → 判失败，且绝不构造 LiteLLMSession"""
    from niu_api.llm_ready import check_llm_ready

    with patch("niu_api.llm_proxy.get_llm_config", return_value={}), patch(
        "agent.generic.litellm_adapter.LiteLLMSession"
    ) as cls:
        ready, message = await check_llm_ready()
    assert ready is False
    assert message
    cls.assert_not_called()


@pytest.mark.asyncio
async def test_local_model_empty_apikey_passes():
    """本地地址 + 空 apikey 豁免判空；cfg 必须带 apikey=""（丢键会让 BaseSession KeyError）"""
    from niu_api.llm_ready import _probe_connectivity

    config = {"apikey": "", "apibase": "http://127.0.0.1:1", "model": "m", "type": "openai"}
    with patch("agent.generic.litellm_adapter.LiteLLMSession") as cls:
        cls.return_value = _session_returning(["pong"], SimpleNamespace(content="pong", thinking=""))
        success, _ = await _probe_connectivity(config)
    assert success is True
    cfg = cls.call_args.kwargs["cfg"]
    assert cfg["apikey"] == ""
    assert "apikey" in cfg


@pytest.mark.asyncio
async def test_session_ctor_exception_returns_failure():
    """LiteLLMSession 构造抛异常（cfg 缺键）→ 返回 (False, ...)，绝不逃出 lifespan"""
    from niu_api.llm_ready import _probe_connectivity

    with patch("agent.generic.litellm_adapter.LiteLLMSession") as cls:
        cls.side_effect = KeyError("apikey")
        success, message = await _probe_connectivity(BASE_CONFIG)
    assert success is False
    assert isinstance(message, str) and message


@pytest.mark.asyncio
async def test_non_dict_litellm_kwargs_returns_failure():
    """litellm_kwargs 被手编成字符串/列表 → 不抛 AttributeError，返回 (bool, str)"""
    from niu_api.llm_ready import _probe_connectivity

    for bad in ("auto", ["response_format"], 7, None):
        config = {**BASE_CONFIG, "litellm_kwargs": bad}
        with patch("agent.generic.litellm_adapter.LiteLLMSession") as cls:
            cls.return_value = _session_returning(["ok"], SimpleNamespace(content="ok", thinking=""))
            result = await _probe_connectivity(config)
        assert isinstance(result, tuple) and len(result) == 2
        assert isinstance(result[0], bool) and isinstance(result[1], str)
        assert result[0] is True
        assert cls.call_args.kwargs["cfg"]["litellm_kwargs"] == {}
