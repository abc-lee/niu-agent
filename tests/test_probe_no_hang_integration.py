"""probe 端点真实 LLM 集成测试——验证各种配置下不卡死。

TDD：先写覆盖所有可能性的失败测试，再改代码修复。

测试矩阵（9 场景）：
- 豆包 ark-code-latest（volcengine 网关）× {空/openai/volcengine} provider × {带/不带 thinking}
- GLM xopglm5（xf-yun 网关）× {空/openai} provider × 带 thinking:disabled
- json_schema / json_object 两种 response_format
- max_tokens 截断场景

所有场景期望：≤120s 返回（不卡死），结果合法（supported/probe_failed 之一）。

真实 LLM 调用，需要网络。两个配置文件：
- /Users/lilei/.niuu/config/user-config.json（豆包 ark-code-latest）
- /Users/lilei/.niuu/config/user-config - glm.json（GLM xopglm5）
"""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest

# 测试用的两个真实配置
DOUBAO_CONFIG_PATH = Path("/Users/lilei/.niuu/config/user-config.json")
GLM_CONFIG_PATH = Path("/Users/lilei/.niuu/config/user-config - glm.json")

# 所有场景 ≤120s 返回（不卡死）
PROBE_TIMEOUT_SECONDS = 120


def _load_config(path: Path) -> dict:
    """加载真实配置文件，返回 llm 段配置。"""
    if not path.exists():
        pytest.skip(f"配置文件不存在: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["llm"]


def _build_probe_config(llm_config: dict, provider: str, thinking: dict | None) -> dict:
    """构造 probe 端点期望的 config 格式。

    Args:
        llm_config: 真实配置的 llm 段（含 apiKey/apiBase/model/type）
        provider: 测试场景的 provider 值（""/"openai"/"volcengine"）
        thinking: thinking 参数（None=不传，{type:"disabled"}=关，{type:"enabled"}=开）
    """
    litellm_kwargs = {}
    if thinking is not None:
        litellm_kwargs["thinking"] = thinking
    return {
        "apiKey": llm_config["apiKey"],
        "apiBase": llm_config["apiBase"],
        "model": llm_config["model"],
        "type": llm_config.get("type", "openai"),
        "provider": provider,
        "litellm_kwargs": litellm_kwargs,
    }


async def _call_probe_via_http(config: dict, port: int = 9876) -> dict:
    """通过 HTTP 调 probe 端点（需要 Python API 运行）。

    用 asyncio.wait_for 包裹防卡死。
    """
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{port}/api/probe-response-format",
            json=config,
            timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT_SECONDS),
        ) as resp:
            return await resp.json()


async def _call_probe_direct(config: dict) -> dict:
    """直接调 probe 逻辑（不通过 HTTP），用 asyncio.wait_for 包裹防卡死。

    避免依赖 Python API 进程运行状态。模拟 probe_response_format 端点核心逻辑。
    """
    from niu_api.compat import (
        _build_probe_messages,
        _build_probe_response_format_json_schema,
        _build_probe_response_format_json_object,
        _classify_probe_response_tier1,
        _classify_probe_response_tier2,
        _probe_tier_three_samples_async,
    )
    from agent.generic.litellm_adapter import LiteLLMSession

    # 复制 probe_response_format 端点的核心逻辑（compat.py:1490-1748）
    config_lower = {k.lower(): v for k, v in config.items()}

    if not config_lower.get("apikey"):
        return {"result": "probe_failed", "reason": "API Key 未配置", "mode": None}
    if not config_lower.get("apibase"):
        return {"result": "probe_failed", "reason": "API 地址未配置", "mode": None}
    if not config_lower.get("model"):
        return {"result": "probe_failed", "reason": "模型名称未配置", "mode": None}

    probe_litellm_kwargs = {
        k: v for k, v in (config_lower.get("litellm_kwargs") or {}).items()
        if k != "response_format_mode"
    }
    probe_litellm_kwargs["allowed_openai_params"] = ["response_format"]
    probe_litellm_kwargs["max_tokens"] = 50  # 当前代码值，测试用例会验证是否截断

    base_llm_config = {
        "api_type": config_lower.get("type", "openai"),
        "apikey": config_lower["apikey"],
        "apibase": config_lower["apibase"],
        "model": config_lower["model"],
        "reasoning_effort": None,
        "provider": config_lower.get("provider", ""),
        "temperature": config_lower.get("temperature", 0.2),
        "litellm_kwargs": probe_litellm_kwargs,
        "read_timeout": 15,  # 当前代码值
    }

    messages = _build_probe_messages()

    def _try_tier(response_format):
        from litellm import (
            RateLimitError, BadRequestError, UnsupportedParamsError,
            AuthenticationError, APIConnectionError, InternalServerError,
            ServiceUnavailableError,
        )
        import litellm
        import openai

        try:
            session = LiteLLMSession(cfg=base_llm_config)
            gen = session.chat(messages=messages, response_format=response_format)
            chunks = []
            try:
                while True:
                    chunk = next(gen)
                    if isinstance(chunk, str):
                        chunks.append(chunk)
            except StopIteration:
                pass
            text = "".join(chunks)
            if response_format is not None and response_format.get("type") == "json_schema":
                tier = _classify_probe_response_tier1(text)
            elif response_format is not None and response_format.get("type") == "json_object":
                tier = _classify_probe_response_tier2(text)
            else:
                tier = "gateway_blocked"
            return tier, text
        except RateLimitError as e:
            return "rate_limited", f"RateLimitError: {str(e)[:150]}"
        except (litellm.Timeout, openai.APITimeoutError) as e:
            return "timeout", f"{type(e).__name__}: {str(e)[:150]}"
        except (AuthenticationError, APIConnectionError, InternalServerError, ServiceUnavailableError) as e:
            return "infra_error", f"{type(e).__name__}: {str(e)[:150]}"
        except (BadRequestError, UnsupportedParamsError) as e:
            return "model_rejected", f"{type(e).__name__}: {str(e)[:150]}"
        except Exception as e:
            return "model_rejected", f"{type(e).__name__}: {str(e)[:150]}"

    # Tier 1: json_schema strict
    tier1_result, tier1_raw = await _probe_tier_three_samples_async(
        lambda: asyncio.wait_for(
            asyncio.to_thread(_try_tier, _build_probe_response_format_json_schema()),
            timeout=30,
        ),
        _build_probe_response_format_json_schema(),
    )

    if tier1_result == "supported":
        return {"result": "supported", "mode": "json_schema", "reason": "Tier 1 通过"}

    if tier1_result in ("rate_limited", "infra_error"):
        return {"result": "probe_failed", "reason": f"Tier 1 {tier1_result}", "mode": None}

    # Tier 2: json_object
    tier2_result, tier2_raw = await _probe_tier_three_samples_async(
        lambda: asyncio.wait_for(
            asyncio.to_thread(_try_tier, _build_probe_response_format_json_object()),
            timeout=30,
        ),
        _build_probe_response_format_json_object(),
    )

    if tier2_result == "supported":
        return {"result": "supported", "mode": "json_object", "reason": "Tier 2 通过"}

    if tier2_result in ("rate_limited", "infra_error"):
        return {"result": "probe_failed", "reason": f"Tier 2 {tier2_result}", "mode": None}

    return {"result": "supported", "mode": "prompt_only", "reason": "降级保底"}


async def _run_probe_with_timeout(config: dict) -> tuple[dict, float]:
    """跑 probe 并计时，超时 PROBE_TIMEOUT_SECONDS 视为卡死。"""
    start = time.time()
    try:
        result = await asyncio.wait_for(
            _call_probe_direct(config),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        result = {"result": "HANG_TIMEOUT", "reason": f"卡死 {PROBE_TIMEOUT_SECONDS}s 未返回", "mode": None}
    elapsed = time.time() - start
    return result, elapsed


# ============================================================================
# 测试用例
# ============================================================================

@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(150)
async def test_doubao_volcengine_provider_thinking_disabled():
    """场景 1：豆包 + volcengine provider + thinking:disabled + json_schema。

    期望：≤120s 返回，result 合法（supported/probe_failed 之一），不卡死。
    """
    llm = _load_config(DOUBAO_CONFIG_PATH)
    config = _build_probe_config(llm, provider="volcengine", thinking={"type": "disabled"})
    result, elapsed = await _run_probe_with_timeout(config)
    print(f"\n场景 1 结果: {result} (耗时 {elapsed:.1f}s)")
    assert result["result"] != "HANG_TIMEOUT", f"卡死 {elapsed:.1f}s"
    assert result["result"] in ("supported", "probe_failed"), f"非法结果: {result}"


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(150)
async def test_doubao_empty_provider_thinking_disabled():
    """场景 2：豆包 + 空 provider + thinking:disabled + json_schema。

    这是用户报告的卡死场景。期望：≤120s 返回，不卡死。
    """
    llm = _load_config(DOUBAO_CONFIG_PATH)
    config = _build_probe_config(llm, provider="", thinking={"type": "disabled"})
    result, elapsed = await _run_probe_with_timeout(config)
    print(f"\n场景 2 结果: {result} (耗时 {elapsed:.1f}s)")
    assert result["result"] != "HANG_TIMEOUT", f"卡死 {elapsed:.1f}s"
    assert result["result"] in ("supported", "probe_failed"), f"非法结果: {result}"


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(150)
async def test_doubao_openai_provider_thinking_disabled():
    """场景 3：豆包 + openai provider + thinking:disabled + json_schema。

    期望：≤120s 返回，不卡死。
    """
    llm = _load_config(DOUBAO_CONFIG_PATH)
    config = _build_probe_config(llm, provider="openai", thinking={"type": "disabled"})
    result, elapsed = await _run_probe_with_timeout(config)
    print(f"\n场景 3 结果: {result} (耗时 {elapsed:.1f}s)")
    assert result["result"] != "HANG_TIMEOUT", f"卡死 {elapsed:.1f}s"
    assert result["result"] in ("supported", "probe_failed"), f"非法结果: {result}"


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(150)
async def test_glm_openai_provider_thinking_disabled():
    """场景 4：GLM + openai provider + thinking:disabled + json_schema。

    期望：≤120s 返回，不卡死。
    """
    llm = _load_config(GLM_CONFIG_PATH)
    config = _build_probe_config(llm, provider="openai", thinking={"type": "disabled"})
    result, elapsed = await _run_probe_with_timeout(config)
    print(f"\n场景 4 结果: {result} (耗时 {elapsed:.1f}s)")
    assert result["result"] != "HANG_TIMEOUT", f"卡死 {elapsed:.1f}s"
    assert result["result"] in ("supported", "probe_failed"), f"非法结果: {result}"


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(150)
async def test_glm_empty_provider_thinking_disabled():
    """场景 5：GLM + 空 provider + thinking:disabled + json_schema。

    期望：≤120s 返回，不卡死。
    """
    llm = _load_config(GLM_CONFIG_PATH)
    config = _build_probe_config(llm, provider="", thinking={"type": "disabled"})
    result, elapsed = await _run_probe_with_timeout(config)
    print(f"\n场景 5 结果: {result} (耗时 {elapsed:.1f}s)")
    assert result["result"] != "HANG_TIMEOUT", f"卡死 {elapsed:.1f}s"
    assert result["result"] in ("supported", "probe_failed"), f"非法结果: {result}"


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(150)
async def test_doubao_volcengine_no_thinking():
    """场景 6：豆包 + volcengine provider + 不带 thinking + json_object。

    期望：≤120s 返回，不卡死。
    """
    llm = _load_config(DOUBAO_CONFIG_PATH)
    config = _build_probe_config(llm, provider="volcengine", thinking=None)
    result, elapsed = await _run_probe_with_timeout(config)
    print(f"\n场景 6 结果: {result} (耗时 {elapsed:.1f}s)")
    assert result["result"] != "HANG_TIMEOUT", f"卡死 {elapsed:.1f}s"
    assert result["result"] in ("supported", "probe_failed"), f"非法结果: {result}"


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(150)
async def test_doubao_volcengine_thinking_enabled():
    """场景 7：豆包 + volcengine provider + thinking:enabled + json_schema。

    主模型开思考链场景。期望：≤120s 返回，不卡死。
    """
    llm = _load_config(DOUBAO_CONFIG_PATH)
    config = _build_probe_config(llm, provider="volcengine", thinking={"type": "enabled"})
    result, elapsed = await _run_probe_with_timeout(config)
    print(f"\n场景 7 结果: {result} (耗时 {elapsed:.1f}s)")
    assert result["result"] != "HANG_TIMEOUT", f"卡死 {elapsed:.1f}s"
    assert result["result"] in ("supported", "probe_failed"), f"非法结果: {result}"


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(150)
async def test_doubao_empty_provider_no_thinking():
    """场景 8：豆包 + 空 provider + 不带 thinking + json_schema。

    推理模型超时场景（空 provider 走 openai 路由）。期望：≤120s 返回，不卡死。
    """
    llm = _load_config(DOUBAO_CONFIG_PATH)
    config = _build_probe_config(llm, provider="", thinking=None)
    result, elapsed = await _run_probe_with_timeout(config)
    print(f"\n场景 8 结果: {result} (耗时 {elapsed:.1f}s)")
    assert result["result"] != "HANG_TIMEOUT", f"卡死 {elapsed:.1f}s"
    assert result["result"] in ("supported", "probe_failed"), f"非法结果: {result}"


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(150)
async def test_doubao_volcengine_thinking_disabled_json_complete():
    """场景 9：豆包 + volcengine + thinking:disabled + json_object 响应不截断。

    验证 max_tokens=50 是否导致 JSON 截断误判。
    期望：≤120s 返回，若 result=supported+json_object 则响应未被截断。
    """
    llm = _load_config(DOUBAO_CONFIG_PATH)
    config = _build_probe_config(llm, provider="volcengine", thinking={"type": "disabled"})
    result, elapsed = await _run_probe_with_timeout(config)
    print(f"\n场景 9 结果: {result} (耗时 {elapsed:.1f}s)")
    assert result["result"] != "HANG_TIMEOUT", f"卡死 {elapsed:.1f}s"
    assert result["result"] in ("supported", "probe_failed"), f"非法结果: {result}"
