"""测试 compat._probe_llm：真实 LLM 调用探测（test-llm 提取的核心逻辑）。

覆盖：判空分支（API Key/地址/模型）、Ollama 本地豁免、键名归一化、
调用成功、流式 stream_error 空响应、超时、401/404/通用异常分类、默认预算。
注意：LiteLLMSession 是 compat.py 函数内局部导入——patch 目标必须是
源头模块 agent.generic.litellm_adapter.LiteLLMSession（patch compat 模块属性会
AttributeError，模块级无此属性）。
"""
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def base_config():
    return {
        "type": "openai",
        "apikey": "sk-test",
        "apibase": "https://api.example.com/v1",
        "model": "test-model",
        "litellm_kwargs": {"thinking": True},
    }


@pytest.mark.asyncio
async def test_probe_llm_missing_api_key():
    from niu_api.compat import _probe_llm

    success, message = await _probe_llm({"apikey": "", "apibase": "https://api.example.com/v1", "model": "m"})
    assert success is False
    assert message == "API Key 未配置"


@pytest.mark.asyncio
async def test_probe_llm_local_apikey_exempt():
    """Ollama 本地地址豁免 apiKey 判空"""
    from niu_api.compat import _probe_llm

    config = {"apikey": "", "apibase": "http://localhost:11434/v1", "model": "llama3"}
    with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_cls:
        mock_session = MagicMock()
        mock_session.chat.return_value = iter(["hi"])
        mock_session_cls.return_value = mock_session
        success, _ = await _probe_llm(config)
    assert success is True


@pytest.mark.asyncio
async def test_probe_llm_missing_apibase():
    from niu_api.compat import _probe_llm

    success, message = await _probe_llm({"apikey": "sk", "apibase": "", "model": "m"})
    assert success is False
    assert message == "API 地址未配置"


@pytest.mark.asyncio
async def test_probe_llm_missing_model():
    from niu_api.compat import _probe_llm

    success, message = await _probe_llm({"apikey": "sk", "apibase": "https://api.example.com/v1", "model": ""})
    assert success is False
    assert message == "模型名称未配置"


@pytest.mark.asyncio
async def test_probe_llm_normalizes_upper_keys(base_config):
    """入口键名归一化：settings 表单原始大写键（apiKey/apiBase）也能正确判空与调用"""
    from niu_api.compat import _probe_llm

    upper_config = {
        "apiKey": "sk-test",
        "apiBase": "https://api.example.com/v1",
        "model": "test-model",
    }
    with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_cls:
        mock_session = MagicMock()
        mock_session.chat.return_value = iter(["hi"])
        mock_session_cls.return_value = mock_session
        success, message = await _probe_llm(upper_config)
    assert success is True
    assert "test-model" in message


@pytest.mark.asyncio
async def test_probe_llm_success(base_config):
    from niu_api.compat import _probe_llm

    with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_cls:
        mock_session = MagicMock()
        # iter 耗尽时 StopIteration 无 value → mock_resp=None → text 非空即 has_content
        mock_session.chat.return_value = iter(["hi"])
        mock_session_cls.return_value = mock_session
        success, message = await _probe_llm(base_config)
    assert success is True
    assert "模型测试通过" in message
    assert "test-model" in message


@pytest.mark.asyncio
async def test_probe_llm_stream_error_empty(base_config):
    """stream_error=True → 模型返回空响应"""
    from niu_api.compat import _probe_llm

    class _MockResp:
        stream_error = True
        content = "partial"
        thinking = None

    class _GenWithValue:
        """next() 抛带 value 的 StopIteration（模拟生成器 StopIteration(e) 路径；
        生成器内不能手动 raise StopIteration（PEP 479），故用普通类实现）"""

        def __iter__(self):
            return self

        def __next__(self):
            raise StopIteration(_MockResp())

    with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_cls:
        mock_session = MagicMock()
        mock_session.chat.return_value = _GenWithValue()
        mock_session_cls.return_value = mock_session
        success, message = await _probe_llm(base_config)
    assert success is False
    assert message == "模型返回空响应"


@pytest.mark.asyncio
async def test_probe_llm_timeout(base_config):
    from niu_api.compat import _probe_llm

    with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_cls:
        mock_session = MagicMock()
        # side_effect=异常实例 → 调用时直接抛（模拟 chat 调用抛超时）
        mock_session.chat.side_effect = TimeoutError("timeout")
        mock_session_cls.return_value = mock_session
        success, message = await _probe_llm(base_config)
    assert success is False
    assert message == "连接超时，请检查网络和 API 地址"


@pytest.mark.asyncio
async def test_probe_llm_unauthorized(base_config):
    from niu_api.compat import _probe_llm

    with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_cls:
        mock_session = MagicMock()
        mock_session.chat.side_effect = RuntimeError("AuthenticationError: 401 Unauthorized")
        mock_session_cls.return_value = mock_session
        success, message = await _probe_llm(base_config)
    assert success is False
    assert message == "API Key 无效或未授权"


@pytest.mark.asyncio
async def test_probe_llm_not_found(base_config):
    from niu_api.compat import _probe_llm

    with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_cls:
        mock_session = MagicMock()
        mock_session.chat.side_effect = RuntimeError("NotFoundError: 404 model not found")
        mock_session_cls.return_value = mock_session
        success, message = await _probe_llm(base_config)
    assert success is False
    assert message == "模型或 API 端点不存在，请检查模型名称和地址"


@pytest.mark.asyncio
async def test_probe_llm_generic_error_masks_key(base_config):
    """通用异常消息脱敏（key=***）"""
    from niu_api.compat import _probe_llm

    with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_cls:
        mock_session = MagicMock()
        mock_session.chat.side_effect = RuntimeError("Connection error to url with key=sk-abc123&other=1")
        mock_session_cls.return_value = mock_session
        success, message = await _probe_llm(base_config)
    assert success is False
    assert "key=***" in message
    assert "sk-abc123" not in message


@pytest.mark.asyncio
async def test_probe_llm_default_budget_pins_spec(base_config):
    """默认预算钉住计划规范值 120/150（防字面量漂移重演 R1 分歧）。
    与 llm_ready.py resolve_probe_budget 对称断言（Task 2 钉 helper 侧——
    两侧各自独立钉规范值，任一侧改动即失败提示同步）。"""
    import inspect

    from niu_api.compat import _probe_llm

    sig = inspect.signature(_probe_llm)
    assert sig.parameters["read_timeout"].default == 120.0
    assert sig.parameters["wait_timeout"].default == 150.0


@pytest.mark.asyncio
async def test_probe_llm_respects_short_timeout(base_config):
    """wait_timeout 参数生效：hang 场景被 wait_for 截断（短预算可测）"""
    from niu_api.compat import _probe_llm

    with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_cls:
        mock_session = MagicMock()

        def _hang(*args, **kwargs):
            # 计划原文 `def _hang():` 无参数——session.chat 以 messages= 关键字调用，
            # 无参签名会立即 TypeError 而非挂起 3s（计划缺陷，最小修正以达成计划
            # 自身预期：wait_for 截断 hang 场景，实测 ~1s）
            import time
            time.sleep(3)

        mock_session.chat.side_effect = _hang
        mock_session_cls.return_value = mock_session
        success, message = await _probe_llm(base_config, read_timeout=1.0, wait_timeout=1.0)
    assert success is False
    assert "超时" in message
