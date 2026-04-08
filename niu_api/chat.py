"""
Chat API endpoints

使用 NiuRunner 作为后端
"""

import json
import asyncio
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from agent.runner import NiuRunner, get_runner
from agent.session import get_message_store

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    """Chat request model"""

    session_id: Optional[str] = None
    message: str
    system_prompt: Optional[str] = ""


class ChatResponse(BaseModel):
    """Chat response model"""

    session_id: str
    reply: str


def _load_llm_config():
    """直接从文件读取 LLM 配置，不走缓存，保留所有原始字段"""
    import json
    from pathlib import Path

    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        llm = data.get("llm", {})

        # 直接返回原始配置，统一键名为小写（兼容不同格式）
        config = {}

        # 处理所有可能的键名格式（apiKey/apikey/api_key）
        for key, value in llm.items():
            # 统一转换为小写
            config[key.lower()] = value

        # 确保必要字段有默认值
        config.setdefault("type", "openai")
        config.setdefault("apikey", "")
        config.setdefault("apibase", "")
        config.setdefault("model", "")

        return config
    except Exception:
        return {"type": "openai", "apikey": "", "apibase": "", "model": ""}


def init_runner(mcp_tools: list = None):
    """
    初始化 Runner（从 API 启动时调用）

    Args:
        mcp_tools: 预加载的 MCP 工具列表
    """
    llm_config = _load_llm_config()

    from agent.mcp_client import get_mcp_manager

    mcp_client = get_mcp_manager()

    # 使用 agent.runner 的全局 runner
    runner = get_runner(llm_config=llm_config, mcp_client=mcp_client)

    # 设置 MCP 工具 Schema
    if mcp_tools:
        runner.set_mcp_tools_schema(mcp_tools)


def get_or_create_runner() -> "NiuRunner":
    """Get or create NiuRunner，配置变更后自动重新初始化"""
    from agent import runner as runner_module

    existing = get_runner()
    current = _load_llm_config()

    if existing is not None and current["apikey"] and current["model"]:
        # Runner 已存在，检查配置是否变更
        runner_llm = getattr(existing, "llm_config", {})
        if runner_llm.get("apikey") != current["apikey"] or runner_llm.get("model") != current["model"]:
            # 配置已变更，重新初始化
            with runner_module._runner_lock:
                runner_module._runner = None

    if get_runner() is None:
        init_runner(mcp_tools=[])

    return get_runner()


@router.post("/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    """
    Main chat endpoint - 使用 NiuRunner 流式响应
    """
    llm_cfg = _load_llm_config()

    if not llm_cfg["apikey"]:
        raise HTTPException(
            status_code=400, detail="LLM not configured. Please set up API key first."
        )

    runner = get_or_create_runner()

    # Get or create session
    session_id = request.session_id or "default"

    # Stream response
    async def generate():
        reply_chunks = []

        def sync_chat():
            return runner.chat(session_id, request.message, stream=True)

        # Run in executor
        loop = asyncio.get_event_loop()
        gen = await loop.run_in_executor(None, sync_chat)

        for chunk in gen:
            if chunk:
                reply_chunks.append(chunk)
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"

        # Send final message
        yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/sync")
async def chat_sync(request: ChatRequest) -> ChatResponse:
    """
    Synchronous chat endpoint - waits for complete response
    """
    llm_cfg = _load_llm_config()

    if not llm_cfg["apikey"]:
        raise HTTPException(
            status_code=400, detail="LLM not configured. Please set up API key first."
        )

    runner = get_or_create_runner()
    session_id = request.session_id or "default"

    # Run chat (non-streaming)
    def sync_chat():
        full_reply = ""
        for chunk in runner.chat(session_id, request.message, stream=True):
            full_reply += chunk
        return full_reply

    loop = asyncio.get_event_loop()
    full_reply = await loop.run_in_executor(None, sync_chat)

    return ChatResponse(session_id=session_id, reply=full_reply)


@router.post("/chat/session")
async def get_or_create_session(request: ChatRequest) -> dict:
    """Get or create a chat session"""
    session_id = request.session_id or "default"
    store = await get_message_store()
    count = await store.count_messages()
    return {"session_id": session_id, "message_count": count}


@router.delete("/chat/session/{session_id}")
async def clear_session(session_id: str):
    """Clear a chat session"""
    store = await get_message_store()
    await store.clear_messages()
    return {"status": "ok", "session_id": session_id}
