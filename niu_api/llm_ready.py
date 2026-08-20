"""LLM readiness gate for lifespan startup.

检测 LLM 配置是否可用（存在性 + 真实连通性探测，复用 compat._probe_llm）。
llm_ready=False 时 lifespan 跳过所有依赖 LLM 的后台组件
（scheduler/IM gateway/HAWatcher/db_monitor/脑区 gate/LightRAG 背景同步/
response_format 后台探测），仅保留 API 服务供配置页使用；
配置成功后由启动器退出并重启。

预算设计（v2.2/v2.4）：STARTUP_READ_TIMEOUT=120 / STARTUP_WAIT_TIMEOUT=150。
read_timeout 是真实首字节封顶（litellm 每次读超时，超时后重试但外层 wait_for
强杀）——120s 覆盖代码库显式支持的 20-120s 首响应推理模型（compat.py 注释）；
与启动器 test-llm 客户端超时（230s）与 settings 前端 socket（230s）对齐——
消除"后端短预算误判降级但启动器判定可用"的分歧（R1/R3 双轮修正）。

逃生口（v2.4 闭环）：resolve_probe_budget 从 config.read_timeout 覆盖预算
（float() 防护非法值）——check_llm_ready 与 /api/test-llm 端点共用——
>120s 首字节慢模型（provider 高负载，patch 前可用、patch 后会被一致判定失败
硬锁死）用户设 llm.read_timeout=180 即对门控、启动器验证、配置页保存全链路生效。
"""
from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

# 启动检测预算（唯一真相源）——test-llm 端点经 resolve_probe_budget 引用
STARTUP_READ_TIMEOUT = 120.0
STARTUP_WAIT_TIMEOUT = 150.0
# 逃生口 read_timeout 上限（v2.5）：wait = read+30 ≤ 220 < 三方客户端 230s——
# 超上限会让 wait 超过 launcher/main.js 客户端（230s）→ 挂起 provider 时启动器
# 超时 proceed-anyway 静默降级（R6 双审查 P1/P2）
MAX_READ_TIMEOUT = 190.0


def resolve_probe_budget(
    config: dict,
    *,
    read_timeout: float = STARTUP_READ_TIMEOUT,
    wait_timeout: float = STARTUP_WAIT_TIMEOUT,
) -> tuple[float, float]:
    """解析探测预算：config.read_timeout 覆盖默认（逃生口）。

    返回 (read_timeout, wait_timeout)。非法/非正值 read_timeout → 默认预算
    （v2.4：float() 转换必须防护——非法值若抛异常会导致 lifespan 启动崩溃、
    API 起不来、配置页不可达，恰好击穿门控"配置坏时保持 API"的核心承诺）。
    显式传参（read_timeout != 默认）优先于 config 覆盖。
    """
    if read_timeout == STARTUP_READ_TIMEOUT:
        configured = config.get("read_timeout")
        if configured:
            try:
                parsed = float(configured)
            except (ValueError, TypeError):
                logger.warning(f"[LLMGate] llm.read_timeout 非法值 {configured!r}，使用默认 {STARTUP_READ_TIMEOUT}s")
                return read_timeout, wait_timeout
            # v2.5 防护强化：bool/NaN/inf/非正值/超上限（R6 双审查）
            # bool: float(True)=1.0 会通过 <=0 检查 → 探测瞬间超时误判不可用
            # inf/nan: 非有限值 → wait_for 永不触发 → lifespan 永久阻塞
            # 超上限: wait(=read+30) 超过三方客户端 230s → 启动器超时 proceed 静默降级
            if isinstance(configured, bool) or not math.isfinite(parsed) or parsed <= 0:
                logger.warning(
                    f"[LLMGate] llm.read_timeout={configured!r} 非法（bool/非有限/非正），"
                    f"使用默认 {STARTUP_READ_TIMEOUT}s"
                )
                return read_timeout, wait_timeout
            if parsed > MAX_READ_TIMEOUT:
                logger.warning(f"[LLMGate] llm.read_timeout={parsed} 超上限 {MAX_READ_TIMEOUT}s，钳制到上限")
                parsed = MAX_READ_TIMEOUT
            read_timeout = parsed
            wait_timeout = max(wait_timeout, read_timeout + 30.0)
            logger.info(f"[LLMGate] 使用 user-config llm.read_timeout={read_timeout}（覆盖默认 {STARTUP_READ_TIMEOUT}s）")
    return read_timeout, wait_timeout


_MINIMAL_PROBE_KEYS = ("apibase", "apikey", "model", "type", "provider")


def _minimal_probe_config(config: dict) -> dict:
    """启动探测最小连通配置：白名单构造，只保留连通所需键。

    能力参数（max_tokens/thinking/reasoning_effort/temperature 等）天然排除——
    启动探测只验证"模型在不在、能不能用"（用户需求 2026-08-20）；能力组合可用性
    由设置页 testAndSave 探测把关（_probe_llm 本体零改动）。白名单而非黑名单剥离：
    未来 _probe_llm 新增能力参数自动被排除，无"忘记剥离"污染风险。
    """
    return {k: config[k] for k in _MINIMAL_PROBE_KEYS if k in config}


async def check_llm_ready(
    *,
    read_timeout: float = STARTUP_READ_TIMEOUT,
    wait_timeout: float = STARTUP_WAIT_TIMEOUT,
) -> tuple[bool, str]:
    """LLM 配置就绪检测：读取配置 + 真实连通性探测。

    返回 (ready, reason)。ready=True 才允许启动依赖 LLM 的后台组件。
    """
    from niu_api.compat import _probe_llm
    from niu_api.llm_proxy import get_llm_config

    try:
        config = get_llm_config()
    except Exception as e:
        # 防御性分支：get_llm_config 实际永不 raise（异常返回空默认配置），
        # 保留以防未来实现变更
        logger.warning(f"[LLMGate] 读取 LLM 配置失败: {e}")
        return False, f"读取 LLM 配置失败: {e}"
    config = {k.lower(): v for k, v in config.items()}

    read_timeout, wait_timeout = resolve_probe_budget(config, read_timeout=read_timeout, wait_timeout=wait_timeout)

    logger.info(f"[LLMGate] 探测 LLM 连通性（启动检测，预算 read_timeout={read_timeout}/wait_for={wait_timeout}）...")
    success, message = await _probe_llm(
        _minimal_probe_config(config),
        read_timeout=read_timeout,
        wait_timeout=wait_timeout,
    )
    if success:
        logger.info(f"[LLMGate] LLM 连通性检测通过: {message}")
        return True, message
    logger.warning(
        f"[LLMGate] LLM 连通性检测失败: {message} — "
        "跳过依赖 LLM 的后台组件，等待配置成功后重启"
    )
    return False, message
