"""
LLM Proxy API

为 LightRAG、浏览器插件等提供 OpenAI 兼容的代理端点。

功能：
1. 提供 /llm/v1/chat/completions 端点（OpenAI 格式）
2. 提供 /llm/v1/embeddings 端点（OpenAI 格式）
3. 将请求转换为 internal LLM SDK 调用
4. 返回 OpenAI 格式的响应

使用方式：
- LightRAG: base_url=http://localhost:9876/llm/v1
- 浏览器插件: Base URL: http://localhost:9876/llm/v1
- Model: 任意（代理会使用配置文件中的模型）
- API Key: 留空或随意填写
"""

import json
import asyncio
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from loguru import logger

from niu_api.internal.brain_region_prompt import inject_brain_region_context

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
    response_format: Optional[Dict[str, Any]] = None,
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


@router.post("/chat/completions")
async def chat_completions(request: OpenAIChatRequest) -> OpenAIChatResponse:
    """
    OpenAI-compatible chat completions endpoint

    This endpoint accepts OpenAI-format requests and converts them to
    internal LLM SDK calls, then returns OpenAI-format responses.

    Used by LightRAG and browser extensions.
    """
    logger.info(f"[LLM Proxy] Received request: model={request.model}, messages={len(request.messages)}")
    logger.info(f"[LLM Proxy] Tools count: {len(request.tools) if request.tools else 0}")
    if request.tools:
        logger.info(f"[LLM Proxy] Tool names: {[t.function.get('name') for t in request.tools if hasattr(t, 'function')]}")

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

    # Inject brain region context for LightRAG extraction requests
    # Reads directly from JSON file — no LightRAG API call to avoid deadlock
    try:
        litellm_messages = inject_brain_region_context(litellm_messages)
    except Exception:
        logger.warning("Brain region injection failed, continuing without it", exc_info=True)

    logger.debug(f"[LLM Proxy] Converted {len(litellm_messages)} messages")
    if litellm_tools:
        logger.debug(f"[LLM Proxy] Tools: {len(litellm_tools)}")

    # Call LLM
    try:
        response = await call_llm_via_litellm(
            messages=litellm_messages,
            tools=litellm_tools,
            response_format=request.response_format,
        )

        # Convert back to OpenAI format
        openai_response = litellm_to_openai_response(response, config["model"])

        logger.info(f"[LLM Proxy] Response: {len(openai_response.choices)} choices")
        return openai_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[LLM Proxy] Error: {e}")
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


# ============================================================================
# Embeddings Endpoint (for LightRAG)
# ============================================================================


class OpenAIEmbeddingRequest(BaseModel):
    """OpenAI embeddings request"""

    model: str
    input: Any  # str or List[str]
    encoding_format: Optional[str] = "float"
    dimensions: Optional[int] = None


@router.post("/embeddings")
async def create_embeddings(request: OpenAIEmbeddingRequest):
    """OpenAI-compatible embeddings endpoint for LightRAG."""
    import time
    import uuid

    from niu_api.internal.embedding import batch_encode

    # Normalize input to list
    texts = request.input if isinstance(request.input, list) else [request.input]

    # Get embeddings using shared model
    embeddings = batch_encode(texts)

    # Apply dimension reduction if requested (Matryoshka-style)
    if request.dimensions and request.dimensions < len(embeddings[0]):
        embeddings = [e[: request.dimensions] for e in embeddings]

    # Format response
    data = [
        {
            "object": "embedding",
            "embedding": list(emb) if not isinstance(emb, list) else emb,
            "index": idx,
        }
        for idx, emb in enumerate(embeddings)
    ]

    return {
        "id": f"embd-{uuid.uuid4().hex[:8]}",
        "object": "list",
        "created": int(time.time()),
        "model": request.model,
        "data": data,
        "usage": {
            "prompt_tokens": sum(len(t.split()) for t in texts),
            "total_tokens": sum(len(t.split()) for t in texts),
        },
    }
