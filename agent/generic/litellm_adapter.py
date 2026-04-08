"""
LiteLLM Adapter Module

LiteLLM统一适配器，提供与现有BaseSession/ToolClient接口兼容的LiteLLM封装。
支持100+ LLM提供商，统一响应格式。
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

import litellm

from .llmcore import BaseSession, MockResponse, MockToolCall, ToolClient


def _write_interaction_log(log_entry: Dict[str, Any]):
    """
    写入 LLM 交互日志（格式化 JSON）

    Args:
        log_entry: 日志条目（字典格式）
    """
    try:
        # 确定日志目录
        log_dir = Path(__file__).parent.parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)

        # 日志文件名：llm_interaction_YYYYMMDD.log
        log_file = log_dir / f"llm_interaction_{datetime.now().strftime('%Y%m%d')}.log"

        # 写入格式化 JSON（带换行和缩进）
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False, indent=2))
            f.write("\n\n")  # 每个条目之间空两行
    except Exception as e:
        print(f"[LiteLLM] Failed to write log: {e}", file=sys.stderr, flush=True)


def get_provider_params(model: str, reasoning_effort: Optional[str] = None) -> Dict[str, Any]:
    """获取提供商特定参数"""
    params: Dict[str, Any] = {}
    model_lower = model.lower()

    # MiniMax: 禁用 reasoning_split（导致tool calling参数丢失）
    # if "minimax" in model_lower:
    #     params["extra_body"] = {"reasoning_split": True}

    # Claude: 启用prompt caching
    if "claude" in model_lower:
        params["extra_headers"] = {"anthropic-beta": "prompt-caching-2024-07-31"}

    # DeepSeek: 用户配置的 reasoning_effort
    if "deepseek" in model_lower and reasoning_effort:
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
            "custom_llm_provider": custom_provider,
            "api_base": self.api_base or None,
            "api_key": self.api_key or None,
            **provider_params,
        }
        if litellm_tools:
            request_params["tools"] = litellm_tools
        if self.proxies:
            request_params["proxy"] = self.proxies

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

        response = litellm.completion(**request_params)

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
                        if hasattr(tc, 'id') and tc.id:
                            tool_calls_accumulator[tc_index]['id'] = tc.id

                        if hasattr(tc, 'function') and tc.function:
                            if hasattr(tc.function, 'name') and tc.function.name:
                                tool_calls_accumulator[tc_index]['name'] = tc.function.name
                            if hasattr(tc.function, 'arguments') and tc.function.arguments:
                                tool_calls_accumulator[tc_index]['arguments'] += tc.function.arguments

                if hasattr(chunk, 'usage') and chunk.usage:
                    usage = chunk.usage

            # 处理累积完成后的tool_calls
            for idx in sorted(tool_calls_accumulator.keys()):
                tc_data = tool_calls_accumulator[idx]
                tc_name = tc_data['name']
                tc_args_raw = tc_data['arguments'] or "{}"

                # 跳过空工具名（MiniMax会把一个tool_call拆成多个chunk）
                if not tc_name or tc_name.strip() == "":
                    print(f"[LiteLLM] Skipping tool_call with empty name at index {idx}: args={tc_args_raw[:100]}", file=sys.stderr, flush=True)
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

                args_str = json.dumps(tc_args, ensure_ascii=False)
                yield f'<tool_use>{{"id": "{tc_data["id"]}", "name": "{tc_name}", "arguments": {args_str}}}</tool_use>'

        except Exception as e:
            print(f"[LiteLLM] Stream error: {e}", file=sys.stderr, flush=True)
            import traceback
            traceback.print_exc(file=sys.stderr)

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

    session = LiteLLMSession(cfg)
    return ToolClient(session)
