"""model-capability-probe 端点接线单测：body 小写归一 / llm-lightrag 分流 / probe_status 形状。

mock Request（async json()）+ patch niu_api.model_probe.probe（源头模块——端点内
函数级 import，patch compat 模块属性无效，同 test_llm_endpoint_wiring.py 模式）。
本端点是同步探测核心的薄壳（asyncio.to_thread），不依赖活 API、不起服务。
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from niu_api.model_probe import build_profile_key


def _make_request(body):
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    return req


def _fake_profile(probe_status="ok"):
    """探测核心返回的档案形状（值域扫描正常完成的形态）。"""
    return {
        "api_base": "https://api.example.com/v1",
        "model": "m",
        "probed_at": "2026-08-18T00:00:00",
        "probe_status": probe_status,
        "ignores_unknown": False,
        "reasoning_effort": {
            "supported": ["minimal", "low", "medium", "high"],
            "unsupported": ["xhigh", "none", "max"],
        },
        "thinking": {"enabled": True, "disabled": True, "returns_reasoning_content": False},
        "response_format": {"status": "ok", "supported": ["json_object"]},
        "tools": {"status": "ok", "supported": ["probe_tool"]},
    }


@pytest.mark.asyncio
async def test_endpoint_lowercases_body_and_probes_llm_section():
    """body 大写键（apiKey/apiBase）→ 小写归一；无 lightrag 标记 → llm 段分流。

    user_config 按 {"llm": ...} 包裹（探测核心取段小写归一）；档案键后缀 |llm。
    """
    from niu_api.compat import model_capability_probe

    body = {"apiKey": "sk", "apiBase": "https://api.example.com/v1", "model": "m", "type": "openai"}
    with patch("niu_api.model_probe.probe", return_value=_fake_profile()) as mock_probe:
        result = await model_capability_probe(_make_request(body))

    mock_probe.assert_called_once()
    kwargs = mock_probe.call_args.kwargs
    assert kwargs["api_base"] == "https://api.example.com/v1"
    assert kwargs["api_key"] == "sk"
    assert kwargs["model"] == "m"
    assert kwargs["api_type"] == "openai"
    assert kwargs["lightrag"] is False
    assert kwargs["user_config"] == {
        "llm": {"apikey": "sk", "apibase": "https://api.example.com/v1", "model": "m", "type": "openai"}
    }
    # probe_status JSON 返回形状：status + 档案路径 + 键 + 档案摘要
    assert result["probe_status"] == "ok"
    assert result["profile_path"].endswith("model_capabilities.json")
    assert result["profile_key"] == build_profile_key("https://api.example.com/v1", "m", lightrag=False)
    assert result["profile_key"].endswith("|llm")
    assert result["profile"]["reasoning_effort"]["supported"] == ["minimal", "low", "medium", "high"]


@pytest.mark.asyncio
async def test_endpoint_lightrag_marker_routes_to_lightrag_section():
    """body 顶层 lightrag: true → lightrag 段分流（user_config={"lightrag_llm": ...}，档案键 |lightrag）。

    标记 pop 后不得进入探测 config（_section_from_user_config 取段时不误带）。
    """
    from niu_api.compat import model_capability_probe

    body = {"apiKey": "sk", "apiBase": "https://api.example.com/v1", "model": "m", "lightrag": True}
    with patch("niu_api.model_probe.probe", return_value=_fake_profile()) as mock_probe:
        result = await model_capability_probe(_make_request(body))

    kwargs = mock_probe.call_args.kwargs
    assert kwargs["lightrag"] is True
    assert kwargs["user_config"] == {
        "lightrag_llm": {"apikey": "sk", "apibase": "https://api.example.com/v1", "model": "m"}
    }
    assert result["profile_key"].endswith("|lightrag")


@pytest.mark.asyncio
async def test_endpoint_partial_status_shape():
    """探测核心返回 partial（thinking 部分失败 / response_format 或 tools 子项失败）→ 原样透传。"""
    from niu_api.compat import model_capability_probe

    body = {"apiKey": "sk", "apiBase": "https://api.example.com/v1", "model": "m"}
    with patch("niu_api.model_probe.probe", return_value=_fake_profile("partial")):
        result = await model_capability_probe(_make_request(body))
    assert result["probe_status"] == "partial"
    assert result["profile"]["thinking"] == {
        "enabled": True,
        "disabled": True,
        "returns_reasoning_content": False,
    }


@pytest.mark.asyncio
async def test_endpoint_failed_status_shape():
    """探测失败（值域扫描遇非值域错误终止，不覆盖旧档）→ probe_status=failed 返回形状。"""
    from niu_api.compat import model_capability_probe

    body = {"apiKey": "sk", "apiBase": "https://api.example.com/v1", "model": "m"}
    with patch("niu_api.model_probe.probe", return_value=_fake_profile("failed")):
        result = await model_capability_probe(_make_request(body))
    assert result["probe_status"] == "failed"


@pytest.mark.asyncio
async def test_endpoint_missing_fields_validation():
    """缺 apibase/model/apiKey（非本地）→ probe_status=failed + error，不调探测核心。"""
    from niu_api.compat import model_capability_probe

    with patch("niu_api.model_probe.probe") as mock_probe:
        r1 = await model_capability_probe(_make_request({}))
        assert r1["probe_status"] == "failed"
        assert "API 地址" in r1["error"]

        r2 = await model_capability_probe(_make_request({"apiBase": "https://api.example.com/v1"}))
        assert r2["probe_status"] == "failed"
        assert "模型" in r2["error"]

        r3 = await model_capability_probe(
            _make_request({"apiBase": "https://api.example.com/v1", "model": "m"})
        )
        assert r3["probe_status"] == "failed"
        assert "API Key" in r3["error"]
    mock_probe.assert_not_called()


@pytest.mark.asyncio
async def test_endpoint_local_api_base_exempts_api_key():
    """本地模型（localhost/127.0.0.1）免 apiKey——对齐 _probe_llm is_local 豁免。"""
    from niu_api.compat import model_capability_probe

    body = {"apiBase": "http://localhost:11434", "model": "llama3"}
    with patch("niu_api.model_probe.probe", return_value=_fake_profile()) as mock_probe:
        result = await model_capability_probe(_make_request(body))
    assert result["probe_status"] == "ok"
    assert mock_probe.call_args.kwargs["api_key"] == ""


@pytest.mark.asyncio
async def test_endpoint_probe_exception_returns_failed_json():
    """probe() 抛异常（如 ValueError）→ 端点返回 probe_status=failed + error，
    不抛 500（对齐 /api/test-llm 端点防御模式）。"""
    from niu_api.compat import model_capability_probe

    body = {"apiKey": "sk", "apiBase": "https://api.example.com/v1", "model": "m"}
    with patch("niu_api.model_probe.probe", side_effect=ValueError("LLM 探测失败")):
        result = await model_capability_probe(_make_request(body))
    assert result["probe_status"] == "failed"
    assert "探测异常" in result["error"]
    assert "LLM 探测失败" in result["error"]
