"""
LLM Proxy Utilities

Provides helper functions for LLM configuration and direct LLM calls
through LiteLLMSession. Used by MCP client sampling callbacks and
LightRAG's _llm_model_func.

Remaining HTTP endpoints:
- GET /llm/v1/models — list configured model
- GET /llm/v1/health — check if LLM is configured
- GET /llm/v1/status — LightRAG and model status
"""

import json
import asyncio
import time
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from loguru import logger

router = APIRouter(prefix="/llm/v1", tags=["llm-proxy"])


# ============================================================================
# OpenAI API Models
# ============================================================================


class OpenAIToolCallFunction(BaseModel):
    """OpenAI tool call function"""

    name: str
    arguments: str


class OpenAIToolCall(BaseModel):
    """OpenAI tool call"""

    id: str
    type: str = "function"
    function: OpenAIToolCallFunction


class OpenAIMessage(BaseModel):
    """OpenAI message format"""

    role: str
    content: Optional[str] = None  # Can be None when tool_calls present
    name: Optional[str] = None
    tool_calls: Optional[List[OpenAIToolCall]] = None


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
    response_format: Optional[Dict[str, Any]] = None  # {"type":"json_object"} or structured schema
    stream: Optional[bool] = False


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

        # Convert tool_calls from dict to OpenAIToolCall if present
        tool_calls = None
        if message.get("tool_calls"):
            tool_calls = []
            for tc in message["tool_calls"]:
                tool_calls.append(
                    OpenAIToolCall(
                        id=tc["id"],
                        type=tc.get("type", "function"),
                        function=OpenAIToolCallFunction(
                            name=tc["function"]["name"],
                            arguments=tc["function"]["arguments"],
                        ),
                    )
                )

        choices.append(
            OpenAIChatResponseChoice(
                index=idx,
                message=OpenAIMessage(
                    role=message.get("role", "assistant"),
                    content=message.get("content"),
                    tool_calls=tool_calls,
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


def get_llm_config(use_lightrag_config: bool = False) -> Dict[str, str]:
    """Read LLM config from file.

    Args:
        use_lightrag_config: If True, read from 'lightrag_llm' section.
            model 为空时使用主 llm 同一模型（正常默认行为）。
            apiKey/apiBase/type 为空时从 llm 段继承。
            reasoning_effort 默认 "none"（独立于模型配置，强制禁用思考链）。
            用户可在 lightrag_llm 段显式设置 reasoning_effort 覆盖默认值。
    """
    from pathlib import Path

    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        llm = data.get("llm", {})

        # If requesting lightrag config, apply lightrag_llm overrides
        if use_lightrag_config:
            lightrag_llm = data.get("lightrag_llm", {})
            if lightrag_llm.get("model"):
                # Independent model configured: inherit missing fields from llm
                if not lightrag_llm.get("apiKey"):
                    lightrag_llm["apiKey"] = llm.get("apiKey", "")
                if not lightrag_llm.get("apiBase"):
                    lightrag_llm["apiBase"] = llm.get("apiBase", "")
                if not lightrag_llm.get("type"):
                    lightrag_llm["type"] = llm.get("type", "openai")
                # Default reasoning_effort to "none" if not explicitly set
                if not lightrag_llm.get("reasoning_effort"):
                    lightrag_llm["reasoning_effort"] = "none"
                llm = lightrag_llm
            else:
                # Use main llm model, but independently apply reasoning_effort
                # model 和 reasoning_effort 是两个独立维度
                llm = dict(llm)
                user_effort = lightrag_llm.get("reasoning_effort")
                llm["reasoning_effort"] = user_effort if user_effort else "none"

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
        return {"type": "openai", "apikey": "", "apibase": "", "model": "", "reasoning_effort": "none"}


async def call_llm_via_litellm(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    response_format: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Call LLM using LiteLLM (through GenericAgent's LiteLLMSession)

    This ensures full SDK path is used.
    """
    import time
    start_time = time.time()
    logger.info(f"[LLM Proxy] call_llm_via_litellm started")

    if config is None:
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
        "reasoning_effort": config.get("reasoning_effort"),
    }

    # Create independent session (not shared with main chat)
    session = LiteLLMSession(cfg=llm_config)

    # Call LLM (chat() returns a generator)
    try:
        # Run in thread to avoid blocking
        def sync_call():
            import time
            start_time = time.time()
            logger.info(f"[LLM Proxy] sync_call started at {start_time}")

            gen = session.chat(messages=messages, tools=tools, response_format=response_format)

            # Consume generator to get MockResponse
            # IMPORTANT: Must use next() to capture StopIteration return value
            # for loop will auto-catch StopIteration and we lose the return value
            chunks = []
            mock_response = None

            try:
                while True:
                    chunk = next(gen)
                    if isinstance(chunk, str):
                        chunks.append(chunk)
            except StopIteration as e:
                # Generator returns MockResponse via StopIteration
                mock_response = e.value

            elapsed = time.time() - start_time
            logger.info(f"[LLM Proxy] sync_call completed in {elapsed:.2f}s, content_len={len(''.join(chunks))}")

            # Extract tool calls from MockResponse
            tool_calls_list = []
            if mock_response and hasattr(mock_response, 'tool_calls'):
                for tc in mock_response.tool_calls:
                    args_str = tc.function.arguments if isinstance(tc.function.arguments, str) else json.dumps(tc.function.arguments)
                    tool_calls_list.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": args_str
                        }
                    })

            # Build OpenAI-format response
            full_text = "".join(chunks)

            logger.info(f"[LLM Proxy] MockResponse exists: {mock_response is not None}")
            logger.info(f"[LLM Proxy] Tool calls count: {len(tool_calls_list)}")
            logger.info(f"[LLM Proxy] Content length: {len(full_text)}")
            if tool_calls_list:
                logger.info(f"[LLM Proxy] Tool call names: {[tc['function']['name'] for tc in tool_calls_list]}")
                logger.info(f"[LLM Proxy] First tool call: {json.dumps(tool_calls_list[0], ensure_ascii=False)}")

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

        response = await asyncio.wait_for(asyncio.to_thread(sync_call), timeout=180)
        elapsed = time.time() - start_time
        logger.info(f"[LLM Proxy] call_llm_via_litellm completed in {elapsed:.2f}s")
        return response

    except asyncio.TimeoutError:
        logger.error("[LLM Proxy] LLM call timed out after 180s")
        raise HTTPException(status_code=504, detail="LLM call timed out")
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        raise HTTPException(status_code=500, detail=f"LLM call failed: {str(e)}")


# ============================================================================
# API Endpoints
# ============================================================================


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


@router.get("/status")
async def lightrag_status():
    """LightRAG and model status endpoint (for system management)."""
    from niu_api.internal.embedding import get_current_model_info
    from niu_api.internal.reranker import get_current_reranker_info

    status = {
        "embedding": get_current_model_info(),
        "reranker": get_current_reranker_info(),
    }

    # LightRAG status (optional, may not be installed)
    try:
        from niu_api.internal.lightrag_manager import get_lightrag_status
        status["lightrag"] = get_lightrag_status()
    except Exception:
        status["lightrag"] = {"installed": False}

    return status
