"""
LiteLLM Adapter Module

LiteLLM统一适配器，提供与现有BaseSession/ToolClient接口兼容的LiteLLM封装。
支持100+ LLM提供商，统一响应格式。

重要：此模块通过配置开关 use_litellm 启用，不影响现有代码。
"""

import json
import re
import sys
from typing import Any, Dict, Generator, Optional, List

import litellm

# 导入现有类用于类型转换
from .llmcore import (
    BaseSession,
    MockResponse,
    MockToolCall,
    MockFunction,
    ToolClient,
    _write_full_interaction_log,
)


# 模型名称映射：从当前配置格式到LiteLLM格式
MODEL_NAME_MAP = {
    "MiniMax-M2.7-highspeed": "minimax/MiniMax-M2.7-highspeed",
    "MiniMax-M2.1": "minimax/MiniMax-M2.1",
    "glm-5": "zhipuai/glm-5",
    "glm-4": "zhipuai/glm-4",
    "claude-3-5-sonnet-20241022": "claude-3-5-sonnet-20241022",
    "claude-3-opus-20240229": "claude-3-opus-20240229",
    "claude-3-sonnet-20240229": "claude-3-sonnet-20240229",
    "gpt-4o": "openai/gpt-4o",
    "gpt-4-turbo": "openai/gpt-4-turbo",
    "gpt-3.5-turbo": "openai/gpt-3.5-turbo",
    "deepseek-chat": "deepseek/deepseek-chat",
    "deepseek-reasoner": "deepseek/deepseek-reasoner",
}


def get_litellm_model_name(model: str) -> str:
    """将配置中的模型名称转换为LiteLLM格式"""
    return MODEL_NAME_MAP.get(model, model)


def get_provider_params(model: str) -> Dict[str, Any]:
    """获取提供商特定参数"""
    params: Dict[str, Any] = {}
    model_lower = model.lower()

    # MiniMax: 启用reasoning_split
    if "minimax" in model_lower:
        params["extra_body"] = {"reasoning_split": True}

    # Claude: 启用prompt caching
    if "claude" in model_lower:
        params["extra_headers"] = {"anthropic-beta": "prompt-caching-2024-07-31"}

    # DeepSeek: 支持reasoning effort
    if "deepseek" in model_lower:
        params["reasoning_effort"] = "high"

    return params


def _convert_tools_schema(tools: Optional[List]) -> Optional[List]:
    """
    将工具schema转换为LiteLLM格式

    LiteLLM接受OpenAI格式的工具定义：
    {
        "type": "function",
        "function": {
            "name": "...",
            "description": "...",
            "parameters": {...}
        }
    }
    """
    if not tools:
        return None

    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue

        # 已经是正确格式
        if "type" in tool and "function" in tool:
            converted.append(tool)
        # Claude格式: {"name": "...", "input_schema": {...}}
        elif "name" in tool and "input_schema" in tool:
            converted.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool["input_schema"],
                }
            })
        # OpenAI格式: {"type": "function", "function": {...}}
        elif tool.get("type") == "function":
            converted.append(tool)
        # 简单格式: {"name": "...", "parameters": {...}}
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

    def raw_ask(
        self,
        prompt: Any,
        model: Optional[str] = None,
        temperature: float = 0.5,
        max_tokens: Optional[int] = None,
        tools: Optional[List] = None,
        **kwargs
    ) -> Generator[str, None, MockResponse]:
        """
        调用LiteLLM并返回响应（生成器版本）

        与BaseSession.raw_ask()接口兼容。

        Args:
            prompt: 协议提示字符串或消息列表
            model: 模型名称（可选）
            temperature: 温度参数
            max_tokens: 最大token数（可选）
            tools: 工具schema列表（可选）

        Yields:
            字符串块（流式响应）

        Returns:
            MockResponse对象（通过StopIteration）
        """
        # 解析prompt：如果是字符串，转换为消息列表
        if isinstance(prompt, str):
            messages = [{"role": "user", "content": prompt}]
        elif isinstance(prompt, list):
            messages = prompt
        else:
            messages = [{"role": "user", "content": str(prompt)}]

        # 获取模型名称
        model = model or self.default_model
        litellm_model = get_litellm_model_name(model)

        # 获取提供商特定参数
        provider_params = get_provider_params(model)

        # 构建完整参数
        request_params: Dict[str, Any] = {
            "model": litellm_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            **provider_params,
        }

        if max_tokens:
            request_params["max_tokens"] = max_tokens

        # 添加工具定义
        litellm_tools = _convert_tools_schema(tools)
        if litellm_tools:
            request_params["tools"] = litellm_tools

        print(f"[LiteLLM] Calling {litellm_model}...", file=sys.stderr, flush=True)

        try:
            response = litellm.completion(**request_params)

        except Exception as e:
            print(f"[LiteLLM] Completion error: {e}", file=sys.stderr, flush=True)
            # 返回错误响应
            error_content = f"Error: {str(e)}"
            yield error_content
            return MockResponse(
                thinking="",
                content=error_content,
                tool_calls=[],
                raw=error_content,
            )

        # 流式处理
        full_content = ""
        reasoning_content = ""
        tool_calls: List[MockToolCall] = []
        usage = None

        try:
            for chunk in response:
                # chunk是ModelResponseStream对象
                delta = chunk.choices[0].delta if hasattr(chunk, 'choices') else None
                if not delta:
                    continue

                # 提取content
                if hasattr(delta, 'content') and delta.content:
                    full_content += delta.content
                    yield delta.content

                # 提取reasoning_content（如果模型支持）
                if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                    reasoning_content += delta.reasoning_content

                # 提取tool_calls（如果模型支持）
                if hasattr(delta, 'tool_calls') and delta.tool_calls:
                    for tc in delta.tool_calls:
                        tc_id = getattr(tc, 'id', None) or f"call_{len(tool_calls)}"
                        tc_name = getattr(tc.function, 'name', None) or ""
                        tc_args_raw = getattr(tc.function, 'arguments', None) or "{}"

                        # 解析arguments
                        if isinstance(tc_args_raw, str):
                            try:
                                tc_args = json.loads(tc_args_raw)
                            except json.JSONDecodeError:
                                tc_args = {}
                        elif isinstance(tc_args_raw, dict):
                            tc_args = tc_args_raw
                        else:
                            tc_args = {}

                        tool_calls.append(
                            MockToolCall(
                                name=tc_name,
                                args=tc_args,
                                id=str(tc_id),
                            )
                        )

                # 提取usage（最后一个chunk）
                if hasattr(chunk, 'usage') and chunk.usage:
                    usage = chunk.usage

        except Exception as e:
            print(f"[LiteLLM] Stream error: {e}", file=sys.stderr, flush=True)
            import traceback
            traceback.print_exc(file=sys.stderr)

        # 构建MockResponse
        mock_resp = MockResponse(
            thinking=reasoning_content,
            content=full_content,
            tool_calls=tool_calls,
            raw=full_content,
        )

        # 添加usage信息（如果可用）
        if usage:
            mock_resp.usage = {
                "prompt_tokens": getattr(usage, 'prompt_tokens', 0) or 0,
                "completion_tokens": getattr(usage, 'completion_tokens', 0) or 0,
                "total_tokens": getattr(usage, 'total_tokens', 0) or 0,
            }

        return mock_resp


def create_litellm_client(config: Dict[str, Any]) -> ToolClient:
    """
    创建LiteLLM客户端的便捷函数

    Args:
        config: LLM配置字典，包含apiKey, model等字段

    Returns:
        配置好的LiteLLMToolClient实例
    """
    # 确保配置有必要的字段
    cfg = {
        "apikey": config.get("apiKey") or config.get("apikey", ""),
        "apibase": config.get("apiBase") or config.get("apibase", ""),
        "model": config.get("model", "gpt-4o"),
    }

    session = LiteLLMSession(cfg)
    return ToolClient(session)
