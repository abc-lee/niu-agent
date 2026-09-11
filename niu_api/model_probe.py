"""模型能力探测器核心（组件 1）。

探测 reasoning_effort / thinking 两项能力（response_format/tools 不在此测——
无档案消费点，且 rf 的探测归属"测试连接并保存"按钮的 testAndSave 流程；
用户拍板 2026-08-18），输出能力档案（~/.niu/model_capabilities.json），
供 CLI 壳（scripts/model_capability_probe.py）与 /api/model-capability-probe
端点共用。

参数可用性探测段（plan 2026-09-10-param-deny-mechanism D3，T2）：值域扫描之前
以「运行时实际发送集 ∩ deny 白名单」发真实请求——400 定位被拒参数（错误消息
正则提取 + 逐个累积移除），定位即写 user-config.json 对应段 capabilities.deny
（llm/lightrag_llm 双落点 + llm-configs.json 命名配置同步）；通过后只清本次
测到且通过的 deny 项（洗白）。response_format/连接项/必需项不入候选。

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

档案写安全（P2-2 修订）：原子写（临时文件 + os.replace）+ 跨平台非阻塞写锁
（Unix fcntl.flock / Windows msvcrt.locking；读-改-写整体持锁；锁被占用 → 跳过写入返回 False，不写坏旧档；探测进程单次调用
内只锁一次，避免嵌套锁）。失败不写坏旧档。
"""

import asyncio
import base64
import json
import logging
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from io import BytesIO
from pathlib import Path

import litellm
from PIL import Image

# 跨平台文件锁（Windows 无 fcntl——探测页在 Windows 上 import 即崩，2026-08-18 实证）。
# 对齐 compat.py _flock/_funlock 模式：Unix 用 fcntl.flock，Windows 用 msvcrt.locking。
if sys.platform == "win32":
    import msvcrt

    def _lock_nonblocking(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_nonblocking(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

from agent.generic.litellm_adapter import (
    _derive_provider_prefix,
    assemble_request_params,
    build_base_params,
    resolved_provider,
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

# ---------------------------------------------------------------------------
# 参数可用性探测段常量（plan 2026-09-10-param-deny-mechanism D3）
# ---------------------------------------------------------------------------
# deny 白名单候选（D3-2）：仅这些参数可进 deny 判定；response_format 排除（R1——
# 静默剥离会使 LightRAG 关键词抽取失去 JSON 契约，rf 已有三档探测治理）；连接项
# （api_key/api_base/timeout）与必需项（model/messages/stream）永不入候选。
# 封闭白名单（R9 备案：扩白名单=在此集合加一行）。顺序=累积线性移除的确定性顺序。
DENY_CANDIDATE_WHITELIST = [
    "temperature", "top_p", "presence_penalty", "frequency_penalty",
    "seed", "logit_bias", "max_tokens",
]
# 400 错误消息参数名提取（D3-3①：已知格式，仅作候选——提取出的名字必须在当前
# 候选集内才采信；未命中 → 回落白名单顺序逐个累积移除）。
_DENIED_PARAM_PATTERNS = (
    re.compile(r"\binvalid\s+(\w+)"),                    # K3: invalid temperature: only 1 is allowed for this model
    re.compile(r"['\"](\w+)['\"]\s+does not support"),   # 'top_p' does not support ...
)
# lightrag 场景默认温度（lightrag_manager.py:103 config.get("temperature", 0.2) 恒发——
# 用户配置段可能根本没有 temperature 键，探测必须含该值，否则测不到=验收失败）。
LIGHTRAG_DEFAULT_TEMPERATURE = 0.2

# ---------------------------------------------------------------------------
# vision 双色交叉子扫描常量（plan v0.5.2 §4-V1 / R7）
# ---------------------------------------------------------------------------
# max_tokens ≥500（R7：qwen 类 reasoning 模型 max_tokens<500 时思考占满预算、
# content 空回答——本地 qwen38-xl 实测 finish=length，看着像失败）。
VISION_MAX_TOKENS = 500
# vision 请求显式短 timeout（单次 ≤45s×2 次 attempt——视觉探测是附加子扫描，
# 不得拖长整体探测时长；与值域扫描"不传 timeout"的拍板互不冲突：那是等深度
# 思考真实响应，这里是给附加项设预算）。
VISION_TIMEOUT = 45
# 双色交叉测试图：纯红 RGB(255,0,0) + 纯蓝 RGB(0,0,255)，32×32 纯色 PNG data URI。
# 双色交叉（R1 P2-4）——单色问答可被"默认答某词"蒙混，两色全对才 supported=true。
_RED_RGB = (255, 0, 0)
_BLUE_RGB = (0, 0, 255)
# 色系命中词（小写子串匹配——中文色名 + 英文色名；"只回答颜色名"提示下
# 模型答 "红色"/"red"/"crimson" 等均须命中对应色系）。
VISION_RED_WORDS = ("红", "red", "赤", "crimson", "scarlet")
VISION_BLUE_WORDS = ("蓝", "blue", "azure")
VISION_QUESTION = "这张图片是什么颜色？只回答颜色名"


def default_profile_path() -> Path:
    """能力档案路径 ~/.niu/model_capabilities.json。"""
    return Path.home() / ".niu" / "model_capabilities.json"


# 模块级常量（让测试 monkeypatch 生效，对齐 niu_api/config.py CONFIG_PATH 模式）
PROFILE_PATH = default_profile_path()


def default_user_config_path() -> Path:
    """user-config.json 路径 ~/.niu/config/user-config.json（与 niu_api.config.CONFIG_PATH 同源）。"""
    return Path.home() / ".niu" / "config" / "user-config.json"


USER_CONFIG_PATH = default_user_config_path()


def default_named_configs_path() -> Path:
    """命名配置合集路径 ~/.niu/config/llm-configs.json（与 config-manager LLM_CONFIGS_PATH 同源）。"""
    return Path.home() / ".niu" / "config" / "llm-configs.json"


NAMED_CONFIGS_PATH = default_named_configs_path()


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
            _lock_nonblocking(lock_fd)
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
            _unlock(lock_fd)
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
        # 剔除 sticky 控制键（同 _strip_thinking_key 副本剔除模式）：sticky_session_headers 是
        # 程序侧三态控制键，非 litellm 参数——探测直发不得整体合并泄入请求参数。
        # thinking 由调用方经 _strip_thinking_key 预剔，此处仅剔 sticky_session_headers。
        params.update({k: v for k, v in litellm_kwargs.items() if k != "sticky_session_headers"})
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
        # 解析后 provider（审计 #7）：生产形态（raw_* 均 None）anthropic 路由剔
        # reasoning_effort；探测值域扫描/场景调用（raw_* 非 None）不受过滤（R9）
        provider=resolved_provider(api_base, model, api_type),
    ))
    return params


# ---------------------------------------------------------------------------
# 参数可用性探测段（plan 2026-09-10-param-deny-mechanism D3——deny 生产者）
# ---------------------------------------------------------------------------


def _collect_param_candidates(section: dict, lightrag: bool) -> dict:
    """参数可用性候选集 = 运行时实际发送集 ∩ deny 白名单（D3-1/D3-2）。

    收集方式=按运行时同一来源组装：
      - llm 场景：niu.md frontmatter temperature（get_subagent_config("niu")——
        runner.py:767 无条件覆盖源；用户配置段可能根本没有 temperature 键，主 Agent 仍恒发 0.6）。
        覆盖序=运行时镜像：段显式值先入候选，frontmatter 后覆盖（litellm_kwargs 最后覆盖不变）
      - lightrag 场景：lightrag_llm 段 + 默认 0.2 恒发（lightrag_manager.py:103）
      - 段显式参数（白名单交集；user-config 键已小写归一）
      - litellm_kwargs 键（白名单交集——生产经顶层合并送达）
      - max_tokens=PROBE_MAX_TOKENS（探测请求恒发——R2-A P2：既有段恒发 max_tokens，
        被拒时须可定位，否则以「非值域错误→failed」早退 deny 永不落盘）
    response_format/连接项/必需项不入候选（D3-2）。返回按白名单顺序的 dict。
    """
    candidates: dict = {}
    if lightrag:
        # lightrag 场景默认 0.2 恒发（config.get("temperature", 0.2)）——段显式值优先
        candidates["temperature"] = LIGHTRAG_DEFAULT_TEMPERATURE
    for k in DENY_CANDIDATE_WHITELIST:
        v = section.get(k)
        if v is not None:
            candidates[k] = v
    if not lightrag:
        # frontmatter temperature 无条件覆盖段显式值——镜像 runner.py:767 运行时覆盖序
        # （llm_config 先取段值，niu.md frontmatter 后覆盖）。候选集必须与运行时实际发送
        # 一致，否则用户把 llm.temperature 改为可接受值时探测仍发 0.6 → deny 漏写/误洗白。
        from agent.subagent import get_subagent_config  # 函数内解析（测试可 patch，避免顶层依赖环）
        try:
            fm_temperature = get_subagent_config("niu").get("temperature")
        except Exception:  # noqa: BLE001 - frontmatter 读取失败按无温度处理（段显式值仍入候选）
            fm_temperature = None
        if fm_temperature is not None:
            candidates["temperature"] = fm_temperature
    for k, v in (section.get("litellm_kwargs") or {}).items():
        if k in DENY_CANDIDATE_WHITELIST and v is not None:
            candidates[k] = v
    candidates["max_tokens"] = PROBE_MAX_TOKENS
    return {k: candidates[k] for k in DENY_CANDIDATE_WHITELIST if k in candidates}


def _build_param_availability_params(
    api_base: str, api_key: str, model: str, api_type: str, candidates: dict,
) -> dict:
    """参数可用性探测请求（D3-2：只发白名单候选集 + 基础参数）。

    刻意不合并 litellm_kwargs/reasoning_effort/thinking/extra_body——移除循环只能
    移除白名单参数，携带非候选参数会使 400 无法归因（D5 fail-closed）；「只发交集
    集」（R9）。max_tokens 取自 candidates（累积移除后无键——build_base_params
    None 不产键）。
    """
    params: dict = {
        **build_base_params(
            stream=False,
            max_tokens=candidates.get("max_tokens"),
            model=_derive_provider_prefix(api_base, model, api_type),
            api_base=api_base or None,
            api_key=api_key or None,
        ),
        "messages": PROBE_MESSAGE,
    }
    for k, v in candidates.items():
        if k != "max_tokens":
            params[k] = v
    return params


def _extract_denied_param_name(error_text: str, remaining: dict) -> str | None:
    """从 400 错误消息提取被拒参数名（D3-3①：已知格式，仅作候选）。

    提取出的名字须在当前候选集内才采信；未命中 → 回落白名单顺序取第一个剩余候选
    （逐个累积移除的确定性顺序）。返回 None = 候选清空（调用方按 D5 无法归因处理）。
    """
    for pattern in _DENIED_PARAM_PATTERNS:
        for m in pattern.finditer(error_text or ""):
            name = m.group(1)
            if name in remaining:
                return name
    for name in DENY_CANDIDATE_WHITELIST:
        if name in remaining:
            return name
    return None


def _probe_param_availability(
    api_base: str, api_key: str, model: str, api_type: str,
    section: dict, lightrag: bool, profile: dict,
) -> bool:
    """参数可用性探测段（D3）：候选集发真实请求 → 400 定位被拒参数 → 定位即写 deny。

    位置 = probe() 值域扫描之前（R2-A P2）。定位算法（定死线性 + 累积移除，R2-A）：
      ① 错误消息正则提取参数名（仅作候选——须在当前候选集内）；
      ② 未命中/移除后仍 400 → 白名单顺序逐个移除重试（每次只移除一个，先前移除的
         保持移除——累积语义，否则「连续两参数被拒」无法归因）。
    直至通过或候选清空。最坏 ≤8 次请求（1 首发 + ≤7 累积移除，R2 预算）。

    - 定位即写 = 通过时刻写（先于值域/thinking 等后续段，不等探测全程结束）：首个
      400 至通过之间被移除的全部参数进 deny（线性回退归因=保守超集 R3-A P3-2——
      可能包含先移除但实际被接受的参数；重探测全通过即洗白自愈）；同时只清本次
      实际测到且通过的参数（remaining=成功请求实际发送集）。已写 deny 后续段失败
      不回滚（R3-A P2-1——「探测 failed 但 deny 已正确落盘、运行时正常」是合法终态）。
    - 返回 False = failed 终止（定位前失败，R4 fail-closed 不写 deny 保持旧值）：
      非 400 错误（超时/401/404/5xx/网络）或候选清空后仍 400（D5：无法定位——
      本轮移除均未获通过确认，属未证实归因，不猜、不写 deny、原样报错）。
    """
    candidates = _collect_param_candidates(section, lightrag)
    remaining = dict(candidates)  # 当前发送集（累积移除：只减不增）
    removed: list[str] = []  # 首个 400 以来被移除的参数（保守超集归因，R3-A P3-2）
    while True:
        params = _build_param_availability_params(api_base, api_key, model, api_type, remaining)
        try:
            litellm.completion(**params)
            break  # 通过 → removed 集 = 定位出的被拒参数（定位即写）
        except Exception as e:  # noqa: BLE001 - 分类规则覆盖全部异常
            if _classify_value_domain_error(e, "param_availability") != "unsupported":
                # 非 400（超时/401/404/5xx/网络）→ 定位前失败 fail-closed（R4：不写 deny）
                profile["probe_status"] = "failed"
                profile["probe_fail_reason"] = f"参数可用性段: {_describe_fail_reason(e)}"
                return False
            name = _extract_denied_param_name(str(e), remaining)
            if name is None:
                # 候选清空仍 400 → 无法归因（D5：不猜、不写 deny、原样报错）
                profile["probe_status"] = "failed"
                profile["probe_fail_reason"] = f"参数被拒但无法定位到具体参数（400）: {str(e)[:120]}"
                return False
            del remaining[name]
            removed.append(name)
    # 通过 → 定位即写：removed 全进 deny + 只清本次测到且通过的（remaining）
    _write_param_deny(removed, list(remaining), model, lightrag)
    return True


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
# vision 双色交叉子扫描（plan v0.5.2 §4-V1）
# ---------------------------------------------------------------------------


def _solid_png_data_uri(rgb: tuple[int, int, int], size: int = 32) -> str:
    """纯色 PNG → base64 data URI（PIL 编码，32×32 极小图）。"""
    buf = BytesIO()
    Image.new("RGB", (size, size), rgb).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _vision_messages(data_uri: str) -> list[dict]:
    """多模态探测消息（OpenAI 形态 image_url data URI——litellm openai/ 路由原样透传）。"""
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": VISION_QUESTION},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ],
    }]


def _response_text(response) -> str:
    """稳健取响应文本（str content 与 list-of-parts 多模态形态均可；mock 同形）。"""
    message = _response_message(response)
    if message is None:
        return ""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            elif isinstance(getattr(part, "text", None), str):
                parts.append(part.text)
        return " ".join(parts)
    return ""


def _answer_matches_color(answer: str, words: tuple[str, ...]) -> bool:
    """色系命中判定（小写子串匹配）。"""
    text = (answer or "").lower()
    return any(w in text for w in words)


def _scan_vision(
    api_base: str, api_key: str, model: str, api_type: str, probe_config: dict,
) -> bool | None:
    """vision 双色交叉子扫描（仅 llm 场景调用——lightrag 不探）。

    三态返回（V8d）：
    - True：探测完成且有视觉（红答红系词且蓝答蓝系词）→ 写 input=["text","image"]
    - False：探测完成但无视觉（任一色未命中，短路）→ 写 input=["text"]
    - None：请求失败（异常/超时重试 1 次后仍失败/网络）或 200 但空回答
      （content None/空——reasoning 预算耗尽截断形态）→ **不写**（保持旧值——
      防网络抖动把已知视觉模型降级 text-only；空串恒未命中色系会被误判 False）
    不抛、不改 probe_status——vision 子扫描失败不得毒化主探测项结果（plan §4-V1）。
    max_tokens=VISION_MAX_TOKENS(≥500，R7 reasoning 占预算陷阱)；timeout=45s×≤2 attempt。
    """
    # 不注入场景 thinking（与值域/thinking 扫描不同）：纯色问答是轻量判定，
    # thinking 结论与其无关；且 thinking+reasoning_effort 组合在部分服务端 400
    # （豆包 high+disabled 实测），附加子扫描不应引入该耦合面。
    # config 副本仍剔除 thinking 键——_build_probe_params 顶层合并 litellm_kwargs，
    # 不剔则场景 thinking 会随顶层通道泄入 vision 请求（双源歧义同 R13）。
    probe_config_no_thinking = _strip_thinking_key(probe_config)
    for rgb, words in ((_RED_RGB, VISION_RED_WORDS), (_BLUE_RGB, VISION_BLUE_WORDS)):
        params = _build_probe_params(
            api_base, api_key, model, api_type, probe_config_no_thinking,
            messages=_vision_messages(_solid_png_data_uri(rgb)),
            timeout=VISION_TIMEOUT,
        )
        # max_tokens 覆盖：_build_probe_params 固定 PROBE_MAX_TOKENS=256，
        # vision 须 ≥500（R7）——参数组装后覆盖单键。
        params["max_tokens"] = VISION_MAX_TOKENS
        answer = ""
        request_failed = False
        for _attempt in range(2):  # 首次超时重试 1 次（同值域扫描 R18 模式）
            try:
                response = litellm.completion(**params)
                answer = _response_text(response)
                break
            except Exception as e:  # noqa: BLE001 - 任何失败 → None，不毒化主探测
                logger.info(
                    "[model_probe] vision 探测请求失败: rgb=%s attempt=%d error=%s %s",
                    rgb, _attempt + 1, type(e).__name__, str(e)[:200],
                )
                if not (_is_timeout_error(e) and _attempt == 0):
                    request_failed = True
                    break
        if request_failed:
            # V8d：请求失败（异常/超时重试后仍失败）→ None——不写 capabilities，
            # 保持旧值（防网络抖动把已知视觉模型降级 text-only）
            return None
        if not answer:
            # P2-1：200 但空回答（content None/空——reasoning 预算耗尽截断形态，
            # R7 陷阱在 max_tokens=500 下仍可能出现）→ 视同"探测未完成" → None
            # （V8d 不写语义，保持旧值）；色名判定仅在非空回答时进行——
            # 空串恒未命中色系会被误判 False，把已探测视觉模型降级 ["text"]
            logger.info(
                "[model_probe] vision 探测空回答: rgb=%s（视同请求失败，不写 capabilities）",
                rgb,
            )
            return None
        if not _answer_matches_color(answer, words):
            logger.info(
                "[model_probe] vision 探测未命中: rgb=%s answer=%r", rgb, answer[:80],
            )
            # V8d：探测完成且无视觉 → False（写 ["text"]）；
            # 双色交叉——单色已证伪，蓝图不再发（省一次调用）
            return False
    return True


def _atomic_write_json(path: Path, data: dict) -> bool:
    """原子 JSON 写（tempfile + os.replace——reader 永远看到完整文件）。

    失败 → log 并返回 False（不抛——vision 结果写入不得毒化主探测，同档案写纪律）。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:  # noqa: BLE001 - 写失败降级为跳过，不毒化主探测
        logger.warning("[model_probe] %s 写入失败: %s", path.name, e)
        return False
    return True


def _write_param_deny(rejected: list[str], passed: list[str], model: str, lightrag: bool) -> None:
    """参数可用性探测结果 → user-config.json 对应段 capabilities.deny + 命名配置同步（D2）。

    - 合并语义（防互抹）：只增删 capabilities.deny 键——对象已存在时保留
      input/probed_at/model；清空=只删 deny 键且**只清本次实际测到且通过的参数**
      （passed——不在本次发送集测不到的参数保留旧判定，R3 洗白规则）。无变化
      （new_deny == current）→ 仅跳过主配置写盘，命名配置同步照跑（防快照漂移）。
    - 新建对象规则（R2-B P1-a）：目标段 capabilities 不存在时新建必须写
      model=探测 model + probed_at——否则 fail-closed 绑定（capabilities.model ==
      当前 model）永不通过（lightrag_llm 段 capabilities 只能由 deny 写侧创建：
      vision 扫描被 `if not lightrag` 排除）。无 rejected 且无既有对象 → 不新建空壳。
    - 串模型守卫（R2-B P1-b，照 V8c 先例）：配置段有效 model ≠ 探测 model → 跳过
      写入 + log（防设置页测候选模型时把候选模型的 deny 错绑当前配置）。有效 model
      = 段自身 model；lightrag_llm 段无独立 model → 回落主 llm.model（与 get_llm_config
      / compat _inject_persisted_capabilities 继承语义一致）。
    - 双落点：user-config 对应段（llm/lightrag_llm 参数化）+ presetId 非空时
      _sync_named_config_snapshot 同步（复用既有原子 upsert）。
    - 永不抛：参数可用性段写入不得毒化主探测（同档案写纪律）。
    """
    path = Path(USER_CONFIG_PATH)
    if not path.exists():
        logger.info("[model_probe] user-config.json 不存在，跳过参数 deny 写入")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - 损坏不写坏文件
        logger.warning("[model_probe] user-config.json 读取失败，跳过参数 deny 写入: %s", e)
        return
    if not isinstance(data, dict):
        logger.warning("[model_probe] user-config.json 顶层非对象，跳过参数 deny 写入")
        return
    section_key = "lightrag_llm" if lightrag else "llm"
    section = data.get(section_key)
    if not isinstance(section, dict):
        logger.warning("[model_probe] user-config.json 无 %s 段，跳过参数 deny 写入", section_key)
        return

    # 串模型守卫（R2-B P1-b）：有效 model ≠ 探测 model → 跳过写入 + log
    sec_model = section.get("model")
    if lightrag and not sec_model:
        sec_model = (data.get("llm") or {}).get("model")
    if sec_model != model:
        logger.info(
            "[model_probe] 配置模型 %r ≠ 探测模型 %r，跳过参数 deny 写入（串模型守卫）",
            sec_model, model,
        )
        return

    caps = section.get("capabilities")
    unchanged = False
    if isinstance(caps, dict):
        # 对象已存在 → 只增删 deny 键（保留 input/probed_at/model——与 vision 写侧防互抹）
        raw_deny = caps.get("deny")
        current = [k for k in raw_deny if isinstance(k, str)] if isinstance(raw_deny, list) else []
        new_deny = [k for k in current if k not in passed]  # 清空：只清本次测到且通过的
        for p in rejected:
            if p not in new_deny:
                new_deny.append(p)
        if new_deny == current:
            unchanged = True  # 无变化 → 仅跳过主配置写盘；命名配置同步照跑（防快照漂移）
        elif new_deny:
            caps["deny"] = new_deny
        else:
            caps.pop("deny", None)
    else:
        # 新建对象规则（R2-B P1-a）：必须写 model + probed_at
        if not rejected:
            return  # 无拒绝且无既有对象 → 无可写内容，不新建空壳
        section["capabilities"] = {
            "model": model,
            "probed_at": datetime.now().isoformat(timespec="seconds"),
            "deny": list(rejected),
        }

    if not unchanged and not _atomic_write_json(path, data):
        return  # 主配置写失败 → 不同步命名配置（同 vision 写纪律）

    name = (data.get("llm") or {}).get("presetId", "")
    if name:
        _sync_named_config_snapshot(name, data)


def _write_vision_capabilities(supported: bool, model: str) -> None:
    """vision 探测结果落 user-config.json llm 段 capabilities 子对象 + 同步命名配置。

    - 文件不存在 → 跳过（无模型配置无意义）；JSON 损坏/无 llm 段 → log 跳过（不写坏文件）。
    - V8c 串模型防护：llm.model ≠ 本次探测 model → 跳过写入 + log（防测候选/第三方
      模型时顶掉当前模型 capabilities）。
    - 只改 llm.capabilities 单键（merge：保留既有 deny 键——plan D2 防互抹）——
      其他段/字段原样保留；原子写（tempfile + os.replace）。
    - capabilities.model 记探测时的模型名：读侧比对 model 一致才采信（防换模型后旧能力误判）。
    - 命名配置同步：llm.presetId 非空 → upsert llm-configs.json 该条目两段快照
      （llm/lightrag_llm；vision_llm 恒在 user-config.json 顶层，不入合集——
      与 config-manager _sync_named_config 同源）；主配置写失败 → 不同步（防快照与主配置分叉）。
    - 永不抛：vision 结果写入不得毒化主探测（同档案写纪律）。
    """
    path = Path(USER_CONFIG_PATH)
    if not path.exists():
        logger.info("[model_probe] user-config.json 不存在，跳过 vision capabilities 写入")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - 损坏不写坏文件
        logger.warning("[model_probe] user-config.json 读取失败，跳过 vision capabilities 写入: %s", e)
        return
    if not isinstance(data, dict):
        logger.warning("[model_probe] user-config.json 顶层非对象，跳过 vision capabilities 写入")
        return
    llm = data.get("llm")
    if not isinstance(llm, dict):
        logger.warning("[model_probe] user-config.json 无 llm 段，跳过 vision capabilities 写入")
        return

    # V8c 串模型防护：读到的当前模型与本次探测 model 不一致（设置页测候选模型/
    # CLI 测第三方模型期间配置被切换）→ 跳过写入 + log，防顶掉当前模型 capabilities
    if llm.get("model") != model:
        logger.info(
            "[model_probe] 配置模型 %r ≠ 探测模型 %r，跳过 vision capabilities 写入（串模型防护）",
            llm.get("model"), model,
        )
        return

    # merge 语义（plan D2 / R7）：保留既有 deny 键——整对象替换会与参数可用性段
    # deny 写侧互抹（R1 双审同抓）。旧档案无 deny 键时 merge 与替换等价，无回归。
    caps = {
        "model": model,
        "input": ["text", "image"] if supported else ["text"],
        "probed_at": datetime.now().isoformat(timespec="seconds"),
    }
    existing_caps = llm.get("capabilities")
    if isinstance(existing_caps, dict) and isinstance(existing_caps.get("deny"), list):
        caps["deny"] = [k for k in existing_caps["deny"] if isinstance(k, str)]
    llm["capabilities"] = caps
    if not _atomic_write_json(path, data):
        return  # 主配置写失败 → 不同步命名配置

    name = llm.get("presetId", "")
    if name:
        _sync_named_config_snapshot(name, data)


def _sync_named_config_snapshot(name: str, user_data: dict) -> None:
    """upsert 命名配置条目（llm/lightrag_llm 两段快照——vision_llm 恒在
    user-config.json 顶层，不入合集；与 config-manager _sync_named_config 同款
    原子 upsert——复制其逻辑，不跨包 import 私有函数）。

    写时刻重读合集单条 upsert；文件不存在 = 空合集；JSON 损坏/configs 非对象 →
    跳过同步（防"损坏=空合集"整体覆写销毁全部条目）。永不抛。
    """
    path = Path(NAMED_CONFIGS_PATH)
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            configs = raw.get("configs", {})
            if not isinstance(configs, dict):
                raise ValueError(f"配置合集文件损坏: configs 应为对象，实际为 {type(configs).__name__}")
        else:
            configs = {}
    except Exception as e:  # noqa: BLE001 - 损坏跳过同步（原坏文件保留不写）
        logger.warning("[model_probe] llm-configs.json 损坏，跳过命名配置同步: %s", e)
        return
    configs[name] = {
        "llm": user_data.get("llm", {}),
        "lightrag_llm": user_data.get("lightrag_llm", {}),
    }
    _atomic_write_json(path, {"configs": configs})


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

    # 参数可用性探测段（plan 2026-09-10-param-deny-mechanism D3）：位置=值域扫描之前
    # （R2-A P2——既有段恒发 max_tokens=256，被拒时若本段在其后则 failed 早退、deny
    # 永不落盘）；定位即写 deny（已写不回滚——后续段失败亦保留 R3-A P2-1）；
    # 定位前失败 → failed 终止 fail-closed（R4：不写 deny 保持旧值）。
    if not _probe_param_availability(api_base, api_key, model, api_type, section, lightrag, profile):
        return profile  # failed——不落盘（旧档保留）

    if not _scan_reasoning_effort(api_base, api_key, model, api_type, probe_config, profile):
        return profile  # failed——不落盘（旧档保留）

    # thinking 探测（response_format/tools 不在此测——无档案消费点，且 rf 的
    # 探测归属"测试连接并保存"按钮的 testAndSave 流程；用户拍板 2026-08-18）
    if not _scan_thinking(api_base, api_key, model, api_type, probe_config, profile):
        return profile  # thinking failed——不落盘（旧档保留）

    # vision 双色交叉子扫描（plan v0.5.2 §4-V1 / v0.6 V8d：仅主 llm 场景——lightrag 不探、
    # 不写 capabilities；三态落 user-config.json llm 段——True→["text","image"] /
    # False（完成且无视觉）→["text"] 覆盖陈旧 ["text","image"] / None（请求失败）→
    # 不写保持旧值；不毒化 probe_status——主探测项结果不受 vision 子扫描影响）
    if not lightrag:
        vision_supported = _scan_vision(api_base, api_key, model, api_type, probe_config)
        if vision_supported is not None:
            _write_vision_capabilities(vision_supported, model)

    if profile["probe_status"] != "failed":
        write_profile(profile, lightrag=lightrag, profile_path=profile_path)
    return profile
