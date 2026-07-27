"""边界场景集成测试——补充 9 场景矩阵未覆盖的路径。

测试目标：
1. Anthropic 路由（api.anthropic.com → anthropic/ 前缀）—— 无真实 key，测推导逻辑
2. 本地 Ollama（localhost → openai/ 前缀 + 空 apiKey 豁免）
3. 自定义网关（my-gateway → openai/ 前缀）
4. test-llm 端点边界（空 apiKey 非本地 / 错误 apiBase / 无效 model）
5. 运行时 lightrag session.chat 调用（验证 provider 前缀推导在运行时生效）

所有场景 ≤120s 返回（不卡死）。
"""
import asyncio
import json
import signal
import sys
import time
from pathlib import Path

import pytest

PROBE_TIMEOUT_SECONDS = 120

DOUBAO_CONFIG_PATH = Path("/Users/lilei/.niuu/config/user-config.json")
GLM_CONFIG_PATH = Path("/Users/lilei/.niuu/config/user-config - glm.json")


def _load_config(path: Path) -> dict:
    if not path.exists():
        pytest.skip(f"配置文件不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))["llm"]


# ============================================================================
# 1. 推导函数边界场景（不调 LLM，纯逻辑）
# ============================================================================

def test_anthropic_api_base_derives_anthropic_prefix():
    """api.anthropic.com → anthropic/ 前缀。"""
    from agent.generic.litellm_adapter import _derive_provider_prefix
    assert _derive_provider_prefix("https://api.anthropic.com/v1", "claude-3-5-sonnet") == "anthropic/claude-3-5-sonnet"


def test_anthropic_api_base_with_region():
    """api.anthropic.com 含区域路径 → anthropic/ 前缀。"""
    from agent.generic.litellm_adapter import _derive_provider_prefix
    assert _derive_provider_prefix("https://api.anthropic.com/v1/messages", "claude-3-opus") == "anthropic/claude-3-opus"


def test_ollama_localhost_derives_openai_prefix():
    """localhost Ollama → openai/ 前缀（OpenAI 兼容路由）。"""
    from agent.generic.litellm_adapter import _derive_provider_prefix
    assert _derive_provider_prefix("http://localhost:11434/v1", "llama3") == "openai/llama3"


def test_127_localhost_derives_openai_prefix():
    """127.0.0.1 → openai/ 前缀。"""
    from agent.generic.litellm_adapter import _derive_provider_prefix
    assert _derive_provider_prefix("http://127.0.0.1:11434/v1", "llama3") == "openai/llama3"


def test_custom_gateway_derives_openai_prefix():
    """自定义网关 → openai/ 前缀（默认 OpenAI 兼容）。"""
    from agent.generic.litellm_adapter import _derive_provider_prefix
    assert _derive_provider_prefix("https://my-gateway.example.com/v1", "gpt-4") == "openai/gpt-4"


def test_one_api_gateway_derives_openai_prefix():
    """one-api/new-api 网关 → openai/ 前缀。"""
    from agent.generic.litellm_adapter import _derive_provider_prefix
    assert _derive_provider_prefix("https://one-api.example.com/v1", "gpt-4") == "openai/gpt-4"


# ============================================================================
# 2. test-llm 端点边界场景（通过 HTTP，需要 Python API 运行）
# ============================================================================

async def _call_test_llm(config: dict, port: int = 9876) -> dict:
    """通过 HTTP 调 test-llm 端点。"""
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{port}/api/test-llm",
            json=config,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            return await resp.json()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(60)
async def test_test_llm_empty_apikey_non_local_returns_error():
    """空 apiKey + 非本地 apiBase → 返回 'API Key 未配置' 错误（不卡死）。"""
    import aiohttp
    config = {
        "apiKey": "",
        "apiBase": "https://ark.cn-beijing.volces.com/api/coding/v3",
        "model": "ark-code-latest",
        "type": "openai",
        "provider": "",
        "litellm_kwargs": {},
    }
    try:
        result = await asyncio.wait_for(_call_test_llm(config), timeout=30)
        assert result.get("success") is False, f"空 apiKey 非本地应该失败: {result}"
        assert "API Key" in result.get("error", "") or "Key" in result.get("error", ""), f"错误消息应含 API Key: {result}"
    except aiohttp.ClientConnectorError:
        pytest.skip("Python API 未运行（跳过 HTTP 端点测试）")
    except asyncio.TimeoutError:
        pytest.fail("test-llm 卡死 30s 未返回")


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(60)
async def test_test_llm_invalid_apibase_returns_error():
    """错误 apiBase → 返回连接错误（不卡死）。"""
    config = {
        "apiKey": "test-invalid-key",
        "apiBase": "https://invalid.example.com/v1",
        "model": "gpt-4",
        "type": "openai",
        "provider": "",
        "litellm_kwargs": {},
    }
    import aiohttp
    try:
        result = await asyncio.wait_for(_call_test_llm(config), timeout=30)
        assert result.get("success") is False, f"错误 apiBase 应该失败: {result}"
    except aiohttp.ClientConnectorError:
        pytest.skip("Python API 未运行")
    except asyncio.TimeoutError:
        pytest.fail("test-llm 卡死 30s 未返回")


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(60)
async def test_test_llm_empty_model_returns_error():
    """空 model → 返回 '模型名称未配置' 错误（不卡死）。"""
    config = {
        "apiKey": "test-key",
        "apiBase": "https://api.openai.com/v1",
        "model": "",
        "type": "openai",
        "provider": "",
        "litellm_kwargs": {},
    }
    import aiohttp
    try:
        result = await asyncio.wait_for(_call_test_llm(config), timeout=30)
        assert result.get("success") is False, f"空 model 应该失败: {result}"
        assert "模型" in result.get("error", "") or "model" in result.get("error", "").lower(), f"错误消息应含 model: {result}"
    except aiohttp.ClientConnectorError:
        pytest.skip("Python API 未运行")
    except asyncio.TimeoutError:
        pytest.fail("test-llm 卡死 30s 未返回")


# ============================================================================
# 3. 运行时 lightrag session.chat 调用（验证 provider 前缀推导在运行时生效）
# ============================================================================

@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(60)
async def test_runtime_lightrag_session_doubao_returns_pure_json():
    """运行时 lightrag session.chat + thinking:disabled + json_object → 返回纯 JSON（无 thinking 块）。

    验证：知识图谱运行时调用链路完整打通（provider 前缀推导 + thinking:disabled 生效）。
    """
    from niu_api.internal.lightrag_manager import _get_litellm_session

    llm_config = _load_config(DOUBAO_CONFIG_PATH)
    config = {
        "apikey": llm_config["apiKey"],
        "apibase": llm_config["apiBase"],
        "model": llm_config["model"],
        "type": "openai",
        "provider": "",
        "reasoning_effort": "high",
        "temperature": 0.2,
        "litellm_kwargs": {
            "thinking": {"type": "disabled"},
            "allowed_openai_params": [],
            "response_format_mode": "prompt_only",
        },
    }

    def handler(signum, frame):
        pytest.fail("运行时 session.chat 卡死 30s")
    signal.signal(signal.SIGALRM, handler)
    signal.alarm(30)

    try:
        session = _get_litellm_session(config)
        gen = session.chat(
            messages=[{"role": "user", "content": 'Return JSON: {"verdict": "ok"}'}],
            response_format={"type": "json_object"},
        )
        chunks = []
        while True:
            try:
                chunk = next(gen)
                if isinstance(chunk, str):
                    chunks.append(chunk)
            except StopIteration:
                break
        text = "".join(chunks)
        # 验证返回纯 JSON（不含 thinking 块）
        try:
            data = json.loads(text.strip())
            assert isinstance(data, dict), f"应返回 JSON dict: {text[:200]}"
            assert data.get("verdict") == "ok", f"verdict 应为 ok: {data}"
        except json.JSONDecodeError:
            pytest.fail(f"返回非合法 JSON（可能含 thinking 块）: {text[:200]}")
    finally:
        signal.alarm(0)


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(60)
async def test_runtime_lightrag_session_glm_returns_pure_json():
    """运行时 lightrag session.chat + GLM + thinking:disabled + json_object → 返回纯 JSON。"""
    from niu_api.internal.lightrag_manager import _get_litellm_session

    llm_config = _load_config(GLM_CONFIG_PATH)
    config = {
        "apikey": llm_config["apiKey"],
        "apibase": llm_config["apiBase"],
        "model": llm_config["model"],
        "type": "openai",
        "provider": "",
        "reasoning_effort": "high",
        "temperature": 0.2,
        "litellm_kwargs": {
            "thinking": {"type": "disabled"},
            "allowed_openai_params": [],
            "response_format_mode": "prompt_only",
        },
    }

    def handler(signum, frame):
        pytest.fail("运行时 session.chat 卡死 30s")
    signal.signal(signal.SIGALRM, handler)
    signal.alarm(30)

    try:
        session = _get_litellm_session(config)
        gen = session.chat(
            messages=[{"role": "user", "content": 'Return JSON: {"verdict": "ok"}'}],
            response_format={"type": "json_object"},
        )
        chunks = []
        while True:
            try:
                chunk = next(gen)
                if isinstance(chunk, str):
                    chunks.append(chunk)
            except StopIteration:
                break
        text = "".join(chunks)
        try:
            data = json.loads(text.strip())
            assert isinstance(data, dict), f"应返回 JSON dict: {text[:200]}"
        except json.JSONDecodeError:
            pytest.fail(f"返回非合法 JSON: {text[:200]}")
    finally:
        signal.alarm(0)
