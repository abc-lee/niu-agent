"""模型能力探测器核心（组件 1）。

探测 reasoning_effort / thinking 两项能力（response_format/tools 不在此测——
无档案消费点，且 rf 的探测归属"测试连接并保存"按钮的 testAndSave 流程，
用户拍板 2026-08-18），输出能力档案（~/.niu/model_capabilities.json），
供 CLI 壳（scripts/model_capability_probe.py）与 /api/model-capability-probe
端点共用。

探测项与成本控制（合计 ≈10 次极小请求/模型；值域候选超时重试最坏 7×2=14 次）：
  1. reasoning_effort 值域 [minimal, low, medium, high, xhigh, none, max] 按序探测，
     每个值至多 2 次请求（max_tokens=256、消息固定 "OK"、不传 timeout 等默认、stream=False；
     首次超时重试 1 次——豆包响应在 10s 边界波动，超时 ≠ 值不支持，R18）。
     7 值并行（max_workers=3——服务端并发限制，用户拍板）；值域全 200 时加
     无效值探针（INVALID_EFFORT_VALUE）判别 ignores_unknown（豆包 2026-08-18
     起 enabled 场景全值接受——"全 200"可能是真支持而非忽略未知参数，实测
     无效值 400 判别有效）。
     请求携带**场景配置的 thinking**（probe_config.litellm_kwargs.thinking——
     lightrag 场景恒 disabled、llm 场景按用户配置，P1-1 修复）——值域结论只对
     当前场景 thinking 成立，不得固定/默认 enabled（豆包实测：high + disabled
     400 Invalid combination；enabled 下测出的全 supported 不能外推到 disabled
     生产场景）
  2. thinking：enabled / disabled 各 1 次（raw_thinking 候选走 extra_body 注入；
     探测 config 副本剔除 litellm_kwargs.thinking——raw 候选单一来源，R13）

传输层（R3 修订）：不经过 LiteLLMSession/llmcore 归一化——直发 litellm.completion，
但复用 _derive_provider_prefix 的路由推导（volces.com → volcengine/，否则豆包
response_format 探测项在 openai 路由挂起不响应，注释实证 litellm_adapter.py L826-833）
+ assemble_request_params 同源注入（保证"与生产同参数"），绕开 BaseSession 合法值
白名单（llmcore.py L64-70——否则 max 被过滤为 None 假阳性，none 永远发不出）。
探测测的是"服务端认不认原始值"；生产发的是"配置值经归一后直发"——无矛盾：
配置页下拉只给档案 supported 值，生产发的值必在 supported 内。

分类规则（R2 精化：错误体必须从 e.body 取——litellm 的 e.response.text 实证为空、
 e.response.json() 抛异常；R19 修订：400 一律 unsupported——body 含 token 是充分
 条件但非必要条件，volcengine 路由实测 400 响应 body=None（litellm 未解析 body），
 body 缺失不改变 400 语义）：
  - 200 → supported
  - 400（值域扫描）→ unsupported，继续探测（值域不连续——400 本身表明该值不被接受）
  - 超时（litellm.Timeout / asyncio.TimeoutError）→ 重试该候选一次；重试仍超时 →
    记 unsupported（保守——无法确认支持）并继续探测（不 failed 终止）；重试遇
    其他非值域错误 → failed 终止
  - 401/404/429/5xx/网络 → probe_status=failed，终止，不覆盖旧档案（服务端拒绝/
    不可达 ≠ 慢，不重试）
  - ignores_unknown：reasoning_effort 7 值全部确认 200（无 400、无超时未确认）→
    加无效值探针判别（R11 增强）：无效值 400 → 服务端严格校验 → 全 200 = 真全支持
    （false）；无效值也 200 → 服务端静默忽略未知参数（true，档位 supported 全列表
    为假象）；探针无法判别（超时/网络）→ 保守 true。置位与场景
    thinking 同步（P1-1）：场景 thinking disabled 下同样判别——任一值
    被拒（如 high+disabled 400）即 false 不进入探针

partial 例外（R4/R6/R7/R9/R10/R17）：
  - response_format/tools 子项超时或失败 → 值域结果照写档案 + 子项标 timeout/
    unsupported，probe_status="partial"（不整体丢弃——response_format 挂起恰是
    豆包 openai 路由已知行为 litellm_adapter.py L826-833 注释实证，整体 failed
    会让 timeout 标记永不出现）
  - thinking 部分失败（enabled 400/disabled 200）→ thinking 段如实记录
    （{"enabled": false, "disabled": true, ...}），probe_status="partial"
  - thinking 双值均 400 → probe_status="partial"（"该服务端不支持 thinking 参数"
    是探测的正常结论，与 reasoning_effort 全 unsupported 时 probe_status="ok"
    对称——但按 R9/R10 规则 thinking 聚合仍记 partial）
  - thinking 双值均 200 → probe_status="ok"
  - reasoning_effort 全 unsupported → probe_status="ok"（探测完成，结果是不支持）

档案写安全（P2-2 修订）：原子写（临时文件 + os.replace）+ fcntl.flock 非阻塞写锁
（读-改-写整体持锁；锁被占用 → 跳过写入返回 False，不写坏旧档；探测进程单次调用
内只锁一次，避免嵌套锁）。失败不写坏旧档。
"""

import asyncio
import fcntl
import json
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import litellm

from agent.generic.litellm_adapter import (
    _derive_provider_prefix,
    assemble_request_params,
    build_base_params,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 探测常量
# ---------------------------------------------------------------------------

REASONING_EFFORT_CANDIDATES = ["minimal", "low", "medium", "high", "xhigh", "none", "max"]
# 无效值探针：判别"7 值全 200"是真全支持还是服务端忽略未知参数（R11 增强）。
# 无效值 400 → 服务端严格校验 → 全 200 = 真支持；无效值也 200 → 忽略未知参数。
# 豆包 2026-08-18 服务端更新接受全值后，"全 200"两种语义无法仅凭值域区分（实测无效值判别有效）。
INVALID_EFFORT_VALUE = "__probe_invalid__"
THINKING_CANDIDATES = ["enabled", "disabled"]
PROBE_MESSAGE = [{"role": "user", "content": "OK"}]
# max_tokens=256（对齐 test-llm 08-13 教训：thinking 模型 max_tokens 太小会被
# 截断误杀——豆包 thinking enabled + reasoning_effort high/max 深度思考时
# max_tokens=8 连思考链都放不下，响应被拖到 9.5s+ 贴超时边界 → 超时重试翻倍，
# 实测 222s 探测时长根因；model_probe.py 此前漏改此常量）。
# 探测不传 timeout（litellm 默认大超时）——显式短 timeout 会在模型深度思考
# （豆包 high/max 档实测 8-12s）返回前主动放弃（用户拍板 2026-08-18）。
PROBE_MAX_TOKENS = 256


def default_profile_path() -> Path:
    """能力档案路径 ~/.niu/model_capabilities.json。"""
    return Path.home() / ".niu" / "model_capabilities.json"


# 模块级常量（让测试 monkeypatch 生效，对齐 niu_api/config.py CONFIG_PATH 模式）
PROFILE_PATH = default_profile_path()


# ---------------------------------------------------------------------------
# 档案键与路径
# ---------------------------------------------------------------------------


def build_profile_key(api_base: str, model: str, lightrag: bool = False) -> str:
    """档案键：api_base|model|llm / api_base|model|lightrag（双场景统一无条件后缀）。

    api_base 规范化：rstrip("/")——settings 保存值与 CLI 读取值尾部斜杠差异不致
    档案不命中，写入/读取统一规范。
    """
    norm = (api_base or "").rstrip("/")
    return f"{norm}|{model}|{'lightrag' if lightrag else 'llm'}"


def is_local_api_base(api_base: str) -> bool:
    """本地模型判定（localhost/127.0.0.1 免 apiKey）——对齐 _probe_llm is_local 豁免。"""
    apibase = (api_base or "").lower()
    return (
        apibase.startswith("http://localhost")
        or apibase.startswith("http://127.0.0.1")
        or apibase.startswith("https://localhost")
        or apibase.startswith("https://127.0.0.1")
    )


# ---------------------------------------------------------------------------
# 档案读写（原子写 + flock 非阻塞锁）
# ---------------------------------------------------------------------------


def load_profile(profile_path=None) -> dict:
    """读全量档案。文件不存在/损坏 → {}（调用方以空档案处理；下次写入自愈）。"""
    path = Path(profile_path) if profile_path else PROFILE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        logger.warning("[model_probe] 档案非 dict 形状，按空档案处理: %s", path)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[model_probe] 档案读取失败，按空档案处理: %s (%s)", path, e)
    return {}


def read_profile(api_base: str, model: str, lightrag: bool = False, profile_path=None) -> dict | None:
    """读指定键的能力档案；无 → None。api_base 规范化 rstrip("/") 后匹配。"""
    data = load_profile(profile_path)
    if not data:
        return None
    return data.get(build_profile_key(api_base, model, lightrag))


def write_profile(profile: dict, lightrag: bool = False, profile_path=None) -> bool:
    """原子写档案（临时文件 + os.replace）+ fcntl.flock 非阻塞写锁。

    读-改-写整体持锁；锁被占用（另一探测进程在写）→ 跳过写入返回 False（旧档保留）。
    写失败抛异常（调用方感知——CLI 退出码 1）。
    """
    path = Path(profile_path) if profile_path else PROFILE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    key = build_profile_key(profile.get("api_base", ""), profile.get("model", ""), lightrag)

    lock_path = path.parent / (path.name + ".lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False  # 另一进程持锁——跳过写入，旧档保留

        existing = load_profile(profile_path=path)
        data = dict(existing)
        data[key] = profile
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
    return True


# ---------------------------------------------------------------------------
# 探测内部工具
# ---------------------------------------------------------------------------


def _section_from_user_config(user_config: dict | None, lightrag: bool) -> dict:
    """从 user-config 数据取对应场景段（键名小写归一——user-config.json 大写键 → 小写）。"""
    data = user_config or {}
    section = data.get("lightrag_llm" if lightrag else "llm") or {}
    return {str(k).lower(): v for k, v in section.items()}


def _strip_thinking_key(probe_config: dict) -> dict:
    """探测 config 副本剔除 litellm_kwargs.thinking（R13）。

    thinking 探测时 raw 候选必须单一来源：若 config.litellm_kwargs.thinking 同时
    存在，顶层通道（litellm_kwargs 合并）与 extra_body 注入双源冲突（volcengine
    transformation 顶层 thinking 转 extra_body 的合并顺序歧义）——副本剔除后
    只发 raw 候选。
    """
    litellm_kwargs = probe_config.get("litellm_kwargs") or {}
    if "thinking" not in litellm_kwargs:
        return probe_config
    new_litellm_kwargs = {k: v for k, v in litellm_kwargs.items() if k != "thinking"}
    return {**probe_config, "litellm_kwargs": new_litellm_kwargs}


def _is_timeout_error(exc: Exception) -> bool:
    """Timeout 类判定（litellm.Timeout / asyncio.TimeoutError）。

    区分"值域扫描超时"（探测请求 10s 超时/服务端慢——豆包响应在边界波动）与
    连接/网络等错误：Timeout 类 → 重试路径；其他（400/401/404/429/5xx/网络）→
    failed 终止（服务端拒绝/不可达 ≠ 慢）。
    """
    return isinstance(exc, (litellm.Timeout, asyncio.TimeoutError))


def _classify_value_domain_error(exc: Exception, token: str) -> str:
    """值域扫描错误分类（R2，R18 补超时，R19 修订）：status==400 → "unsupported"
    （保守——400 本身表明该值不被接受；body 含 token 是充分条件但非必要条件，body
    缺失不改变 400 语义——volcengine 路由实测 400 响应 body=None，litellm 未解析
    body）；Timeout 类 → "timeout"（重试该候选一次——超时 ≠ 值不支持）；其他状态码
    （401/404/429/5xx/网络）→ "failed"（终止——服务端拒绝/不可达 ≠ 慢，不重试）。

    错误体必须从 e.body 取——litellm 的 e.response.text 实证为空、e.response.json()
    抛异常。
    """
    if _is_timeout_error(exc):
        return "timeout"
    status = getattr(exc, "status_code", None)
    if status == 400:
        return "unsupported"
    return "failed"


def _describe_fail_reason(exc: Exception) -> str:
    """探测失败原因描述（用户可读——429 限流/401 认证/404 不存在/5xx 服务端）。"""
    status = getattr(exc, "status_code", None)
    if status == 429:
        return "服务端限流（429），请稍后重试"
    if status == 401:
        return "认证失败（401），请检查 API Key"
    if status == 404:
        return "模型或地址不存在（404），请检查 API 地址和模型名"
    if status and 500 <= status < 600:
        return f"服务端错误（{status}），请稍后重试"
    return f"{type(exc).__name__}: {str(exc)[:120]}"


def _response_message(response):
    """稳健取响应 message（mock 与真实 ModelResponse 均可）。"""
    try:
        return response.choices[0].message
    except Exception:  # noqa: BLE001 - mock 形状缺失时按无 message 处理
        return None


def _build_probe_params(
    api_base: str,
    api_key: str,
    model: str,
    api_type: str,
    probe_config: dict,
    *,
    raw_reasoning_effort: str | None = None,
    raw_thinking: dict | None = None,
    response_format: dict | None = None,
    tools: list | None = None,
    messages: list | None = None,
    timeout: int | None = None,
) -> dict:
    """组装单次探测请求参数（直发 litellm.completion）。

    与 chat() 同构（litellm_adapter.py L842-877 参数组装顺序）：
      1. build_base_params(stream=False, max_tokens=256) + 前缀推导 model——
         **不传 timeout**（litellm 默认大超时，等模型真实响应；临时脚本实测
         豆包深度思考档 8-12s，显式短 timeout 会主动放弃本会返回的响应——
         用户拍板 2026-08-18）
      2. litellm_kwargs 顶层合并（allowed_openai_params 等需顶层送达 litellm——
         与生产 request_params.update(litellm_kwargs) 一致）+ 非空时 drop_params
      3. response_format 顶层 + drop_params=True（与 chat() 调用点同决策）
      4. tools 顶层
      5. assemble_request_params 增量（extra_body 注入 + drop_params 决策）
    """
    params: dict = {
        **build_base_params(
            stream=False,
            max_tokens=PROBE_MAX_TOKENS,
            timeout=timeout,
            model=_derive_provider_prefix(api_base, model, api_type),
            api_base=api_base or None,
            api_key=api_key or None,
        ),
        "messages": messages if messages is not None else PROBE_MESSAGE,
    }
    litellm_kwargs = probe_config.get("litellm_kwargs") or {}
    if litellm_kwargs:
        params.update(litellm_kwargs)
        params["drop_params"] = True
    if response_format is not None:
        params["response_format"] = response_format
        params["drop_params"] = True
    if tools is not None:
        params["tools"] = tools
    params.update(assemble_request_params(
        probe_config,
        raw_reasoning_effort=raw_reasoning_effort,
        raw_thinking=raw_thinking,
    ))
    return params


# ---------------------------------------------------------------------------
# 探测项
# ---------------------------------------------------------------------------


def _scan_reasoning_effort(
    api_base: str, api_key: str, model: str, api_type: str,
    probe_config: dict, profile: dict,
) -> bool:
    """reasoning_effort 值域扫描。返回 False = failed 终止（调用方不落盘）。

    R18：候选超时（litellm.Timeout/asyncio.TimeoutError）→ 重试该候选一次；
    重试仍超时 → 记 unsupported（保守——无法确认支持）并继续探测（不 failed
    终止）；重试遇其他非值域错误 → failed 终止。
    R19：单值 400（body 缺失/None 亦然——volcengine 路由实测 400 响应 body=None，
    litellm 未解析 body）→ 一律 unsupported 继续探测，不因 body 无法匹配 token
    误分类 failed 中断。

    P1-1 修复——值域结论与场景 thinking 强耦合：请求 thinking 必须 = 场景配置的
    thinking（probe_config.litellm_kwargs.thinking：lightrag 场景恒 disabled、
    llm 场景按用户配置），不得固定/默认 enabled——否则 enabled 下测出的全 supported
    不能外推到 disabled 生产场景（豆包实测：high + disabled 400 Invalid combination，
    该值须记 unsupported，值域结论才与生产一致）。
    单一来源（同 _scan_thinking R13 纪律）：config 副本剔除 thinking 键（无顶层
    通道），场景 thinking 经 raw_thinking 显式注入 extra_body——顶层 + extra_body
    双源歧义消除（behavior 实证：volcengine/openai 路由最终 wire body 与双通道
    一致，传输无损）。
    """
    probe_config_no_thinking = _strip_thinking_key(probe_config)
    scene_thinking = (probe_config.get("litellm_kwargs") or {}).get("thinking")
    all_confirmed_supported = True  # ignores_unknown 只在 7 值全部确认 200 时置位（R11）

    def _probe_one(cand: str) -> tuple[str, bool]:
        """单值探测：200 → (cand, True)；400/超时重试仍失败 → (cand, False)；
        非值域错误 → raise（外层转 failed）。"""
        params = _build_probe_params(
            api_base, api_key, model, api_type, probe_config_no_thinking,
            raw_reasoning_effort=cand,
            raw_thinking=scene_thinking,
        )
        try:
            litellm.completion(**params)
            return cand, True
        except Exception as e:  # noqa: BLE001 - 分类规则覆盖全部异常
            cls = _classify_value_domain_error(e, "reasoning_effort")
            if cls == "unsupported":
                return cand, False
            if cls == "timeout":
                # 超时 → 重试该候选一次（豆包响应在 10s 边界波动，超时 ≠ 值不支持——
                # Task 5 实测 minimal 成功/low 超时即被 failed 终止的错误归因）
                try:
                    litellm.completion(**params)
                    return cand, True
                except Exception as e2:  # noqa: BLE001 - 分类规则覆盖全部异常
                    if _classify_value_domain_error(e2, "reasoning_effort") in ("unsupported", "timeout"):
                        # 重试仍超时/400 → 无法确认支持 → 保守记 unsupported，继续探测（不 failed 终止）
                        return cand, False
                    raise
            raise

    # 7 值并行（互不依赖——串行 7×慢响应是探测耗时的主因，并行收敛到最慢值）。
    # 并行度限制 3：服务端 API 有并发限制，7 个并行会触发限流/封禁（用户拍板）。
    with ThreadPoolExecutor(max_workers=3) as _ex:
        futures = {_ex.submit(_probe_one, cand): cand for cand in REASONING_EFFORT_CANDIDATES}
        for fut in as_completed(futures):
            try:
                cand, supported = fut.result()
            except Exception as e:
                profile["probe_status"] = "failed"
                profile["probe_fail_reason"] = _describe_fail_reason(e)
                return False
            if supported:
                profile["reasoning_effort"]["supported"].append(cand)
            else:
                profile["reasoning_effort"]["unsupported"].append(cand)
                all_confirmed_supported = False

    # 并行完成顺序不定——按候选顺序排序保持档案输出确定性
    profile["reasoning_effort"]["supported"].sort(key=REASONING_EFFORT_CANDIDATES.index)
    profile["reasoning_effort"]["unsupported"].sort(key=REASONING_EFFORT_CANDIDATES.index)
    if all_confirmed_supported:
        # 7 值全 200——需判别"真全支持"还是"服务端静默忽略未知参数"（R11）：
        # 加无效值探针：无效值 400 → 服务端严格校验 → 全 200 = 真支持（false）；
        # 无效值也 200 → 服务端忽略未知参数（true）；探针无法判别（超时/网络）→ 保守 true。
        # 背景：豆包 2026-08-18 服务端更新接受 reasoning_effort 全值——"全 200"从此
        # 可能是真支持而非忽略未知参数，仅凭值域无法区分（实测无效值 400 判别有效）。
        invalid_params = _build_probe_params(
            api_base, api_key, model, api_type, probe_config_no_thinking,
            raw_reasoning_effort=INVALID_EFFORT_VALUE,
            raw_thinking=scene_thinking,
        )
        try:
            litellm.completion(**invalid_params)
            # 无效值也 200 → 服务端不校验未知参数 → 忽略未知参数
            profile["ignores_unknown"] = True
        except Exception as e:  # noqa: BLE001 - 分类规则覆盖全部异常
            cls = _classify_value_domain_error(e, "reasoning_effort")
            if cls == "unsupported":
                # 无效值被拒（400）→ 服务端严格校验 → 全 200 = 真支持
                profile["ignores_unknown"] = False
            else:
                # 探针无法判别（超时/网络）→ 保守保持 true（R11 原语义：宁可少显示不误导）
                profile["ignores_unknown"] = True
    return True


def _scan_thinking(
    api_base: str, api_key: str, model: str, api_type: str,
    probe_config: dict, profile: dict,
) -> bool:
    """thinking enabled/disabled 各 1 次探测。返回 False = failed 终止。

    R13：探测 config 副本剔除 litellm_kwargs.thinking——raw 候选单一来源。
    R18：超时重试仅限值域扫描（reasoning_effort）——thinking 探测超时仍 failed
    终止（不重试）。
    R19：thinking 值 400（body 缺失亦然）→ 该值 false 继续（400 即该值不被接受），
    仅非 400 状态码（401/404/429/5xx/网络）→ failed 终止。
    状态聚合（R9/R10/R15/R17）：双 true→ok / 一 false→partial / 双 false→partial。
    """
    probe_config_no_thinking = _strip_thinking_key(probe_config)
    values: dict = {}
    returns_reasoning_content = False
    for cand in THINKING_CANDIDATES:
        params = _build_probe_params(
            api_base, api_key, model, api_type, probe_config_no_thinking,
            raw_thinking={"type": cand},
        )
        try:
            response = litellm.completion(**params)
            values[cand] = True
            message = _response_message(response)
            if message and getattr(message, "reasoning_content", None):
                returns_reasoning_content = True
        except Exception as e:  # noqa: BLE001 - 分类规则覆盖全部异常
            if _classify_value_domain_error(e, "thinking") == "unsupported":
                values[cand] = False
            else:
                profile["probe_status"] = "failed"
                profile["probe_fail_reason"] = _describe_fail_reason(e)
                return False
    profile["thinking"] = {
        "enabled": bool(values.get("enabled")),
        "disabled": bool(values.get("disabled")),
        "returns_reasoning_content": returns_reasoning_content,
    }
    if not (values.get("enabled") and values.get("disabled")):
        profile["probe_status"] = "partial"
    return True


# ---------------------------------------------------------------------------
# 探测主入口
# ---------------------------------------------------------------------------


def probe(
    *,
    api_base: str,
    api_key: str,
    model: str,
    api_type: str = "openai",
    lightrag: bool = False,
    user_config: dict | None = None,
    profile_path=None,
) -> dict:
    """探测模型能力，输出能力档案 dict（probe_status != "failed" 时落盘）。

    Args:
        api_base: API Base URL（规范化 rstrip("/") 后写入档案）
        api_key: API Key（本地模型可传 ""）
        model: 模型名（不带 provider 前缀）
        api_type: "openai"/"anthropic"（路由推导用）
        lightrag: True = lightrag_llm 场景（档案键后缀 |lightrag，config 取
            lightrag_llm 段）；False = llm 场景（档案键后缀 |llm）
        user_config: user-config.json 全量数据（取对应段 litellm_kwargs 等；
            None → 空段，探测仅 raw 候选）
        profile_path: 档案路径覆盖（默认 PROFILE_PATH；测试传 tmp 路径）

    Returns:
        能力档案 dict。probe_status:
          - "failed": 值域扫描遇非值域错误终止（超时重试 1 次后仍失败亦终止）——
            不覆盖旧档案（调用方退出码 1）
          - "partial": 值域成功但 thinking 部分不支持 / response_format/tools 子项失败
          - "ok": 全部探测完成
    """
    if not api_base:
        raise ValueError("api_base 不能为空")
    if not model:
        raise ValueError("model 不能为空")

    # 请求原样透传用户配置的 api_base（与生产同参数）；档案/键规范化 rstrip("/")
    norm_api_base = api_base.rstrip("/")
    section = _section_from_user_config(user_config, lightrag)
    probe_config = {
        "reasoning_effort": section.get("reasoning_effort"),
        "litellm_kwargs": section.get("litellm_kwargs") or {},
        "extra_body": section.get("extra_body") or {},
    }

    profile = {
        "api_base": norm_api_base,
        "model": model,
        "probed_at": datetime.now().isoformat(timespec="seconds"),
        "probe_status": "ok",
        "ignores_unknown": False,
        "reasoning_effort": {"supported": [], "unsupported": []},
        "thinking": {},
    }

    if not _scan_reasoning_effort(api_base, api_key, model, api_type, probe_config, profile):
        return profile  # failed——不落盘（旧档保留）

    # thinking 探测（response_format/tools 不在此测——无档案消费点，且 rf 的
    # 探测归属"测试连接并保存"按钮的 testAndSave 流程；用户拍板 2026-08-18）
    if not _scan_thinking(api_base, api_key, model, api_type, probe_config, profile):
        return profile  # thinking failed——不落盘（旧档保留）

    if profile["probe_status"] != "failed":
        write_profile(profile, lightrag=lightrag, profile_path=profile_path)
    return profile
