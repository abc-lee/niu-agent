"""模型能力探测器核心（组件 1）。

探测 reasoning_effort / thinking / response_format / tools 四项能力，输出能力档案
（~/.niu/model_capabilities.json），供 CLI 壳（scripts/model_capability_probe.py）与
/api/model-capability-probe 端点共用。

探测项与成本控制（合计 ≈11 次极小请求/模型，单次 ≤10s）：
  1. reasoning_effort 值域 [minimal, low, medium, high, xhigh, none, max] 按序探测，
     每个值 1 次请求（max_tokens=8、消息固定 "OK"、timeout=10、stream=False）
  2. thinking：enabled / disabled 各 1 次（raw_thinking 候选走 extra_body 注入；
     探测 config 副本剔除 litellm_kwargs.thinking——raw 候选单一来源，R13）
  3. response_format：json_object 1 次（litellm_kwargs 合并
     allowed_openai_params=["response_format"] 逃生口——volcengine 路由 drop_params
     会静默丢弃 response_format，不加逃生口测到假 ok）
  4. tools：1 次带工具请求

传输层（R3 修订）：不经过 LiteLLMSession/llmcore 归一化——直发 litellm.completion，
但复用 _derive_provider_prefix 的路由推导（volces.com → volcengine/，否则豆包
response_format 探测项在 openai 路由挂起不响应，注释实证 litellm_adapter.py L826-833）
+ assemble_request_params 同源注入（保证"与生产同参数"），绕开 BaseSession 合法值
白名单（llmcore.py L64-70——否则 max 被过滤为 None 假阳性，none 永远发不出）。
探测测的是"服务端认不认原始值"；生产发的是"配置值经归一后直发"——无矛盾：
配置页下拉只给档案 supported 值，生产发的值必在 supported 内。

分类规则（R2 精化：错误体必须从 e.body 取——litellm 的 e.response.text 实证为空、
e.response.json() 抛异常）：
  - 200 → supported
  - 400 + e.body 含 "reasoning_effort"（值域扫描）→ unsupported，继续探测（值域不连续）
  - 其他 400（max_tokens 过小/min 约束/模型名错/限流——body 不含 token）→
    probe_status=failed，终止，不覆盖旧档案
  - 401/404/429/5xx/网络 → probe_status=failed，终止，不覆盖旧档案
  - ignores_unknown：reasoning_effort 7 值全 200 且无一个 400 → true（服务端静默
    忽略未知参数——档位 supported 全列表为假象）；部分值 400 → false

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
THINKING_CANDIDATES = ["enabled", "disabled"]
PROBE_MESSAGE = [{"role": "user", "content": "OK"}]
PROBE_MAX_TOKENS = 8
PROBE_TIMEOUT = 10
PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "probe_tool",
        "description": "模型能力探测工具（探测 tools 参数支持）",
        "parameters": {"type": "object", "properties": {}},
    },
}


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


def _classify_value_domain_error(exc: Exception, token: str) -> str:
    """值域扫描错误分类（R2）：400 + e.body 含 token → "unsupported"（继续探测）；
    其他（400 无 token / 401 / 404 / 429 / 5xx / 网络 / 超时）→ "failed"（终止）。

    错误体必须从 e.body 取——litellm 的 e.response.text 实证为空、e.response.json()
    抛异常。
    """
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    body_text = json.dumps(body, ensure_ascii=False) if body else ""
    if status == 400 and token in body_text:
        return "unsupported"
    return "failed"


def _classify_subitem_error(exc: Exception) -> str:
    """子项（response_format/tools）错误分类：超时/挂起 → "timeout"；其他 → "unsupported"。"""
    if isinstance(exc, (litellm.Timeout, asyncio.TimeoutError)):
        return "timeout"
    return "unsupported"


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
) -> dict:
    """组装单次探测请求参数（直发 litellm.completion）。

    与 chat() 同构（litellm_adapter.py L842-877 参数组装顺序）：
      1. build_base_params(stream=False, max_tokens=8, timeout=10) + 前缀推导 model
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
            timeout=PROBE_TIMEOUT,
            model=_derive_provider_prefix(api_base, model, api_type),
            api_base=api_base or None,
            api_key=api_key or None,
        ),
        "messages": PROBE_MESSAGE,
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
    """reasoning_effort 值域扫描。返回 False = failed 终止（调用方不落盘）。"""
    saw_400 = False
    for cand in REASONING_EFFORT_CANDIDATES:
        params = _build_probe_params(
            api_base, api_key, model, api_type, probe_config,
            raw_reasoning_effort=cand,
        )
        try:
            litellm.completion(**params)
            profile["reasoning_effort"]["supported"].append(cand)
        except Exception as e:  # noqa: BLE001 - 分类规则覆盖全部异常
            if _classify_value_domain_error(e, "reasoning_effort") == "unsupported":
                profile["reasoning_effort"]["unsupported"].append(cand)
                saw_400 = True
            else:
                profile["probe_status"] = "failed"
                return False
    if not saw_400:
        # 7 值全 200 且无一个 400 → 服务端静默忽略未知参数（R11）
        profile["ignores_unknown"] = True
    return True


def _scan_thinking(
    api_base: str, api_key: str, model: str, api_type: str,
    probe_config: dict, profile: dict,
) -> bool:
    """thinking enabled/disabled 各 1 次探测。返回 False = failed 终止。

    R13：探测 config 副本剔除 litellm_kwargs.thinking——raw 候选单一来源。
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
                return False
    profile["thinking"] = {
        "enabled": bool(values.get("enabled")),
        "disabled": bool(values.get("disabled")),
        "returns_reasoning_content": returns_reasoning_content,
    }
    if not (values.get("enabled") and values.get("disabled")):
        profile["probe_status"] = "partial"
    return True


def _probe_response_format(
    api_base: str, api_key: str, model: str, api_type: str,
    probe_config: dict, profile: dict,
) -> None:
    """response_format json_object 1 次探测（R7 逃生口：allowed_openai_params）。

    超时/挂起 → {"status": "timeout", "supported": []}（区别于 400 的 unsupported，
    供 UI 提示"该服务端对 response_format 挂起"）；失败仅降级 partial。
    """
    rf_kwargs = {**(probe_config.get("litellm_kwargs") or {}), "allowed_openai_params": ["response_format"]}
    rf_config = {**probe_config, "litellm_kwargs": rf_kwargs}
    params = _build_probe_params(
        api_base, api_key, model, api_type, rf_config,
        response_format={"type": "json_object"},
    )
    try:
        litellm.completion(**params)
        status = "ok"
    except Exception as e:  # noqa: BLE001 - 子项失败仅降级 partial
        status = _classify_subitem_error(e)
    profile["response_format"] = {
        "status": status,
        "supported": ["json_object"] if status == "ok" else [],
    }
    if status != "ok":
        profile["probe_status"] = "partial"


def _probe_tools(
    api_base: str, api_key: str, model: str, api_type: str,
    probe_config: dict, profile: dict,
) -> None:
    """tools 1 次带工具请求（R6/R8）。

    200 带 tool_calls → ok；200 无 tool_calls → ok（服务端接受 tools 参数即视为
    支持，模型选择纯文本回复属正常）；超时 → timeout；400/其他 → unsupported。
    """
    params = _build_probe_params(
        api_base, api_key, model, api_type, probe_config,
        tools=[PROBE_TOOL],
    )
    try:
        litellm.completion(**params)
        status = "ok"
    except Exception as e:  # noqa: BLE001 - 子项失败仅降级 partial
        status = _classify_subitem_error(e)
    profile["tools"] = {
        "status": status,
        "supported": ["probe_tool"] if status == "ok" else [],
    }
    if status != "ok":
        profile["probe_status"] = "partial"


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
          - "failed": 值域扫描遇非值域错误终止——不覆盖旧档案（调用方退出码 1）
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
        "response_format": {"status": "ok", "supported": ["json_object"]},
        "tools": {"status": "ok", "supported": ["probe_tool"]},
    }

    if not _scan_reasoning_effort(api_base, api_key, model, api_type, probe_config, profile):
        return profile  # failed——不落盘（旧档保留）
    if not _scan_thinking(api_base, api_key, model, api_type, probe_config, profile):
        return profile  # failed——不落盘（旧档保留）

    _probe_response_format(api_base, api_key, model, api_type, probe_config, profile)
    _probe_tools(api_base, api_key, model, api_type, probe_config, profile)

    if profile["probe_status"] != "failed":
        write_profile(profile, lightrag=lightrag, profile_path=profile_path)
    return profile
