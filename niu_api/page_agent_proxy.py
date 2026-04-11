"""
Page-Agent Proxy API

为 Page-Agent 浏览器插件提供 OpenAI 兼容的代理端点。

功能：
1. 提供 /v1/chat/completions 端点（OpenAI 格式）
2. 将请求转换为 internal LLM SDK 调用
3. 返回 OpenAI 格式的响应

使用方式：
在 Page-Agent 插件中配置：
- Base URL: http://localhost:9876/proxy/v1
- Model: 任意（代理会使用配置文件中的模型）
- API Key: 留空或随意填写
"""

import json
import asyncio
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from loguru import logger

router = APIRouter(prefix="/proxy/v1", tags=["page-agent-proxy"])


# ============================================================================
# OpenAI API Models
# ============================================================================


class OpenAIMessage(BaseModel):
    """OpenAI message format"""

    role: str
    content: str
    name: Optional[str] = None


class OpenAIToolCallFunction(BaseModel):
    """OpenAI tool call function"""

    name: str
    arguments: str


class OpenAIToolCall(BaseModel):
    """OpenAI tool call"""

    id: str
    type: str = "function"
    function: OpenAIToolCallFunction


class OpenAITool(BaseModel):
    """OpenAI tool definition"""

    type: str = "function"
    function: Dict[str, Any]


class OpenAIChatRequest(BaseModel):
    """OpenAI chat completions request"""

    model: str
    messages: List[OpenAIMessage]
    temperature: Optional[float] = 1.0
    tools: Optional[List[OpenAITool]] = None
    tool_choice: Optional[Any] = None
    parallel_tool_calls: Optional[bool] = False


class OpenAIChatResponseChoice(BaseModel):
    """OpenAI chat response choice"""

    index: int
    message: OpenAIMessage
    finish_reason: str


class OpenAIUsage(BaseModel):
    """OpenAI usage stats"""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class OpenAIChatResponse(BaseModel):
    """OpenAI chat completions response"""

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[OpenAIChatResponseChoice]
    usage: OpenAIUsage


# ============================================================================
# Format Conversion Utilities
# ============================================================================


def openai_to_litellm_messages(openai_messages: List[OpenAIMessage]) -> List[Dict[str, Any]]:
    """Convert OpenAI messages to LiteLLM format"""
    litellm_messages = []
    for msg in openai_messages:
        litellm_msg = {
            "role": msg.role,
            "content": msg.content,
        }
        if msg.name:
            litellm_msg["name"] = msg.name
        litellm_messages.append(litellm_msg)
    return litellm_messages


def openai_to_litellm_tools(openai_tools: Optional[List[OpenAITool]]) -> Optional[List[Dict[str, Any]]]:
    """Convert OpenAI tools to LiteLLM format"""
    if not openai_tools:
        return None

    litellm_tools = []
    for tool in openai_tools:
        if tool.type == "function":
            litellm_tools.append(tool.function)
    return litellm_tools if litellm_tools else None


def litellm_to_openai_response(
    litellm_response: Dict[str, Any], model: str
) -> OpenAIChatResponse:
    """Convert LiteLLM response to OpenAI format"""
    import time
    import uuid

    choices = []
    for idx, choice in enumerate(litellm_response.get("choices", [])):
        message = choice.get("message", {})
        choices.append(
            OpenAIChatResponseChoice(
                index=idx,
                message=OpenAIMessage(
                    role=message.get("role", "assistant"),
                    content=message.get("content", ""),
                ),
                finish_reason=choice.get("finish_reason", "stop"),
            )
        )

    usage = litellm_response.get("usage", {})

    return OpenAIChatResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:8]}",
        created=int(time.time()),
        model=model,
        choices=choices,
        usage=OpenAIUsage(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        ),
    )


# ============================================================================
# LLM Client
# ============================================================================


def get_llm_config() -> Dict[str, str]:
    """Read LLM config from file"""
    from pathlib import Path

    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        llm = data.get("llm", {})

        # 统一转换为小写键名
        config = {}
        for key, value in llm.items():
            config[key.lower()] = value

        config.setdefault("type", "openai")
        config.setdefault("apikey", "")
        config.setdefault("apibase", "")
        config.setdefault("model", "")

        return config
    except Exception:
        return {"type": "openai", "apikey": "", "apibase": "", "model": ""}


async def call_llm_via_litellm(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Call LLM using LiteLLM (through GenericAgent's LiteLLMSession)

    This ensures full SDK path is used.
    """
    config = get_llm_config()

    if not config["apikey"]:
        raise HTTPException(status_code=500, detail="LLM not configured")

    # Import LiteLLMSession from agent
    from agent.generic.litellm_adapter import LiteLLMSession

    # Create session with config
    llm_config = {
        "api_type": config.get("type", "openai"),
        "apikey": config["apikey"],
        "apibase": config["apibase"],
        "model": config["model"],
    }

    # Create independent session (not shared with main chat)
    session = LiteLLMSession(cfg=llm_config)

    # Call LLM (chat() returns a generator)
    try:
        # Run in thread to avoid blocking
        def sync_call():
            gen = session.chat(messages=messages, tools=tools)

            # Consume generator to get MockResponse
            chunks = []
            mock_response = None

            try:
                for chunk in gen:
                    if isinstance(chunk, str):
                        chunks.append(chunk)
            except StopIteration as e:
                # Generator returns MockResponse via StopIteration
                mock_response = e.value

            # If no StopIteration was raised, try to get the response
            # (some generators might return directly)
            if mock_response is None:
                # Try one more time to get the return value
                import inspect
                if inspect.isgenerator(gen):
                    try:
                        while True:
                            chunk = next(gen)
                            if isinstance(chunk, str):
                                chunks.append(chunk)
                    except StopIteration as e:
                        mock_response = e.value

            # Extract tool calls from MockResponse
            tool_calls_list = []
            if mock_response and hasattr(mock_response, 'tool_calls'):
                for tc in mock_response.tool_calls:
                    tool_calls_list.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments if isinstance(tc.function.arguments, str) else json.dumps(tc.function.arguments)
                        }
                    })

            # Build OpenAI-format response
            full_text = "".join(chunks)

            response = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": full_text or None,
                            "tool_calls": tool_calls_list if tool_calls_list else None,
                        },
                        "finish_reason": "tool_calls" if tool_calls_list else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": mock_response.usage.get("prompt_tokens", 0) if mock_response and hasattr(mock_response, 'usage') else 0,
                    "completion_tokens": mock_response.usage.get("completion_tokens", 0) if mock_response and hasattr(mock_response, 'usage') else 0,
                    "total_tokens": mock_response.usage.get("total_tokens", 0) if mock_response and hasattr(mock_response, 'usage') else 0,
                },
            }

            return response

        response = await asyncio.to_thread(sync_call)
        return response

    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        raise HTTPException(status_code=500, detail=f"LLM call failed: {str(e)}")


# ============================================================================
# API Endpoints
# ============================================================================


@router.post("/chat/completions")
async def chat_completions(request: OpenAIChatRequest) -> OpenAIChatResponse:
    """
    OpenAI-compatible chat completions endpoint

    This endpoint accepts OpenAI-format requests and converts them to
    internal LLM SDK calls, then returns OpenAI-format responses.

    Used by Page-Agent browser extension.
    """
    logger.info(f"[Page-Agent Proxy] Received request: model={request.model}, messages={len(request.messages)}")

    # Check LLM configuration
    config = get_llm_config()
    if not config["apikey"]:
        raise HTTPException(
            status_code=500,
            detail="LLM not configured. Please set API key in config/user-config.json"
        )

    # Convert OpenAI format to LiteLLM format
    litellm_messages = openai_to_litellm_messages(request.messages)
    litellm_tools = openai_to_litellm_tools(request.tools)

    logger.debug(f"[Page-Agent Proxy] Converted {len(litellm_messages)} messages")
    if litellm_tools:
        logger.debug(f"[Page-Agent Proxy] Tools: {len(litellm_tools)}")

    # Call LLM
    try:
        response = await call_llm_via_litellm(
            messages=litellm_messages,
            tools=litellm_tools,
        )

        # Convert back to OpenAI format
        openai_response = litellm_to_openai_response(response, config["model"])

        logger.info(f"[Page-Agent Proxy] Response: {len(openai_response.choices)} choices")
        return openai_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Page-Agent Proxy] Error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/models")
async def list_models():
    """List available models (OpenAI-compatible endpoint)"""
    config = get_llm_config()

    return {
        "object": "list",
        "data": [
            {
                "id": config.get("model", "unknown"),
                "object": "model",
                "created": 0,
                "owned_by": "user",
            }
        ],
    }


@router.get("/health")
async def health_check():
    """Health check endpoint"""
    config = get_llm_config()

    if not config["apikey"]:
        return {"status": "error", "message": "LLM not configured"}

    return {
        "status": "ok",
        "model": config.get("model", "unknown"),
        "api_base": config.get("apibase", "unknown"),
    }
