"""
LiteLLM Adapter Module

LiteLLM统一适配器，提供与现有BaseSession/ToolClient接口兼容的LiteLLM封装。
支持100+ LLM提供商，统一响应格式。
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

# 在导入 litellm 之前设置环境变量，避免远程获取 model cost map 和 aiohttp 初始化开销
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_NO_AIOHTTP_TRANSPORT", "True")

import litellm

from agent.runner import is_stop_requested

logger = logging.getLogger(__name__)

# 抑制 LiteLLM 的调试输出（"Provider List" 等提示）
litellm.suppress_debug_info = True


def _register_model_cost(model: str):
    """将模型注册到 litellm.model_cost 并置零，避免查找失败触发 Provider List 警告"""
    if model and model.lower() not in litellm.model_cost:
        litellm.model_cost[model.lower()] = {"input_cost_per_token": 0, "output_cost_per_token": 0}

from .llmcore import BaseSession, MockResponse, MockToolCall, ToolClient
from .http_logger import install_http_logger

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


# 完整无截断的原始日志序号计数器
_raw_seq_counter = 0


def _write_raw_log(log_type: str, data: dict, seq: Optional[int] = None) -> None:
    """写入完整无截断的原始日志到 JSON 文件。

    与 _write_interaction_log（人类可读、有截断）互补，
    记录完整的 request/response 数据用于排查底层问题。

    Args:
        log_type: "request" 或 "response"
        data: 日志数据
        seq: 可选的序号。如果传入，使用该序号（同一LLM调用的request/response共享）；
             如果不传，从计数器取并递增。
    """
    global _raw_seq_counter
    try:
        log_dir = Path(__file__).parent.parent.parent / "logs" / "raw_http" / datetime.now().strftime("%Y%m%d")
        log_dir.mkdir(parents=True, exist_ok=True)
        if seq is None:
            seq = _raw_seq_counter
            _raw_seq_counter += 1
        filepath = log_dir / f"{seq:06d}_{log_type}.json"
        filepath.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[LiteLLM] Failed to write raw log: {e}", file=sys.stderr, flush=True)


def _write_interaction_log(log_entry: Dict[str, Any]):
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
        log_dir = Path(__file__).parent.parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"llm_interaction_{datetime.now().strftime('%Y%m%d')}.log"

        with open(log_file, "a", encoding="utf-8") as f:
            if log_entry["type"] == "request":
                _format_request_log(f, log_entry)
            elif log_entry["type"] == "response_complete":
                _format_response_log(f, log_entry)
    except Exception as e:
        print(f"[LiteLLM] Failed to write log: {e}", file=sys.stderr, flush=True)


def _format_request_log(f, log_entry: Dict[str, Any]):
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
        f.write(f"[可用工具]\n")
        for name in tool_names:
            f.write(f"  - {name}\n")
        f.write("\n")


def _format_response_log(f, log_entry: Dict[str, Any]):
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
        f.write(f"[工具调用]\n")
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


def get_provider_params(model: str, reasoning_effort: Optional[str] = None) -> Dict[str, Any]:
    """获取提供商特定参数"""
    params: Dict[str, Any] = {}
    model_lower = model.lower()

    # Claude: 启用prompt caching
    if "claude" in model_lower:
        params["extra_headers"] = {"anthropic-beta": "prompt-caching-2024-07-31"}

    # reasoning_effort: 支持 OpenAI o-series, DeepSeek, 火山方舟等模型
    # LiteLLM 将此参数作为 OpenAI 标准参数传递；不支持该参数的模型会被 LiteLLM 的 drop_params 忽略
    if reasoning_effort:
        params["reasoning_effort"] = reasoning_effort

    return params


def _convert_tools_schema(tools: Optional[List]) -> Optional[List]:
    """
    将工具schema转换为LiteLLM格式（OpenAI格式）。
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

    return converted if converted else None


class LiteLLMSession(BaseSession):
    """
    LiteLLM适配器Session

    提供与BaseSession接口兼容的LiteLLM封装。
    使用LiteLLM统一调用不同提供商的LLM。
    """

    def __init__(self, cfg):
        super().__init__(cfg)
        self.api_type = cfg.get("api_type", "openai")

    def chat(
        self,
        messages: List,
        tools: Optional[List] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> Generator[str, None, MockResponse]:
        """
        原生 LiteLLM 调用（Generator版本）。

        Yields:
            文本块（用于流式显示）
            <tool_use> 标签块
        Returns:
            MockResponse（通过 StopIteration）
        """
        custom_provider = getattr(self, 'api_type', 'openai')
        provider_params = get_provider_params(self.default_model, getattr(self, 'reasoning_effort', None))
        litellm_tools = _convert_tools_schema(tools)

        request_params: Dict[str, Any] = {
            "model": self.default_model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "custom_llm_provider": custom_provider,
            "api_base": self.api_base or None,
            "api_key": self.api_key or None,
            "timeout": 120,  # 120s timeout to prevent indefinite blocking
            **provider_params,
        }
        # Only drop unsupported params when passing reasoning_effort
        # (e.g., some models don't support this OpenAI extension parameter)
        if provider_params.get("reasoning_effort"):
            request_params["drop_params"] = True
        if response_format is not None:
            request_params["drop_params"] = True
        if litellm_tools:
            request_params["tools"] = litellm_tools
        if self.proxies:
            request_params["proxy"] = self.proxies
        if self.temperature is not None:
            request_params["temperature"] = self.temperature
        if response_format is not None:
            request_params["response_format"] = response_format

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
            response = litellm.completion(**request_params)
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
        tool_calls: List[MockToolCall] = []
        usage = None

        try:
            chunk_count = 0
            # 用于累积tool_calls的增量数据（按index分组）
            tool_calls_accumulator: Dict[int, Dict[str, Any]] = {}

            for chunk in response:
                chunk_count += 1

                # 协作式停止：每个 chunk 后检查，发现停止立即中断流式生成
                if is_stop_requested():
                    logger.info("[LLM] Stop requested, breaking stream")
                    break

                delta = getattr(chunk, 'choices', [None])[0].delta if hasattr(chunk, 'choices') else None
                if not delta:
                    continue

                if hasattr(delta, 'content') and delta.content:
                    full_content += delta.content
                    yield delta.content

                if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                    reasoning_content += delta.reasoning_content

                if hasattr(delta, 'tool_calls') and delta.tool_calls:
                    for tc in delta.tool_calls:
                        # 获取index（流式响应中同一个tool_call的多个chunk共享同一个index）
                        tc_index = getattr(tc, 'index', len(tool_calls_accumulator))

                        # 初始化或更新累积器
                        if tc_index not in tool_calls_accumulator:
                            tool_calls_accumulator[tc_index] = {
                                'id': getattr(tc, 'id', None) or f"call_{tc_index}",
                                'name': '',
                                'arguments': ''
                            }

                        # 累积数据（增量更新）
                        # id 只有第一个 chunk 有
                        if hasattr(tc, 'id') and tc.id:
                            tool_calls_accumulator[tc_index]['id'] = tc.id

                        if hasattr(tc, 'function') and tc.function:
                            fn = tc.function
                            # name 只有第一个 chunk 有，不要用 None 覆盖已有 name
                            if hasattr(fn, 'name') and fn.name:
                                tool_calls_accumulator[tc_index]['name'] = fn.name
                            # arguments 每个 chunk 都累加
                            if hasattr(fn, 'arguments') and fn.arguments:
                                tool_calls_accumulator[tc_index]['arguments'] += fn.arguments

                if hasattr(chunk, 'usage') and chunk.usage:
                    usage = chunk.usage

            # 处理累积完成后的tool_calls
            was_stopped = is_stop_requested()
            for idx in sorted(tool_calls_accumulator.keys()):
                tc_data = tool_calls_accumulator[idx]
                tc_name = tc_data['name']
                tc_args_raw = tc_data['arguments'] or "{}"

                # 停止中断时，跳过不完整的 tool_calls（arguments 未结束的 JSON）
                if was_stopped:
                    try:
                        json.loads(tc_args_raw)
                    except json.JSONDecodeError:
                        logger.debug(f"[LLM] Skipping incomplete tool_call due to stop: {tc_name}")
                        continue

                # 跳过空工具名（MiniMax会把一个tool_call拆成多个chunk，只有name的chunk有name）
                if not tc_name or tc_name.strip() == "":
                    continue

                if isinstance(tc_args_raw, str):
                    try:
                        tc_args = json.loads(tc_args_raw)
                    except json.JSONDecodeError:
                        tc_args = {}
                elif isinstance(tc_args_raw, dict):
                    tc_args = tc_args_raw
                else:
                    tc_args = {}

                tool_calls.append(MockToolCall(
                    name=tc_name,
                    args=tc_args,
                    id=str(tc_data['id']),
                ))

                # [已注释] MiniMax 工具调用走 tool_calls 字段，不走这个 yield。
                # 这个 yield 会输出 <tool_use> 文本到流式响应中，被误当成对话内容输出到界面。
                # 如果 MiniMax 正确使用 tool_calls，本行不需要任何 yield 输出。
                # yield f'<tool_use>{{"id": "{tc_data["id"]}", "name": "{tc_name}", "arguments": {args_str}}}</tool_use>'

        except Exception as e:
            error_msg = str(e)

            # 检测 context_length_exceeded 错误 — 设置标记让 agent_loop 触发强制压缩
            if _is_context_overflow_error(e):
                logger.warning(f"[STREAM] Context length exceeded: {e}")
                return MockResponse(
                    thinking=reasoning_content or "",
                    content=full_content or "",
                    tool_calls=tool_calls,
                    raw=full_content or "",
                    context_overflow=True,
                    usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                )

            is_socket_error = "10038" in error_msg or "10054" in error_msg or "non-socket" in error_msg.lower()

            if is_socket_error and not full_content:
                # WinError 10038/10054: Windows socket 在流式传输中被关闭，尝试非流式 fallback
                logger.warning(f"[STREAM] Socket error with empty content, trying non-stream fallback: {e}")
                try:
                    fallback_params = {**request_params, "stream": False}
                    fallback_response = litellm.completion(**fallback_params)
                    if fallback_response and fallback_response.choices:
                        choice = fallback_response.choices[0]
                        full_content = choice.message.content or ""
                        if full_content:
                            yield full_content
                        if hasattr(choice.message, "reasoning_content") and choice.message.reasoning_content:
                            reasoning_content = choice.message.reasoning_content
                        # 提取 tool_calls（非流式响应直接在 message 上）
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
                        logger.info(f"[STREAM] Non-stream fallback succeeded ({len(full_content)} chars, {len(tool_calls)} tool_calls)")
                except Exception as fb_err:
                    logger.error(f"[STREAM] Non-stream fallback also failed: {fb_err}")
            else:
                logger.error(f"[STREAM] Stream error: {e}")
                if full_content:
                    logger.warning(f"[STREAM] Using partial content ({len(full_content)} chars)")

        mock_resp = MockResponse(
            thinking=reasoning_content,
            content=full_content,
            tool_calls=tool_calls,
            raw=full_content,
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
            "usage": mock_resp.usage if hasattr(mock_resp, 'usage') else None
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
        }, seq=raw_log_seq)

        return mock_resp


def create_litellm_client(config: Dict[str, Any]) -> ToolClient:
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

    # 将当前模型注册到 cost map（置零），避免 LiteLLM 查找费率失败触发 Provider List
    _register_model_cost(cfg["model"])

    session = LiteLLMSession(cfg)
    return ToolClient(session)
