"""测试 niu_api.llm_ready：resolve_probe_budget（预算解析/逃生口）与 check_llm_ready。

预算 = 120s/150s（覆盖 20-120s 首响应推理模型）；user-config llm.read_timeout
可覆盖（逃生口——>120s 慢模型通道，有效范围 ≤190s）。与启动器 test-llm 客户端
（230s）对齐。
注意：check_llm_ready 函数内 `from niu_api.compat import _probe_llm` /
`from niu_api.llm_proxy import get_llm_config`——patch 目标必须是源头模块
（niu_api.compat / niu_api.llm_proxy），patch llm_ready 模块属性无效。
"""
from unittest.mock import patch

import pytest

from niu_api.llm_ready import (
    STARTUP_READ_TIMEOUT,
    STARTUP_WAIT_TIMEOUT,
    resolve_probe_budget,
)


def test_gate_constants_pin_spec_values():
    """启动门控常量钉住计划规范值 120/150——与 _probe_llm 默认值对称断言
    （Task 1 test_probe_llm_default_budget_pins_spec 钉 _probe_llm 侧——
    两侧各自独立钉规范值，任一侧改动即失败提示同步）。"""
    assert STARTUP_READ_TIMEOUT == 120.0
    assert STARTUP_WAIT_TIMEOUT == 150.0


def test_escape_hatch_wait_below_three_way_client():
    """三方一致性不变量（v2.6）：Python 侧 MAX_READ_TIMEOUT+30 必须 < 230——
    230 是 launcher main.rs 两处 + settings 前端 socket 三处的跨语言字面量
    （无代码级连接，此测试只钉 Python 侧；客户端 230 改动靠实机验证清单
    场景 B 人工核对——若改小则挂起 provider 场景启动器提前超时 proceed-anyway
    静默降级重演 R6 P1）。"""
    from niu_api.llm_ready import MAX_READ_TIMEOUT

    assert MAX_READ_TIMEOUT + 30.0 < 230.0


def test_resolve_probe_budget_default():
    """无 read_timeout → 默认 120/150"""
    rt, wt = resolve_probe_budget({"apiKey": "sk", "model": "m"})
    assert rt == STARTUP_READ_TIMEOUT
    assert wt == STARTUP_WAIT_TIMEOUT


def test_resolve_probe_budget_override():
    """config.read_timeout=180 → (180, max(150, 180+30)=210)"""
    rt, wt = resolve_probe_budget({"read_timeout": 180})
    assert rt == 180.0
    assert wt == 210.0


def test_resolve_probe_budget_invalid_value():
    """非法值（非数值字符串/字典）→ 默认预算不抛异常（v2.4：float() 防护——
    否则 lifespan 启动崩溃、配置页不可达，击穿门控核心承诺）"""
    rt, wt = resolve_probe_budget({"read_timeout": "abc"})
    assert rt == STARTUP_READ_TIMEOUT
    assert wt == STARTUP_WAIT_TIMEOUT
    rt2, wt2 = resolve_probe_budget({"read_timeout": {"nested": 1}})
    assert rt2 == STARTUP_READ_TIMEOUT
    assert wt2 == STARTUP_WAIT_TIMEOUT


def test_resolve_probe_budget_zero_or_negative():
    """0/负数 → 默认预算（float 成功但无意义——防 read_timeout=0 立即超时）"""
    rt, wt = resolve_probe_budget({"read_timeout": 0})
    assert rt == STARTUP_READ_TIMEOUT
    assert wt == STARTUP_WAIT_TIMEOUT
    rt3, wt3 = resolve_probe_budget({"read_timeout": -5})
    assert rt3 == STARTUP_READ_TIMEOUT
    assert wt3 == STARTUP_WAIT_TIMEOUT


def test_resolve_probe_budget_bool():
    """bool 排除（v2.5：float(True)=1.0 会通过 <=0 检查 → 探测瞬间超时误判）"""
    rt, wt = resolve_probe_budget({"read_timeout": True})
    assert rt == STARTUP_READ_TIMEOUT
    assert wt == STARTUP_WAIT_TIMEOUT


def test_resolve_probe_budget_nan_inf():
    """NaN/inf 排除（v2.5：非有限值 → wait_for 永不触发 → lifespan 永久阻塞）"""
    rt, wt = resolve_probe_budget({"read_timeout": float("nan")})
    assert rt == STARTUP_READ_TIMEOUT
    rt2, wt2 = resolve_probe_budget({"read_timeout": float("inf")})
    assert rt2 == STARTUP_READ_TIMEOUT


def test_resolve_probe_budget_over_cap():
    """超上限钳制（v2.5：wait 必须 ≤ 三方客户端 230s——否则挂起 provider 时
    launcher 客户端超时 proceed-anyway 静默降级）"""
    from niu_api.llm_ready import MAX_READ_TIMEOUT

    rt, wt = resolve_probe_budget({"read_timeout": 500})
    assert rt == MAX_READ_TIMEOUT
    assert wt == MAX_READ_TIMEOUT + 30.0


@pytest.mark.asyncio
async def test_check_llm_ready_pass():
    from niu_api.llm_ready import check_llm_ready

    with patch("niu_api.llm_proxy.get_llm_config", return_value={"apiKey": "sk", "apiBase": "https://x/v1", "model": "m"}), patch(
        "niu_api.compat._probe_llm", return_value=(True, "模型测试通过 (model=m, provider=openai)")
    ) as mock_probe:
        ready, message = await check_llm_ready()
    assert ready is True
    assert "通过" in message
    mock_probe.assert_called_once()
    args, kwargs = mock_probe.call_args
    assert kwargs["read_timeout"] == STARTUP_READ_TIMEOUT
    assert kwargs["wait_timeout"] == STARTUP_WAIT_TIMEOUT


@pytest.mark.asyncio
async def test_check_llm_ready_fail_probe():
    from niu_api.llm_ready import check_llm_ready

    with patch("niu_api.llm_proxy.get_llm_config", return_value={"apiKey": "sk", "apiBase": "https://x/v1", "model": "m"}), patch(
        "niu_api.compat._probe_llm", return_value=(False, "API Key 无效或未授权")
    ):
        ready, message = await check_llm_ready()
    assert ready is False
    assert "API Key 无效" in message


@pytest.mark.asyncio
async def test_check_llm_ready_missing_config():
    """get_llm_config 永不 raise（异常返回空默认配置）——空配置走 _probe_llm 判空。
    真实路径：空默认配置 → _probe_llm 判空返回 (False, 'API Key 未配置')。"""
    from niu_api.llm_ready import check_llm_ready

    with patch("niu_api.llm_proxy.get_llm_config", return_value={}):
        ready, message = await check_llm_ready()
    assert ready is False
    assert "API Key 未配置" in message


@pytest.mark.asyncio
async def test_check_llm_ready_config_read_timeout_override():
    """逃生口经 resolve_probe_budget 生效：config.read_timeout=180 → 180/210"""
    from niu_api.llm_ready import check_llm_ready

    cfg = {"apiKey": "sk", "apiBase": "https://x/v1", "model": "m", "read_timeout": 180}
    with patch("niu_api.llm_proxy.get_llm_config", return_value=cfg), patch(
        "niu_api.compat._probe_llm", return_value=(True, "ok")
    ) as mock_probe:
        ready, _ = await check_llm_ready()
    assert ready is True
    assert mock_probe.call_args.kwargs["read_timeout"] == 180.0
    assert mock_probe.call_args.kwargs["wait_timeout"] == 210.0


@pytest.mark.asyncio
async def test_check_llm_ready_invalid_read_timeout_no_crash():
    """非法 read_timeout 不崩启动（float() 防护）：默认预算继续探测"""
    from niu_api.llm_ready import check_llm_ready

    cfg = {"apiKey": "sk", "apiBase": "https://x/v1", "model": "m", "read_timeout": "oops"}
    with patch("niu_api.llm_proxy.get_llm_config", return_value=cfg), patch(
        "niu_api.compat._probe_llm", return_value=(True, "ok")
    ) as mock_probe:
        ready, _ = await check_llm_ready()
    assert ready is True
    assert mock_probe.call_args.kwargs["read_timeout"] == STARTUP_READ_TIMEOUT


@pytest.mark.asyncio
async def test_check_llm_ready_custom_budget():
    """显式传预算参数（覆盖默认与 config 覆盖——显式传参优先）"""
    from niu_api.llm_ready import check_llm_ready

    with patch("niu_api.llm_proxy.get_llm_config", return_value={"apiKey": "sk", "apiBase": "https://x/v1", "model": "m", "read_timeout": 180}), patch(
        "niu_api.compat._probe_llm", return_value=(True, "ok")
    ) as mock_probe:
        ready, _ = await check_llm_ready(read_timeout=30.0, wait_timeout=60.0)
    assert ready is True
    assert mock_probe.call_args.kwargs["read_timeout"] == 30.0
    assert mock_probe.call_args.kwargs["wait_timeout"] == 60.0


def test_check_llm_ready_uses_minimal_probe_config(monkeypatch):
    """启动探测只测连通性——传给 _probe_llm 的配置只含白名单键，无能力参数（用户需求 2026-08-20）。"""
    import asyncio

    from niu_api import llm_ready

    captured = {}

    async def fake_probe(config, **kwargs):
        captured["config"] = config
        return True, "ok"

    monkeypatch.setattr("niu_api.compat._probe_llm", fake_probe)
    # 注意：check_llm_ready 内部是函数局部 import（from niu_api.compat import _probe_llm）
    # → 必须 patch 源头模块 niu_api.compat._probe_llm（llm_ready 无模块级属性，patch 它无效）
    # 注入带能力参数的已保存配置（get_llm_config 返回小写键 + setdefault provider）
    monkeypatch.setattr(
        "niu_api.llm_proxy.get_llm_config",
        lambda: {"apibase": "http://127.0.0.1:1", "apikey": "k", "model": "m", "type": "openai",
                 "provider": "openai",
                 "max_tokens": 32768, "thinking": {"type": "enabled"}, "reasoning_effort": "high",
                 "temperature": 0.7, "litellm_kwargs": {"max_tokens": 32768, "thinking": {"type": "enabled"}}},
    )

    async def run():
        return await llm_ready.check_llm_ready()

    ok, msg = asyncio.run(run())

    assert ok
    cfg = captured["config"]
    assert set(cfg.keys()) == {"apibase", "apikey", "model", "type", "provider"}
    assert "max_tokens" not in cfg
    assert "thinking" not in cfg
    assert "reasoning_effort" not in cfg
    assert "temperature" not in cfg
    assert "litellm_kwargs" not in cfg


def test_test_llm_empty_body_uses_minimal_probe_config(monkeypatch):
    """启动器兜底（body 空）读已保存配置时走最小连通配置——只测连通性。"""
    import asyncio

    from niu_api import compat

    captured = {}

    async def fake_probe(config, **kwargs):
        captured["config"] = config
        return True, "ok"

    monkeypatch.setattr(compat, "_probe_llm", fake_probe)
    monkeypatch.setattr(
        "niu_api.llm_proxy.get_llm_config",
        lambda: {"apiBase": "http://127.0.0.1:1", "apiKey": "k", "model": "m", "type": "openai",
                 "provider": "openai",
                 "max_tokens": 32768, "thinking": {"type": "enabled"}, "reasoning_effort": "high"},
    )

    class FakeRequest:
        async def json(self):
            return {}

    async def run():
        return await compat.test_llm(FakeRequest())

    result = asyncio.run(run())

    assert result["success"] is True
    cfg = captured["config"]
    assert set(cfg.keys()) == {"apibase", "apikey", "model", "type", "provider"}
    assert "max_tokens" not in cfg
    assert "thinking" not in cfg
    assert "reasoning_effort" not in cfg
