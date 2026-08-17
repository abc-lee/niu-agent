"""探测与生产参数一致性（组件 3——一套参数）。

- build_base_params 防漂移断言（R7）：探测形态基础字段集合 ⊆ 生产形态基础字段集合
  ——Task 1 落地后即绿。
- 三处探测一致性断言（_probe_llm / probe-response-format 用 assemble_request_params
  后 reasoning_effort 从配置透传）：**预期红相**——compat.py 改造在 Task 3，
  本 Task 仅验 build_base_params 部分，Task 3 转绿。
  llm_ready.check_llm_ready 复用 _probe_llm（llm_ready.py L100）→ 被
  test_probe_llm_passes_reasoning_effort_from_config 传递覆盖。
"""

import asyncio
from unittest.mock import patch


# ===== build_base_params 防漂移（Task 1 绿） =====


def test_probe_base_params_keys_subset_of_production():
    """探测形态基础字段 ⊆ 生产形态基础字段（R7 防漂移）。

    探测用 build_base_params(stream=False, max_tokens=8, timeout=10)，
    生产形态显式传同值 build_base_params(stream=True, max_tokens=8, timeout=10)
    ——None 参数不产键语义下，生产形态缺 max_tokens/timeout 键会导致子集断言
    必然失败，故必须显式传同值。
    """
    from agent.generic.litellm_adapter import build_base_params

    common = {
        "model": "openai/gpt-4o",
        "api_base": "https://api.openai.com/v1",
        "api_key": "test-key",
    }
    probe_form = build_base_params(stream=False, max_tokens=8, timeout=10, **common)
    prod_form = build_base_params(stream=True, max_tokens=8, timeout=10, **common)
    assert set(probe_form.keys()) <= set(prod_form.keys()), (
        f"探测形态基础字段应 ⊆ 生产形态基础字段\n"
        f"probe keys: {sorted(probe_form)}\n"
        f"prod  keys: {sorted(prod_form)}"
    )


def test_probe_base_params_values_match_production():
    """同值传入时探测与生产的非 stream 字段值一致（只有 stream 布尔不同）。"""
    from agent.generic.litellm_adapter import build_base_params

    common = {
        "model": "openai/gpt-4o",
        "api_base": "https://api.openai.com/v1",
        "api_key": "test-key",
    }
    probe_form = build_base_params(stream=False, max_tokens=8, timeout=10, **common)
    prod_form = build_base_params(stream=True, max_tokens=8, timeout=10, **common)
    assert probe_form["stream"] is False
    assert prod_form["stream"] is True
    for key in ("stream_options", "max_tokens", "timeout", "model", "api_base", "api_key"):
        assert probe_form[key] == prod_form[key], f"字段 {key} 值应一致"


# ===== 三处探测一致性（预期红相，Task 3 转绿） =====


def test_probe_llm_passes_reasoning_effort_from_config():
    """_probe_llm 探测请求应携带配置的 reasoning_effort（不再硬编码 None）。

    探测与生产同一套参数（组件 3）——config.reasoning_effort 经
    assemble_request_params 注入 extra_body 送达。
    当前红相：compat.py L1494 硬编码 reasoning_effort=None，无 extra_body。
    """
    from niu_api.compat import _probe_llm

    cfg = {
        "apiType": "openai",
        "apiKey": "test-key",
        "apiBase": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "reasoning_effort": "high",
    }
    with patch("agent.generic.litellm_adapter.litellm.completion") as mock_completion:
        mock_completion.side_effect = Exception("stop-test")
        asyncio.run(_probe_llm(cfg))
        call_kwargs = mock_completion.call_args[1]
    assert call_kwargs["extra_body"]["reasoning_effort"] == "high", (
        f"_probe_llm 探测应透传 config.reasoning_effort（经 extra_body 送达），got: {call_kwargs}"
    )


def test_probe_response_format_passes_reasoning_effort_from_config():
    """probe-response-format 探测请求应携带配置的 reasoning_effort（不再硬编码 None）。

    当前红相：compat.py L1877 硬编码 reasoning_effort=None，无 extra_body。
    """
    from unittest.mock import AsyncMock

    from fastapi import Request

    from niu_api.compat import probe_response_format

    cfg = {
        "apiType": "openai",
        "apiKey": "test-key",
        "apiBase": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "reasoning_effort": "high",
    }
    mock_request = AsyncMock(spec=Request)
    mock_request.json = AsyncMock(return_value=cfg)

    with patch("agent.generic.litellm_adapter.litellm.completion") as mock_completion:
        mock_completion.side_effect = Exception("stop-test")
        result = asyncio.run(probe_response_format(mock_request))
        call_kwargs = mock_completion.call_args[1]
    assert "result" in result, "端点应正常返回结果"
    assert call_kwargs["extra_body"]["reasoning_effort"] == "high", (
        f"probe-response-format 探测应透传 config.reasoning_effort（经 extra_body 送达），got: {call_kwargs}"
    )
