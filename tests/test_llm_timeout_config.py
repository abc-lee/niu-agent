"""测试 read_timeout 在 LLM 客户端创建链路的透传。"""
from agent.generic.litellm_adapter import create_litellm_client
from agent.generic.llmcore import ToolClient
from agent.runner import create_client
from niu_api.internal.lightrag_manager import _get_litellm_session


def test_runner_create_client_passes_read_timeout():
    """端到端：runner.create_client 应把 read_timeout 透传到 session（经 ToolClient.backend）。"""
    client = create_client({
        "apikey": "k",
        "apibase": "https://example.com/v1",
        "model": "m",
        "type": "openai",
        "read_timeout": 60,
    })
    assert isinstance(client, ToolClient)
    assert client.backend.read_timeout == 60


def test_create_litellm_client_passes_read_timeout():
    """直接调用 create_litellm_client 也应透传 read_timeout（防御第二层）。"""
    client = create_litellm_client({
        "apikey": "k",
        "apibase": "https://example.com/v1",
        "model": "m",
        "type": "openai",
        "read_timeout": 90,
    })
    assert client.backend.read_timeout == 90


def test_runner_create_client_timeout_default():
    """未配置 read_timeout 时应使用默认值 300。"""
    client = create_client({
        "apikey": "k",
        "apibase": "https://example.com/v1",
        "model": "m",
        "type": "openai",
    })
    assert client.backend.read_timeout == 300


def test_runner_create_client_falsy_read_timeout_falls_back():
    """falsy read_timeout（null/空串）应回退默认 300（防止 int(None) 崩溃）。"""
    client = create_client({
        "apikey": "k",
        "apibase": "https://example.com/v1",
        "model": "m",
        "type": "openai",
        "read_timeout": None,
    })
    assert client.backend.read_timeout == 300


def test_lightrag_session_passes_read_timeout():
    """LightRAG session 应透传 read_timeout。"""
    config = {
        "type": "openai", "apikey": "k", "apibase": "https://example.com/v1",
        "model": "m-lightrag-unique", "reasoning_effort": "none", "provider": "",
        "litellm_kwargs": {}, "temperature": 0.2,
        "read_timeout": 120,
    }
    session = _get_litellm_session(config)
    assert session.read_timeout == 120
