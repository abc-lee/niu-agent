"""test-llm 端点接线单测：body/file 双路径 + resolve_probe_budget 预算透传。

mock Request（async json()）+ patch niu_api.compat._probe_llm——不依赖活 API。
注意：端点内 `from niu_api.llm_proxy import get_llm_config` 是函数内 import——
patch 目标必须是源头模块 niu_api.llm_proxy（patch compat 模块属性无效）。
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_request(body):
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    return req


@pytest.mark.asyncio
async def test_endpoint_body_path_uses_body_read_timeout():
    """body 非空 → 走 body 路径——body.read_timeout 覆盖预算（180 → 180/210）"""
    from niu_api.compat import test_llm

    body = {"apiKey": "sk", "apiBase": "https://x/v1", "model": "m", "read_timeout": 180}
    with patch("niu_api.compat._probe_llm", return_value=(True, "模型测试通过 (model=m)")) as mock_probe:
        result = await test_llm(_make_request(body))
    assert result["success"] is True
    assert mock_probe.call_args.kwargs["read_timeout"] == 180.0
    assert mock_probe.call_args.kwargs["wait_timeout"] == 210.0


@pytest.mark.asyncio
async def test_endpoint_body_path_no_read_timeout_default():
    """body 无 read_timeout → 默认 120/150"""
    from niu_api.compat import test_llm

    body = {"apiKey": "sk", "apiBase": "https://x/v1", "model": "m"}
    with patch("niu_api.compat._probe_llm", return_value=(True, "ok")) as mock_probe:
        result = await test_llm(_make_request(body))
    assert result["success"] is True
    assert mock_probe.call_args.kwargs["read_timeout"] == 120.0
    assert mock_probe.call_args.kwargs["wait_timeout"] == 150.0


@pytest.mark.asyncio
async def test_endpoint_file_path_uses_config_read_timeout():
    """空 body → 读文件配置——config.read_timeout 覆盖预算（启动器验证路径 180 → 180/210）"""
    from niu_api.compat import test_llm

    cfg = {"apiKey": "sk", "apiBase": "https://x/v1", "model": "m", "read_timeout": 180}
    with patch("niu_api.compat._probe_llm", return_value=(True, "ok")) as mock_probe, patch(
        "niu_api.llm_proxy.get_llm_config", return_value=cfg
    ):
        result = await test_llm(_make_request(None))
    assert result["success"] is True
    assert mock_probe.call_args.kwargs["read_timeout"] == 180.0
    assert mock_probe.call_args.kwargs["wait_timeout"] == 210.0


@pytest.mark.asyncio
async def test_endpoint_failure_maps_error():
    """失败 → {success: False, error: message}"""
    from niu_api.compat import test_llm

    body = {"apiKey": "sk", "apiBase": "https://x/v1", "model": "m"}
    with patch("niu_api.compat._probe_llm", return_value=(False, "API Key 无效或未授权")):
        result = await test_llm(_make_request(body))
    assert result["success"] is False
    assert result["error"] == "API Key 无效或未授权"
