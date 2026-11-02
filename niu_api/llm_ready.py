"""LLM readiness gate for lifespan startup.

检测 LLM 配置是否可用（存在性 + 连通性探测，由本模块 _probe_connectivity 独立
完成——设置页探测 compat._probe_llm 是另一套判定，两者完全分离，互不复用）。
llm_ready=False 时 lifespan 跳过所有依赖 LLM 的后台组件
（scheduler/IM gateway/HAWatcher/db_monitor/脑区 gate/LightRAG 背景同步），
仅保留 API 服务供配置页使用；配置成功后由启动器退出并重启。

预算设计（v2.2/v2.4）：STARTUP_READ_TIMEOUT=120 / STARTUP_WAIT_TIMEOUT=150。
read_timeout 是真实首字节封顶（litellm 每次读超时，超时后重试但外层 wait_for
强杀）——120s 覆盖代码库显式支持的 20-120s 首响应推理模型（compat.py 注释）；
预算常量经 resolve_probe_budget 同时服务设置页 /api/test-llm，并与 settings
前端 socket（230s）对齐——消除"后端短预算误判降级但设置页判定可用"的分歧
（R1/R3 双轮修正；启动器已不再自行探测）。

逃生口（v2.4 闭环）：resolve_probe_budget 从 config.read_timeout 覆盖预算
（float() 防护非法值）——check_llm_ready 与 /api/test-llm 端点共用——
>120s 首字节慢模型（provider 高负载，patch 前可用、patch 后会被一致判定失败
硬锁死）用户设 llm.read_timeout=180 即对门控、配置页保存全链路生效。
"""
from __future__ import annotations

import asyncio
import logging
import math

logger = logging.getLogger(__name__)

# 启动检测预算（唯一真相源）——test-llm 端点经 resolve_probe_budget 引用
STARTUP_READ_TIMEOUT = 120.0
STARTUP_WAIT_TIMEOUT = 150.0
# 逃生口 read_timeout 上限（v2.5）：wait = read+30 ≤ 220 < 三方客户端 230s——
# 超上限会让 wait 超过 settings 前端 socket（230s）→ 挂起 provider 时设置页
# socket 超时（R6 双审查 P1/P2）
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


# 生产链路 agent/runner.py 的 create_client 不传 proxy（只取 apikey/apibase/model/type
# 等），探测同样不传：否则 llm.proxy 配坏时会出现「探测失败、生产可用」的判定分歧。
_CONNECTIVITY_PASSTHROUGH_KEYS = ("apibase", "apikey", "model", "type", "provider")


def _connectivity_error_text(exc: Exception) -> str:
    """连通性探测异常→用户可读文本（脱敏：key=/Bearer 打码）。"""
    import re

    msg = str(exc)
    low = msg.lower()
    if "401" in msg or "unauthorized" in low or "invalid api key" in low:
        return "API Key 无效或未授权"
    if "404" in msg or "not found" in low:
        return "模型或 API 端点不存在，请检查模型名称和地址"
    safe = re.sub(r"key=[^&\s]+", "key=***", msg)
    safe = re.sub(r"Bearer\s+[^\s]+", "Bearer ***", safe)[:200]
    if "provider" in low or "unmapped" in low:
        return f"Provider 路由错误: {safe}"
    return f"模型测试失败: {safe}"


async def _probe_connectivity(
    config: dict,
    *,
    read_timeout: float = STARTUP_READ_TIMEOUT,
    wait_timeout: float = STARTUP_WAIT_TIMEOUT,
) -> tuple[bool, str]:
    """启动连通性探测（全程唯一一处）：发 hi，拿到任何非空输出即通过。

    与设置页探测（compat._probe_llm）判定完全分离：
    - 不注入 capabilities deny、不透传 reasoning_effort / thinking / temperature
    - 不传 max_tokens（历史教训：探测写死上限 5/50/256 都造成过误判——7ad29dc7、5ef339c8、f2416de5）
    - 不看 finish_reason；首个非空正文块即返回，不再消费后续流
    """
    try:
        return await _probe_connectivity_impl(
            config, read_timeout=read_timeout, wait_timeout=wait_timeout
        )
    except Exception as e:  # noqa: BLE001 — 探测绝不允许打断 lifespan
        return False, _connectivity_error_text(e)


async def _probe_connectivity_impl(
    config: dict,
    *,
    read_timeout: float,
    wait_timeout: float,
) -> tuple[bool, str]:
    """连通性探测主体：除构造/流异常外不再自行兜底（外层统一兜住）。"""
    from agent.generic.litellm_adapter import LiteLLMSession

    config = {k.lower(): v for k, v in (config or {}).items()}
    # 按「键存在」收集（不能按真值过滤：本地模型 apikey="" 是受支持形态，
    # 丢掉空 key 会让 BaseSession.__init__ 的 cfg["apikey"] 抛 KeyError）
    llm_config = {k: config[k] for k in _CONNECTIVITY_PASSTHROUGH_KEYS if k in config}
    llm_config.setdefault("apikey", "")
    apibase = str(llm_config.get("apibase") or "")
    apikey = str(llm_config.get("apikey") or "")
    is_local = apibase.startswith(
        ("http://localhost", "http://127.0.0.1", "https://localhost", "https://127.0.0.1")
    )
    if not apikey and not is_local:
        return False, "API Key 未配置"
    if not apibase:
        return False, "API 地址未配置"
    if not llm_config.get("model"):
        return False, "模型名称未配置"

    # 用户手编的 litellm_kwargs 可能是字符串/列表（手册允许手改）→ 必须 isinstance 守卫
    kw = config.get("litellm_kwargs")
    sticky = kw.get("sticky_session_headers") if isinstance(kw, dict) else None
    llm_config.update(
        {
            "api_type": llm_config.pop("type", "openai"),
            "apikey": apikey,
            "apibase": apibase,
            "model": llm_config.get("model") or "",
            "provider": llm_config.get("provider") or "",
            "litellm_kwargs": {"sticky_session_headers": sticky} if sticky else {},
            "read_timeout": read_timeout,
            # opencode zen go 要求会话头（缺失 → 400 MissingSessionID），与设置页探测同值
            "sticky_session_id": "probe-test-llm",
        }
    )

    def _sync_probe() -> tuple[bool, str]:
        # 构造也放在这里：任何 cfg 异常都被外层 except 兜住，绝不逃出 lifespan
        session = LiteLLMSession(cfg=llm_config)
        gen = session.chat(messages=[{"role": "user", "content": "hi"}])
        resp = None
        try:
            while True:
                chunk = next(gen)
                if isinstance(chunk, str) and chunk.strip():
                    return True, "连通性正常（有正文输出）"
        except StopIteration as e:
            resp = e.value
        finally:
            gen.close()
        if resp is not None and (getattr(resp, "content", "") or getattr(resp, "thinking", "")):
            return True, "连通性正常（有思考输出）"
        return False, "模型没有任何输出（正文与思考均为空）"

    try:
        return await asyncio.wait_for(asyncio.to_thread(_sync_probe), timeout=wait_timeout)
    except TimeoutError:
        return False, "连接超时，请检查网络和 API 地址"


async def check_llm_ready(
    *,
    read_timeout: float = STARTUP_READ_TIMEOUT,
    wait_timeout: float = STARTUP_WAIT_TIMEOUT,
) -> tuple[bool, str]:
    """LLM 配置就绪检测：读取配置 + 真实连通性探测。

    返回 (ready, reason)。ready=True 才允许启动依赖 LLM 的后台组件。
    """
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
    success, message = await _probe_connectivity(
        config,
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
