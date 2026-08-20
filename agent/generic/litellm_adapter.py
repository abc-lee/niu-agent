"""
LiteLLM Adapter Module

LiteLLM统一适配器，提供与现有BaseSession/ToolClient接口兼容的LiteLLM封装。
支持100+ LLM提供商，统一响应格式。
"""

import json
import logging
import os
import re
import sys
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import Any

# 在导入 litellm 之前设置环境变量，避免远程获取 model cost map 和 aiohttp 初始化开销
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_NO_AIOHTTP_TRANSPORT", "True")

import litellm

from agent.runner import is_stop_requested

logger = logging.getLogger(__name__)

# 截断标记：模型侧截断（finish_reason="length"）时追加到 content 末尾
TRUNCATION_MARKER = "[输出因超过最大长度被自动截断，内容不完整。请基于以上不完整内容，缩短后重新输出完整内容。]"

# 抑制 LiteLLM 的调试输出（"Provider List" 等提示）
litellm.suppress_debug_info = True


def _register_model_cost(model: str):
    """将模型注册到 litellm.model_cost 并置零，避免查找失败触发 Provider List 警告"""
    if model and model.lower() not in litellm.model_cost:
        litellm.model_cost[model.lower()] = {"input_cost_per_token": 0, "output_cost_per_token": 0}

from .http_logger import install_http_logger  # noqa: E402
from .llmcore import BaseSession, MockResponse, MockToolCall, ToolClient  # noqa: E402

install_http_logger()

# === 上下文溢出统一检测 ===

_OVERFLOW_PATTERNS = [
    "context_length_exceeded",
    "maximum context length",
    "prompt is too long",
    "prompt: length",
    "exceed context limit",
    "is longer than the model's context length",
    "input tokens exceed the configured limit",
    "exceeds the maximum number of tokens",
    "input is too long",
    "context window exceeded",
]


def _is_context_overflow_error(exc: Exception) -> bool:
    """三层检测：isinstance > HTTP 413 > 字符串匹配"""
    # Layer 1: litellm ContextWindowExceededError
    try:
        from litellm import ContextWindowExceededError
        if isinstance(exc, ContextWindowExceededError):
            return True
    except ImportError:
        pass

    # Layer 2: HTTP 413
    status_code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status_code == 413:
        return True

    # Layer 3: 字符串模式匹配
    msg = str(exc).lower()
    return any(p in msg for p in _OVERFLOW_PATTERNS)


# === LLM 错误分类（流式错误重试/标记机制） ===
# getattr 默认值用 None（不是 Exception），避免缺失时 isinstance 匹配所有异常
try:
    import litellm
    _RETRYABLE_EXC = tuple(x for x in (
        litellm.APIConnectionError,
        litellm.Timeout,
        litellm.RateLimitError,
    ) if x is not None)
    _FATAL_EXC = tuple(x for x in (
        litellm.AuthenticationError,
        getattr(litellm, 'PermissionDeniedError', None),
        getattr(litellm, 'BudgetExceededError', None),
        getattr(litellm, 'ContentPolicyViolationError', None),
    ) if x is not None)
    _UNCERTAIN_EXC = tuple(x for x in (
        litellm.InternalServerError,
        litellm.ServiceUnavailableError,
        getattr(litellm, 'BadGatewayError', None),
    ) if x is not None)
except (ImportError, AttributeError):
    _RETRYABLE_EXC = ()
    _FATAL_EXC = ()
    _UNCERTAIN_EXC = ()


def _classify_stream_error(e) -> str:
    """分类流式错误。返回 'retryable' | 'fatal' | 'uncertain'。"""
    # 1. 字符串匹配兜底（优先，确保未验证类型也能分类）
    type_name = type(e).__name__
    if 'MidStreamFallback' in type_name:
        return 'retryable'
    # 2. isinstance 检查
    if _FATAL_EXC and isinstance(e, _FATAL_EXC):
        return 'fatal'
    if _UNCERTAIN_EXC and isinstance(e, _UNCERTAIN_EXC):
        return 'uncertain'
    if _RETRYABLE_EXC and isinstance(e, _RETRYABLE_EXC):
        return 'retryable'
    # 3. 默认归入 retryable（未知错误给重试机会）
    return 'retryable'

def _sanitize_error_msg(msg: str) -> str:
    """脱敏错误信息中的敏感字段。"""
    # 脱敏 API key（key=xxx, api_key=xxx, apikey=xxx）
    msg = re.sub(r'(key|api_key|apikey)\s*[=:]\s*\S+', r'\1=***', msg, flags=re.IGNORECASE)
    msg = re.sub(r'Bearer\s+\S+', 'Bearer ***', msg, flags=re.IGNORECASE)
    return msg


# === E2 LLM 错误友好文案（纯函数——三层识别通道 + 原文保底，任何输入返回非空字符串、绝不抛异常） ===

# 错误原文截断上限（复用 E1 _TOOL_ERROR_MAX 模式——防超长错误刷屏）
_LLM_ERROR_MAX = 500
# 截断时尾部保留长度——保尾（类型/关键信息常在尾部）
_LLM_ERROR_TAIL = 100

# 标准 LLM 错误 → 中文友好文案（通道 1 翻译）。映射依赖显式 error_type ①——
# LiteLLMUnknownProvider 的 str() 含基类名 BadRequestError，子串②会误映射，故只经显式类型命中。
# 其余类型走识别通道 2/3。
# 注：InvalidRequestError 为 litellm 1.88.1 DEPRECATED 死类（全包零 raise 点）——不收录；
# APIError/InternalServerError 有意不在映射表——500/未映射码经 exception_type() 归入 APIError
# （兜底类）→ 通道 2 原文展示（用户看到 500 英文原文属预期行为，非翻译缺失）。
_LLM_ERROR_FRIENDLY: dict[str, str] = {
    "RateLimitError": "模型服务限流（429），请稍后重试",
    "ServiceUnavailableError": "模型服务暂不可用（503），请稍后重试",
    "AuthenticationError": "模型认证失败（401），请检查 API Key 配置",
    "NotFoundError": "模型或服务不存在（404），请检查模型配置",
    "BadRequestError": "模型请求被拒绝（400），请检查请求参数",
    "LiteLLMUnknownProvider": "模型服务商配置错误，请检查模型名/服务商设置",
    "APIConnectionError": "无法连接模型服务，请检查网络",
    "Timeout": "模型响应超时，请稍后重试",
    "BudgetExceededError": "模型配额已用完，请等待配额恢复或更换模型",
    "MidStreamFallbackError": "模型流式响应中断，请稍后重试",
}


def _truncate_error_text(text: str) -> str:
    """原文截断保尾 ≤500：前 _LLM_ERROR_MAX-(len('...')+_LLM_ERROR_TAIL) + '...' + 尾 _LLM_ERROR_TAIL。"""
    if len(text) > _LLM_ERROR_MAX:
        return text[: _LLM_ERROR_MAX - (len("...") + _LLM_ERROR_TAIL)] + "..." + text[-_LLM_ERROR_TAIL:]
    return text


def _extract_error_type_from_text(text: str) -> str | None:
    """从错误文本提取类型名（format 内部二级提取 ②+③，与 extract_error_type 同源）。

    ② 映射表键名子串匹配——真实 litellm 异常 str() 带模块前缀 "litellm.RateLimitError: ..."，
       锚定正则跨不了 '.'，须子串匹配；优先匹配冒号前类型段，冒号后消息段仅兜底——
       降低长消息内嵌其他键名的跨错误误映射；亦覆盖 "Timeout" 这类无 Error 后缀键。
    ③ 通用正则 r'([A-Za-z]+Error)'（去 ^ 锚定）兜底——提取不到返回 None。
    """
    if not text:
        return None
    # ② 映射表键名子串匹配：冒号前类型段优先
    prefix = text.split(":", 1)[0]
    for key in _LLM_ERROR_FRIENDLY:
        if key in prefix:
            return key
    # 冒号后消息段兜底
    for key in _LLM_ERROR_FRIENDLY:
        if key in text:
            return key
    # ③ 通用正则兜底
    m = re.search(r'([A-Za-z]+Error)', text)
    if m:
        return m.group(1)
    return None


def extract_error_type(error_msg) -> str | None:
    """从错误消息提取 LLM 错误类型名（独立导出，与 format_llm_error_for_user 内部二级提取同源）。

    notify 站点统一调用，防实施者自写不一致。任何输入不抛异常（str 强转失败返回 None）。
    """
    try:
        raw = str(error_msg or '')
    except Exception:
        return None
    return _extract_error_type_from_text(_sanitize_error_msg(raw))


def format_llm_error_for_user(error_msg: str, error_type: str | None = None) -> str:
    """把 LLM 错误转用户友好文案。三层识别通道，任何输入都返回非空字符串（绝不抛异常）。

    处理顺序：先 str(error_msg or '') 强转（坏 __str__ → <unprintable> 内层 try/except 兜底）
    → 再过 _sanitize_error_msg 脱敏（key=/api_key=/Bearer 等敏感字段——用户可见错误文本
    必须脱敏，LLM_ERROR 路径的 error_msg 虽已脱敏，幂等无害）→ 提取类型 → 生成文案。

    error_type 提取顺序（三级）：
      ① 显式 error_type（except 子句有异常对象处直接传 type(e).__name__；LLM_ERROR 路径
         透传 MockResponse.error_type_name——无模块前缀，命中 BudgetExceededError 等
         str() 无类型信息特例）
      ② 映射表键名子串匹配（见 _extract_error_type_from_text）
      ③ 通用正则 r'([A-Za-z]+Error)'（去 ^ 锚定）兜底——提取不到返回 None。
    error_type 按真值判定：空串 "" 与 None 统一（都触发文本提取、都走通道 3 保底）——
    消除"模型调用失败（）：…"悬空空名（空 error_type 若走通道 2 会渲染空括号）。

    三层通道：
      通道 1（标准错误）：error_type 在 _LLM_ERROR_FRIENDLY 映射表命中 → 中文翻译文案。
      通道 2（非标准但可识别类型）：error_type 不在映射表 → 类型名 + 错误原文
        （原文为空时省略冒号——无悬空冒号）。
      通道 3（原文保底）：error_type 为空/None（未显式给定且提取不到）→ 裸原文展示（无前缀）；
        空/None 输入 → "模型调用失败"（保底不变式——error_type 为空时统一走通道 3，
        非空时按通道 1/2）。
    原文一律截断保尾 ≤500（复用 E1 _TOOL_ERROR_MAX 模式）。

    幂等性（full_reply 不重复 format 的理由）：通道 3 输出再 format 幂等（裸原文原样返回）；
    通道 2 输出再 format 会双包——"模型调用失败（X）："中的 X 会被③正则提取 → 二次包装。
    源头友好化后（full_reply）已是友好文案，任何再 format 都有双包风险（通道 2），
    调用方不得对 full_reply 重复 format。
    """
    # 1. str 强转（坏 __str__ → 内层 try/except 兜底 <unprintable>）
    try:
        raw = str(error_msg or '')
    except Exception:
        raw = "<unprintable>"
    # 2. 脱敏（幂等）
    sanitized = _sanitize_error_msg(raw)
    # 3. 三级类型提取（①显式参数优先；空/None 时走 ②③）
    if not error_type:
        error_type = _extract_error_type_from_text(sanitized)
    # 4. 原文截断保尾 ≤500
    truncated = _truncate_error_text(sanitized)
    # 5. 三层通道文案（error_type 真值判定：空串与 None 统一走通道 3 保底）
    if error_type:
        friendly = _LLM_ERROR_FRIENDLY.get(error_type)
        if friendly is not None:
            # 通道 1：标准错误翻译
            return friendly
        # 通道 2：非标准类型 → 类型名 + 原文（原文为空时省略冒号）
        if truncated:
            return f"模型调用失败（{error_type}）：{truncated}"
        return f"模型调用失败（{error_type}）"
    # 通道 3：裸原文保底（无前缀）；空/None 输入 → "模型调用失败"
    if truncated:
        return truncated
    return "模型调用失败"


def is_litellm_error_type(type_name: str) -> bool:
    """判断异常类型名是否为 litellm 异常类。

    getattr(litellm, type_name) / getattr(litellm.exceptions, type_name) 双模块动态判定——
    litellm 顶层导出含 RateLimitError/BudgetExceededError/LiteLLMUnknownProvider 等；
    litellm.exceptions 子模块含全部异常类（MidStreamFallbackError 等仅子模块有）——
    天然覆盖静态名单漏类 + 未来新增类。与纯 hasattr 的区别：取到属性后校验
    isinstance(cls, type) and issubclass(cls, BaseException) 确认为异常类——
    "exceptions"（子模块）/ "model_cost"（dict 数据）等非类属性 hasattr 也为 True，
    不校验会误判；ValueError/KeyError 等内部异常因非 litellm 属性自动排除。
    """
    if not isinstance(type_name, str) or not type_name:
        return False
    for mod in (litellm, litellm.exceptions):
        cls = getattr(mod, type_name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            return True
    return False


# 敏感字段名匹配：api_key / apiKey / apikey / authorization / secret / token（大小写不敏感）。
# token 需整词匹配（前后为非字母数字），避免误伤 prompt_tokens / total_tokens 等
# token 计数字段和 tokenizer 之类的普通词。
_SENSITIVE_KEY_RE = re.compile(
    r"(^|[^a-z0-9])(api[_-]?key|authorization|secret|token)([^a-z0-9]|$)",
    re.IGNORECASE,
)


def _is_sensitive_key(key: str) -> bool:
    """判断字段名是否携带敏感信息（api_key / authorization / token / secret 等）。"""
    return bool(_SENSITIVE_KEY_RE.search(key))


def _mask_api_key_value(value: Any) -> Any:
    """脱敏单个密钥字符串：sk-abcdef...xyz -> sk-ab...yz（保留前4后3）。

    参照 http_logger._mask_api_key 风格；None / 空串 / 非字符串值原样返回，
    避免破坏日志结构（如 api_key=None 时仍记录为空）。
    """
    if not isinstance(value, str) or not value:
        return value
    if len(value) <= 8:
        return "***"
    return value[:4] + "..." + value[-3:]


def _mask_sensitive(raw: Any) -> Any:
    """递归脱敏 dict/list 中携带敏感字段名的值（api_key/authorization/token/secret 等）。

    纯函数：不修改入参，返回脱敏后的新结构。用于 _write_raw_log 落盘前，
    避免明文 API key 写入 ~/.niu/logs/raw_http/*_request.json。
    """
    if isinstance(raw, dict):
        return {
            k: (_mask_sensitive(v) if isinstance(v, (dict, list)) else _mask_api_key_value(v))
            if _is_sensitive_key(k)
            else _mask_sensitive(v)
            for k, v in raw.items()
        }
    if isinstance(raw, list):
        return [_mask_sensitive(item) for item in raw]
    return raw


# 完整无截断的原始日志序号计数器
_raw_seq_counter = 0


def _get_app_log_dir() -> Path:
    """获取应用层日志根目录 ~/.niu/logs/，便于测试 monkeypatch。

    litellm_adapter 原用 Path(__file__).parent.parent.parent / "logs" 绝对路径
    （见 _write_raw_log 和 _write_interaction_log），chdir 无效。
    抽出此函数让测试可拦截。带 resolve() 与 gateway.py 的 _get_gateway_log_dir 保持一致。
    """
    import os
    home = os.path.expanduser("~")
    return Path(home) / ".niu" / "logs"


def _write_raw_log(log_type: str, data: dict, seq: int | None = None) -> None:
    """写入完整无截断的原始日志到 JSON 文件。

    与 _write_interaction_log（人类可读、有截断）互补，
    记录完整的 request/response 数据用于排查底层问题。
    落盘前经 _mask_sensitive 脱敏 api_key / authorization / token / secret 等敏感字段，
    避免明文密钥写入 raw_http 日志。

    Args:
        log_type: "request" 或 "response"
        data: 日志数据
        seq: 可选的序号。如果传入，使用该序号（同一LLM调用的request/response共享）；
             如果不传，从计数器取并递增。
    """
    global _raw_seq_counter
    try:
        from niu_api.config import get_logging_config
        if not get_logging_config().enabled:
            return  # 静默跳过
        log_dir = _get_app_log_dir() / "raw_http" / datetime.now().strftime("%Y%m%d")
        log_dir.mkdir(parents=True, exist_ok=True)
        if seq is None:
            seq = _raw_seq_counter
            _raw_seq_counter += 1
        filepath = log_dir / f"{seq:06d}_{log_type}.json"
        filepath.write_text(
            json.dumps(_mask_sensitive(data), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[LiteLLM] Failed to write raw log: {e}", file=sys.stderr, flush=True)


def _write_interaction_log(log_entry: dict[str, Any]):
    """
    写入 LLM 交互日志（人类可读格式）

    格式示例：
    ========== 19:25:28 [MiniMax-M2.7-highspeed] ==========
    [系统提示词]
    # Role: 妞妞...
    ...
    [用户输入]
    用户拖入了以下文件...
    [可用工具]
    - lightrag-server/lightrag_query
    - photo-server/ingest_photo
    ...
    [AI回复]
    好的，老板！...
    [工具调用]
    - chat-with-file-processor({"task": "入库照片：..."})
    [思考链]
    <thinking>...</thinking>
    """
    try:
        from niu_api.config import get_logging_config
        if not get_logging_config().enabled:
            return  # 静默跳过
        log_dir = _get_app_log_dir()
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"llm_interaction_{datetime.now().strftime('%Y%m%d')}.log"

        with open(log_file, "a", encoding="utf-8") as f:
            if log_entry["type"] == "request":
                _format_request_log(f, log_entry)
            elif log_entry["type"] == "response_complete":
                _format_response_log(f, log_entry)
    except Exception as e:
        print(f"[LiteLLM] Failed to write log: {e}", file=sys.stderr, flush=True)


def _format_request_log(f, log_entry: dict[str, Any]):
    """格式化请求日志（简练但不缺内容）"""
    ts = log_entry.get("timestamp", "")
    model = log_entry.get("model", "")
    messages = log_entry.get("messages", [])
    tools = log_entry.get("tools", [])

    # 分隔线
    f.write(f"\n{'=' * 60}\n")
    f.write(f"[{ts}] {model}\n")
    f.write(f"{'=' * 60}\n")

    # 系统提示词（完整记录，包含动态注入的历史参考消息和工具描述）
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            f.write(f"[系统提示词]\n{content}\n\n")
            break

    # 历史对话（记录完整上下文）
    history_msgs = [m for m in messages if m.get("role") in ("user", "assistant", "tool")]
    if len(history_msgs) > 1:  # 有历史消息
        f.write(f"[历史对话]（共{len(history_msgs)-1}条历史消息）\n")
        # 记录最近10条历史（排除当前输入）
        recent_history = history_msgs[-11:-1] if len(history_msgs) > 11 else history_msgs[:-1]
        for i, msg in enumerate(recent_history, 1):
            role = msg.get("role", "")
            content = msg.get("content", "")

            # 每条消息最多200字
            if len(content) > 200:
                content = content[:200] + "..."

            # 标记消息类型
            if role == "user":
                f.write(f"{i}. 👤 {content}\n")
            elif role == "assistant":
                f.write(f"{i}. 🤖 {content}\n")
            elif role == "tool":
                tool_name = msg.get("name", "tool")
                f.write(f"{i}. 🔧 [{tool_name}] {content}\n")
        f.write("\n")

    # 当前用户输入（完整记录）
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if len(content) > 400:
                content = content[:400] + "\n...（已截断）"
            f.write(f"[用户输入]\n{content}\n\n")
            break

    # 可用工具（只列名称）
    if tools:
        tool_names = []
        for t in tools:
            if "function" in t:
                name = t["function"].get("name", "?")
            elif "name" in t:
                name = t["name"]
            else:
                name = str(t)[:40]
            tool_names.append(name)
        f.write("[可用工具]\n")
        for name in tool_names:
            f.write(f"  - {name}\n")
        f.write("\n")


def _format_response_log(f, log_entry: dict[str, Any]):
    """格式化响应日志"""
    content = log_entry.get("content", "")
    tool_calls = log_entry.get("tool_calls", [])
    thinking = log_entry.get("thinking", "")
    usage = log_entry.get("usage")

    # AI回复
    if content:
        if len(content) > 600:
            content = content[:600] + "\n...（已截断）"
        f.write(f"[AI回复]\n{content}\n\n")

    # 思考链
    if thinking:
        th = thinking if len(thinking) <= 400 else thinking[:400] + "\n...（已截断）"
        f.write(f"[思考链]\n{th}\n\n")

    # 工具调用
    if tool_calls:
        f.write("[工具调用]\n")
        for tc in tool_calls:
            name = tc.get("name", "?")
            args = tc.get("arguments", {})
            args_str = json.dumps(args, ensure_ascii=False)
            # 截断太长的参数
            if len(args_str) > 200:
                args_str = args_str[:200] + "...}"
            f.write(f"  - {name}({args_str})\n")
        f.write("\n")

    # Token使用量
    if usage:
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        tt = usage.get("total_tokens", 0)
        f.write(f"[Token] prompt={pt} completion={ct} total={tt}\n")

    f.write("\n")


def _derive_provider_prefix(api_base: str | None, model: str, api_type: str | None = None) -> str:
    """根据 apiBase + api_type 自动推导 LiteLLM provider 前缀，加到 model 名上。

    Why: 豆包网关对 openai 路由的 response_format 请求挂起不响应（json_schema/json_object
    都挂起），必须走 volcengine 路由才正常。custom_llm_provider 参数对豆包无效（实测卡死），
    只有 model 前缀 'volcengine/...' 才能让 LiteLLM 走对路由。

    推导优先级：
    1. volces.com 域名 → volcengine/（豆包专属，最高优先级，域名是火山引擎官方）
    2. api.anthropic.com 域名 → anthropic/（Anthropic 官方）
    3. api_type="anthropic" → anthropic/（第三方网关走 anthropic 协议，如 minimax-anthropic）
    4. 默认 → openai/（OpenAI 兼容路由，含 api.openai.com/xf-yun/自定义网关/localhost Ollama）

    通用性：从 apiBase 推导 provider 是标准做法（curl/httpx 都这么做），
    volcengine/openai/anthropic 是 LiteLLM 内置 provider 名，不是豆包特定 hack。

    Args:
        api_base: 用户配置的 apiBase URL
        model: 用户配置的 model 名（不带前缀）
        api_type: 可选，用户配置的 type（"openai"/"anthropic"），用于第三方网关走 anthropic 协议

    Returns:
        带 provider 前缀的 model 名（如 'volcengine/ark-code-latest'）
    """
    api_base_lower = (api_base or "").lower()
    # 1. 域名匹配优先（volces.com 是火山引擎官方域名，必须走 volcengine 路由）
    if "volces.com" in api_base_lower:
        return f"volcengine/{model}"
    if "api.anthropic.com" in api_base_lower:
        return f"anthropic/{model}"
    # 2. api_type=anthropic 覆盖（第三方网关走 anthropic 协议，如 minimax-anthropic 预设）
    if api_type == "anthropic":
        return f"anthropic/{model}"
    # 3. 默认 openai 兼容路由（api.openai.com、xf-yun、自定义网关、localhost Ollama）
    return f"openai/{model}"


def get_provider_params(model: str) -> dict[str, Any]:
    """获取提供商特定参数。

    注：reasoning_effort 不再经此函数进顶层参数——统一由 assemble_request_params
    注入 extra_body 送达（litellm 白名单碰不到，任何模型任何路由同一行为）。
    """
    params: dict[str, Any] = {}
    model_lower = model.lower()

    # Claude: 启用prompt caching
    if "claude" in model_lower:
        params["extra_headers"] = {"anthropic-beta": "prompt-caching-2024-07-31"}

    return params


def _convert_tools_schema(tools: list | None, model: str = "") -> list | None:
    """
    将工具schema转换为LiteLLM格式（OpenAI格式）。

    Claude 模型在最后一个 tool 打 cache_control breakpoint，
    让 tools 也命中 prompt cache（tools_schema 稳定，每轮不变）。
    """
    if not tools:
        return None

    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue

        if "type" in tool and "function" in tool:
            converted.append(tool)
        elif "name" in tool and "input_schema" in tool:
            converted.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool["input_schema"],
                }
            })
        elif tool.get("type") == "function":
            converted.append(tool)
        elif "name" in tool and "parameters" in tool:
            converted.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool["parameters"],
                }
            })

    if not converted:
        return None

    # Claude: 最后一个 tool 打 cache_control breakpoint
    # tools_schema 每轮稳定（base + static MCP + disk），可安全 cache
    model_lower = (model or "").lower()
    if "claude" in model_lower:
        converted[-1] = {**converted[-1], "cache_control": {"type": "ephemeral"}}

    return converted


def build_base_params(stream=True, max_tokens=None, timeout=None, **overrides) -> dict:
    """共享基础参数组装（探测直发与生产 chat 共用一份，杜绝两份漂移）。

    - 产出 stream/stream_options/temperature/proxies/api_base/api_key/timeout/
      extra_headers 等基础字段；None 值不产键（探测形态 max_tokens=8/timeout=10
      显式传入才有对应键，生产缺省则无）
    - extra_headers 来自 get_provider_params（prompt caching L565-568 保留），
      由调用方经 **overrides 传入
    - 探测用 build_base_params(stream=False, max_tokens=8, timeout=10) + 前缀推导
      model + 固定消息；生产用 build_base_params(stream=True) + chat 调用态字段
    """
    params: dict[str, Any] = {
        "stream": stream,
        "stream_options": {"include_usage": True},
    }
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    if timeout is not None:
        params["timeout"] = timeout
    for key, value in overrides.items():
        if value is not None:
            params[key] = value
    return params


def assemble_request_params(
    config: dict,
    raw_reasoning_effort: str | None = None,
    raw_thinking: dict | None = None,
) -> dict:
    """组装 extra_body/drop_params 增量——探测与生产同源参数注入（组件 3 契约）。

    返回**仅含 extra_body/drop_params 键**的增量 dict（不含 messages/model/stream
    等调用态字段——由 chat()/探测器在增量上补齐）。合并用法：
    request_params = {**build_base_params(...), **assemble_request_params(config)}

    - raw_reasoning_effort 非 None（探测）：该值无条件注入 extra_body
      （绕过 none 排除与 llmcore 归一化——测服务端对 none/xhigh 的真实值域）
    - raw_thinking 非 None（探测 thinking 候选）：注入 extra_body.thinking（dict 形态）
    - 均 None（生产）：reasoning_effort 从 config 读归一值（排除 "none"——none 不注入，
      语义由 thinking disabled 表达——豆包/zen none 400 实测）；thinking 从
      config.litellm_kwargs 读
    - extra_body 合并：用户已有 extra_body 键优先（{**注入, **用户}）——注入值不整块丢失
    - drop_params：raw 非 None / litellm_kwargs 非空 → True（触发时才返回该 key，
      不触发不含——避免增量恒 False 覆盖 chat() 对 response_format 的置 True）
    """
    litellm_kwargs = config.get("litellm_kwargs") or {}
    injected_extra: dict[str, Any] = {}

    effort = raw_reasoning_effort if raw_reasoning_effort is not None else config.get("reasoning_effort")
    if effort and (raw_reasoning_effort is not None or effort != "none"):
        injected_extra["reasoning_effort"] = effort

    thinking_val = raw_thinking if raw_thinking is not None else litellm_kwargs.get("thinking")
    if thinking_val:
        injected_extra["thinking"] = thinking_val

    result: dict[str, Any] = {}
    if injected_extra:
        user_extra_body = config.get("extra_body") or litellm_kwargs.get("extra_body") or {}
        result["extra_body"] = {**injected_extra, **user_extra_body}
    if raw_reasoning_effort is not None or raw_thinking is not None or litellm_kwargs:
        result["drop_params"] = True
    return result


class LiteLLMSession(BaseSession):
    """
    LiteLLM适配器Session

    提供与BaseSession接口兼容的LiteLLM封装。
    使用LiteLLM统一调用不同提供商的LLM。
    """

    def __init__(self, cfg):
        super().__init__(cfg)
        self.api_type = cfg.get("api_type", "openai")
        self.provider = cfg.get("provider", "")
        # max_tokens 顶层字段并入 litellm_kwargs——chat() 经 request_params.update(self.litellm_kwargs) 送达。
        # kwargs 已有 max_tokens 则 kwargs 优先：压缩（compat 程序预算）/探测（256/50）在 config 层注入 kwargs，
        # 不被用户顶层配置覆盖；用户 kwargs 显式配置同样优先于顶层字段（兼容存量配置）。
        mt = cfg.get("max_tokens")
        if mt is not None and "max_tokens" not in self.litellm_kwargs:
            self.litellm_kwargs = {**self.litellm_kwargs, "max_tokens": mt}
        # stop 检查回调：默认全局停止标志（主 Agent），call-time 解析模块全局（测试 monkeypatch 有效）；
        # 子 Agent 由 call_subagent 按来源覆盖属性（同步 user=全局 or terminate；异步 user/program/scheduler=仅 terminate）
        self.stop_check = self._default_stop_check

    def _default_stop_check(self):
        """默认 stop 检查：call-time 解析模块全局 is_stop_requested（monkeypatch 生效）。"""
        return is_stop_requested()

    def _do_streaming_completion(self, response):
        """消费流式响应（generator）。不调 litellm.completion()。

        litellm.completion() 保留在 chat() 中，初始调用错误保持 raise 行为不变。
        _do_streaming_completion 只负责流式消费循环，接收 response 对象。

        Yields:
            str: 流式内容增量
        Returns:
            tuple(content, thinking, tool_calls, finish_reason, usage, was_stopped)
        Raises:
            Exception: 流式传输中的任何异常（由调用方捕获分类）
        """
        full_content = ""
        reasoning_content = ""
        tool_calls: list[MockToolCall] = []
        usage = None
        last_finish_reason = None
        was_stopped = False
        tool_calls_accumulator: dict[int, dict] = {}
        chunk_count = 0

        for chunk in self._interruptible_iter(response):
            chunk_count += 1
            # 协作式停止：每个 chunk 后检查，发现停止立即中断流式生成
            if self.stop_check():
                was_stopped = True
                break
            if hasattr(chunk, 'choices') and chunk.choices:
                choice = chunk.choices[0]
                delta = getattr(choice, 'delta', None)
                if delta:
                    if hasattr(delta, 'content') and delta.content:
                        full_content += delta.content
                        yield delta.content
                    if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                        reasoning_content += delta.reasoning_content
                    if hasattr(delta, 'tool_calls') and delta.tool_calls:
                        for tc in delta.tool_calls:
                            # 获取index（流式响应中同一个tool_call的多个chunk共享同一个index）
                            idx = getattr(tc, 'index', len(tool_calls_accumulator))
                            if idx not in tool_calls_accumulator:
                                tool_calls_accumulator[idx] = {
                                    'id': getattr(tc, 'id', None) or f"call_{idx}",
                                    'name': '',
                                    'arguments': ''
                                }
                            # 累积数据（增量更新）
                            if hasattr(tc, 'id') and tc.id:
                                tool_calls_accumulator[idx]['id'] = tc.id
                            if hasattr(tc, 'function') and tc.function:
                                if hasattr(tc.function, 'name') and tc.function.name:
                                    tool_calls_accumulator[idx]['name'] = tc.function.name
                                if hasattr(tc.function, 'arguments') and tc.function.arguments:
                                    tool_calls_accumulator[idx]['arguments'] += tc.function.arguments
                if hasattr(choice, 'finish_reason') and choice.finish_reason:
                    last_finish_reason = choice.finish_reason
            if hasattr(chunk, 'usage') and chunk.usage:
                usage = chunk.usage

        # 循环后重新检查 is_stop_requested
        was_stopped = was_stopped or self.stop_check()

        # 循环后处理：tool_calls JSON 解析
        for idx in sorted(tool_calls_accumulator.keys()):
            tc_data = tool_calls_accumulator[idx]
            tc_name = tc_data['name']
            # 跳过空工具名
            if not tc_name or not tc_name.strip():
                continue
            tc_args_raw = tc_data['arguments']
            tc_args = {}
            if was_stopped:
                # 停止中断时，跳过不完整 JSON
                try:
                    tc_args = json.loads(tc_args_raw) if tc_args_raw else {}
                except json.JSONDecodeError:
                    continue
            else:
                if isinstance(tc_args_raw, dict):
                    tc_args = tc_args_raw
                elif isinstance(tc_args_raw, str) and tc_args_raw:
                    try:
                        tc_args = json.loads(tc_args_raw)
                    except json.JSONDecodeError:
                        tc_args = {}
                else:
                    tc_args = {}
            tool_calls.append(MockToolCall(
                name=tc_name,
                args=tc_args,
                id=str(tc_data['id']),
            ))

        return (full_content, reasoning_content, tool_calls, last_finish_reason, usage, was_stopped)

    def _interruptible_iter(self, response):
        """可中断的流式迭代：后台线程推进 response，前台轮询 stop_check。

        解决协作式停止在"LLM 连接挂起无 chunk"时失效的问题——stop 条件任何时刻
        置位，前台 ≤0.2s 内打断，不依赖底层是否吐 chunk。

        注意：litellm CustomStreamWrapper 无同步 close()（仅 aclose），stop 后后台
        线程无法立即断开，靠 q.put timeout 退出循环 + daemon 线程兜底（最多挂到
        httpx read_timeout 由底层释放）。
        """
        import queue as _queue
        import threading as _threading

        q = _queue.Queue(maxsize=2)
        pull_stop = _threading.Event()

        def _pull():
            try:
                for chunk in response:
                    if pull_stop.is_set():
                        break
                    try:
                        q.put(("chunk", chunk), timeout=1.0)
                    except _queue.Full:
                        # 下游消费方短暂停滞：重试（与 done/error 一致），
                        # pull_stop 由循环顶检查兜底，消费方恢复后正常送达
                        while not pull_stop.is_set():
                            try:
                                q.put(("chunk", chunk), timeout=1.0)
                                break
                            except _queue.Full:
                                continue
                        if pull_stop.is_set():
                            break
                while not pull_stop.is_set():
                    try:
                        q.put(("done", None), timeout=1.0)
                        break
                    except _queue.Full:
                        continue
            except BaseException as e:  # noqa: BLE001 - 流式异常原样上抛（含 KeyboardInterrupt 转队列错误，可接受）
                # 先无条件尝试一次：stop 与流错误竞态时 error 不能被吞（P3）
                try:
                    q.put(("error", e), timeout=1.0)
                except _queue.Full:
                    while not pull_stop.is_set():
                        try:
                            q.put(("error", e), timeout=1.0)
                            break
                        except _queue.Full:
                            continue

        t = _threading.Thread(target=_pull, daemon=True, name="llm-stream-pull")
        t.start()
        try:
            while True:
                try:
                    kind, payload = q.get(timeout=0.2)
                except _queue.Empty:
                    if self.stop_check():
                        pull_stop.set()
                        break
                    continue
                if kind == "chunk":
                    yield payload
                elif kind == "error":
                    raise payload
                else:  # done
                    break
        finally:
            pull_stop.set()

    def chat(
        self,
        messages: list,
        tools: list | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> Generator[str, None, MockResponse]:
        """
        原生 LiteLLM 调用（Generator版本）。

        Yields:
            文本块（用于流式显示）
            <tool_use> 标签块
        Returns:
            MockResponse（通过 StopIteration）
        """
        # 从 apiBase 自动推导 LiteLLM provider 前缀，加到 model 名上。
        # Why: 豆包网关对 openai 路由的 response_format 请求挂起不响应（json_schema/json_object 都挂起），
        # 必须走 volcengine 路由才正常。custom_llm_provider 参数对豆包无效（实测卡死），
        # 只有 model 前缀 'volcengine/...' 才能让 LiteLLM 走对路由。
        # 通用性：从 apiBase 推导 provider 是标准做法（curl/httpx 都这么做），
        # volcengine/openai/anthropic 是 LiteLLM 内置 provider 名，不是豆包特定 hack。
        # 用户传的 self.provider 字段已废弃（页面下拉框将删除），保留兼容但优先用 apiBase 推导。
        # custom_llm_provider 不再传——model 前缀已决定路由，同时传会冲突
        # （实测 model='volcengine/ark-code-latest' + custom_llm_provider='openai' 会被
        # custom_llm_provider 覆盖走 openai 路由，豆包网关报 NotFoundError）。
        custom_provider = self.provider or ("anthropic" if self.api_type == "anthropic" else "openai")
        model_with_prefix = _derive_provider_prefix(self.api_base, self.default_model, self.api_type)
        # 仅保留 extra_headers（prompt caching L565-568）；reasoning_effort 不再进顶层参数——
        # 统一经 assemble_request_params 走 extra_body 送达（litellm 白名单碰不到，消除双通道冗余）
        provider_params = get_provider_params(self.default_model)
        litellm_tools = _convert_tools_schema(tools, self.default_model)

        # 基础组装（共享 build_base_params——探测直发与生产 chat 共用一份，杜绝两份漂移）
        request_params: dict[str, Any] = {
            **build_base_params(
                stream=True,
                timeout=self.read_timeout,
                model=model_with_prefix,
                api_base=self.api_base or None,
                api_key=self.api_key or None,
                temperature=self.temperature,
                proxies=self.proxies,
                **provider_params,
            ),
            "messages": messages,
        }
        if response_format is not None:
            request_params["response_format"] = response_format
            request_params["drop_params"] = True
        # 用户自定义 litellm_kwargs（如 thinking 等模型特定参数）非空时也启用 drop_params，
        # 让 LiteLLM 自动丢弃不支持的参数（如 OpenAI 路由下的 thinking），避免 UnsupportedParamsError。
        # 通用修复：不针对任何特定模型，未来任何模型特定参数都能自动适配。
        if self.litellm_kwargs:
            request_params.update(self.litellm_kwargs)
            request_params["drop_params"] = True
        if litellm_tools:
            request_params["tools"] = litellm_tools
        # 参数注入增量（extra_body 统一送达 + drop_params 决策）——在 litellm_kwargs.update
        # 之后：extra_body 合并语义 {**注入, **用户} 用户键优先，注入值不因用户 extra_body
        # 存在而整块丢失；drop_params 仅在触发时返回（增量不含该键时不覆盖调用点置位）
        request_params.update(assemble_request_params({
            "reasoning_effort": self.reasoning_effort,
            "litellm_kwargs": self.litellm_kwargs,
        }))

        # 记录完整请求（全量，包含 messages）
        _write_interaction_log({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "request",
            "model": self.default_model,
            "provider": custom_provider,
            "messages": messages,  # 完整 messages
            "tools": tools,        # 完整 tools schema
            "provider_params": provider_params if provider_params else None
        })

        # 获取原始日志序号（同一LLM调用的request/response共享）
        global _raw_seq_counter
        raw_log_seq = _raw_seq_counter
        _raw_seq_counter += 1

        # 记录完整无截断的原始请求
        _write_raw_log("request", {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "model": self.default_model,
            "provider": custom_provider,
            "messages": messages,
            "tools": tools,
            "provider_params": provider_params,
            "request_params": {k: v for k, v in request_params.items() if k not in ("messages", "tools")},
        }, seq=raw_log_seq)

        try:
            # R5-P1：TTFT（请求发送 + 响应头等待）同步阻塞——包可中断层，stop 置位放弃等待
            # （后台线程继续跑，结果丢弃；返回空 MockResponse → agent_loop L1058 stop 检查 STOPPED）
            from agent.generic.interruptible import run_interruptibly as _ri
            _ok, response = _ri(
                lambda: litellm.completion(**request_params),
                self.stop_check,
            )
            if not _ok:
                logger.info("[STREAM] Stop requested during initial call (TTFT), aborting")
                return MockResponse(
                    thinking="", content="", tool_calls=[], raw="",
                    finish_reason="stop", stream_error=False,
                )
        except Exception as init_err:
            # 初始 API 调用就失败（如 context_length_exceeded），直接返回 MockResponse
            if _is_context_overflow_error(init_err):
                logger.warning(f"[STREAM] Context length exceeded on initial call: {init_err}")
                return MockResponse(
                    thinking="",
                    content="",
                    tool_calls=[],
                    raw="",
                    context_overflow=True,
                    usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                )
            # 非 context overflow 错误，重新抛出
            raise

        full_content = ""
        reasoning_content = ""
        tool_calls: list[MockToolCall] = []
        usage = None
        last_finish_reason = None  # 捕获流式最后一个非空 finish_reason
        was_stopped = False
        _stream_error_occurred = False
        _stream_error_msg = ""
        _stream_error_type = None
        _stream_error_type_name = None  # E2：异常类名透传（error_type 内部类别不动——llm_proxy 消费 error_type="retry_exhausted" 等保持）

        try:
            full_content, reasoning_content, tool_calls, last_finish_reason, usage, was_stopped = \
                yield from self._do_streaming_completion(response)
        except Exception as e:
            _stream_error_occurred = True
            _stream_error_msg = _sanitize_error_msg(str(e))
            _stream_error_type_name = type(e).__name__
            error_msg = str(e)

            # 检测 context_length_exceeded 错误
            if _is_context_overflow_error(e):
                logger.warning(f"[STREAM] Context length exceeded: {e}")
                return MockResponse(
                    thinking=reasoning_content or "",
                    content=full_content or "",
                    tool_calls=tool_calls,
                    raw=full_content or "",
                    context_overflow=True,
                    usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    finish_reason=last_finish_reason or "stop",
                )

            is_socket_error = "10038" in error_msg or "10054" in error_msg or "non-socket" in error_msg.lower()

            if is_socket_error and not full_content:
                # Windows socket error with empty content → non-stream fallback
                logger.warning(f"[STREAM] Socket error with empty content, trying non-stream fallback: {e}")
                try:
                    fallback_params = {**request_params, "stream": False}
                    _ok_f, fallback_response = _ri(
                        lambda: litellm.completion(**fallback_params),
                        self.stop_check,
                    )
                    if not _ok_f:
                        # fallback 等待中 stop 置位：放弃，返回已积累内容（stream_error=False → L1058 STOPPED）
                        logger.info("[STREAM] Stop requested during socket fallback, aborting")
                        return MockResponse(
                            thinking=reasoning_content or "",
                            content=full_content or "",
                            tool_calls=tool_calls,
                            raw=full_content or "",
                            finish_reason=last_finish_reason or "stop",
                            stream_error=False,
                        )
                    if fallback_response and fallback_response.choices:
                        choice = fallback_response.choices[0]
                        full_content = choice.message.content or ""
                        if full_content:
                            yield full_content
                        if hasattr(choice.message, "reasoning_content") and choice.message.reasoning_content:
                            reasoning_content = choice.message.reasoning_content
                        if hasattr(choice.message, "tool_calls") and choice.message.tool_calls:
                            for tc in choice.message.tool_calls:
                                tc_args = {}
                                if hasattr(tc, "function") and tc.function:
                                    if hasattr(tc.function, "arguments") and tc.function.arguments:
                                        try:
                                            tc_args = json.loads(tc.function.arguments)
                                        except json.JSONDecodeError:
                                            tc_args = {}
                                    tool_calls.append(MockToolCall(
                                        name=getattr(tc.function, "name", ""),
                                        args=tc_args,
                                        id=getattr(tc, "id", f"call_fallback_{len(tool_calls)}"),
                                    ))
                        if hasattr(fallback_response, "usage") and fallback_response.usage:
                            usage = fallback_response.usage
                        if hasattr(choice, "finish_reason") and choice.finish_reason:
                            last_finish_reason = choice.finish_reason
                        _stream_error_occurred = False
                        _stream_error_msg = ""
                        logger.info(f"[STREAM] Non-stream fallback succeeded ({len(full_content)} chars, {len(tool_calls)} tool_calls)")
                except Exception as fb_err:
                    logger.error(f"[STREAM] Non-stream fallback also failed: {fb_err}")
                    _stream_error_type = "retry_exhausted"
                    _stream_error_msg = _sanitize_error_msg(str(fb_err))
                    _stream_error_type_name = type(fb_err).__name__
            else:
                # 其他错误 → 分类 + 重试
                logger.error(f"[STREAM] Stream error: {e}")
                error_type = _classify_stream_error(e)
                if error_type == "fatal":
                    logger.warning(f"[STREAM] Fatal error ({type(e).__name__}), no retry")
                    _stream_error_type = "fatal"
                else:
                    max_retries = 3 if error_type == "retryable" else 2
                    retry_succeeded = False
                    for retry_idx in range(1, max_retries + 1):
                        if self.stop_check():
                            logger.info("[STREAM] Stop requested, aborting retry")
                            _stream_error_type = "stopped"
                            break
                        logger.info(f"[STREAM] Retry {retry_idx}/{max_retries} for {type(e).__name__}")
                        try:
                            _ok_r, retry_response = _ri(
                                lambda: litellm.completion(**request_params),
                                self.stop_check,
                            )
                            if not _ok_r:
                                # 重试中 stop 置位：放弃（结果丢弃），返回已积累内容
                                logger.info("[STREAM] Stop requested during retry call, aborting")
                                return MockResponse(
                                    thinking=reasoning_content or "",
                                    content=full_content or "",
                                    tool_calls=tool_calls,
                                    raw=full_content or "",
                                    finish_reason=last_finish_reason or "stop",
                                    stream_error=False,
                                )
                            full_content, reasoning_content, tool_calls, \
                                last_finish_reason, usage, was_stopped = \
                                yield from self._do_streaming_completion(retry_response)
                            _stream_error_occurred = False
                            _stream_error_msg = ""
                            retry_succeeded = True
                            logger.info(f"[STREAM] Retry {retry_idx} succeeded ({len(full_content)} chars)")
                            break
                        except Exception as retry_e:
                            if _is_context_overflow_error(retry_e):
                                logger.warning(f"[STREAM] Retry hit context_overflow, stopping")
                                return MockResponse(
                                    thinking=reasoning_content or "",
                                    content=full_content or "",
                                    tool_calls=tool_calls,
                                    raw=full_content or "",
                                    context_overflow=True,
                                    finish_reason=last_finish_reason or "stop",
                                )
                            logger.error(f"[STREAM] Retry {retry_idx} failed: {retry_e}")
                            _stream_error_msg = _sanitize_error_msg(str(retry_e))
                            _stream_error_type_name = type(retry_e).__name__
                    if not retry_succeeded:
                        if _stream_error_type is None:
                            _stream_error_type = "retry_exhausted"
                        logger.error(f"[STREAM] All {max_retries} retries exhausted")

        # === 截断标记注入 ===
        # A1: finish_reason="length" — 模型侧截断
        if last_finish_reason == "length" and full_content:
            marker = "\n\n" + TRUNCATION_MARKER
            full_content += marker
            yield marker

        # A5: thinking chain 截断
        if last_finish_reason == "length" and reasoning_content:
            reasoning_content += "\n\n[思考链因超长被自动截断]"

        # A2: 用户 stop 中断（排除 A1 已处理的 length 截断场景）
        if was_stopped and full_content and last_finish_reason != "length":
            marker = "\n\n[输出被用户中断，内容不完整。]"
            full_content += marker
            yield marker

        mock_resp = MockResponse(
            thinking=reasoning_content,
            content=full_content,
            tool_calls=tool_calls,
            raw=full_content,
            finish_reason=last_finish_reason or "stop",
            stream_error=_stream_error_occurred,
            error_type=_stream_error_type,
            error_msg=_stream_error_msg or None,
            error_type_name=_stream_error_type_name,
        )

        if usage:
            mock_resp.usage = {
                "prompt_tokens": getattr(usage, 'prompt_tokens', 0) or 0,
                "completion_tokens": getattr(usage, 'completion_tokens', 0) or 0,
                "total_tokens": getattr(usage, 'total_tokens', 0) or 0,
            }

        # 记录完整响应
        _write_interaction_log({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "response_complete",
            "model": self.default_model,
            "thinking": reasoning_content,  # 完整思考链
            "content": full_content,        # 完整内容
            "tool_calls": [
                {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments  # 完整参数
                }
                for tc in tool_calls
            ] if tool_calls else [],
            "usage": mock_resp.usage if hasattr(mock_resp, 'usage') else None,
            "finish_reason": mock_resp.finish_reason,
        })

        # 记录完整无截断的原始响应
        _write_raw_log("response", {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "model": self.default_model,
            "thinking": reasoning_content,
            "content": full_content,
            "tool_calls": [
                {"name": tc.function.name, "arguments": tc.function.arguments}
                for tc in tool_calls
            ] if tool_calls else [],
            "usage": mock_resp.usage if hasattr(mock_resp, 'usage') else None,
            "finish_reason": mock_resp.finish_reason,
        }, seq=raw_log_seq)

        return mock_resp


def create_litellm_client(config: dict[str, Any]) -> ToolClient:
    """
    创建LiteLLM客户端的便捷函数

    Args:
        config: LLM配置字典，包含apiKey, model, apiBase, type等字段

    Returns:
        配置好的LiteLLMToolClient实例
    """
    api_type = config.get("api_type", config.get("type", "openai"))
    api_base = config.get("apiBase") or config.get("api_base") or config.get("apibase")
    api_key = config.get("apiKey") or config.get("apikey", "")

    cfg = {
        "apikey": api_key,
        "apibase": api_base or "",
        "model": config.get("model", "gpt-4o"),
        "api_type": api_type,
    }
    if "temperature" in config and config["temperature"] is not None:
        cfg["temperature"] = config["temperature"]
    if "reasoning_effort" in config and config["reasoning_effort"] is not None:
        cfg["reasoning_effort"] = config["reasoning_effort"]
    if config.get("max_tokens") is not None:
        cfg["max_tokens"] = config["max_tokens"]
    cfg["provider"] = config.get("provider", "")
    cfg["litellm_kwargs"] = config.get("litellm_kwargs", {})
    cfg["read_timeout"] = config.get("read_timeout") or 300

    # 将当前模型注册到 cost map（置零），避免 LiteLLM 查找费率失败触发 Provider List
    _register_model_cost(cfg["model"])

    session = LiteLLMSession(cfg)
    return ToolClient(session)
