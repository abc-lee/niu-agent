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
from niu_api.config import get_config

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


def init_runner(mcp_tools: list = None):
    """
    初始化 Runner（从 API 启动时调用）

    Args:
        mcp_tools: 预加载的 MCP 工具列表
    """
    config = get_config()
    llm_config = {
        "type": config.llm.provider if config.llm else "openai",
        "apikey": config.llm.api_key if config.llm else "",
        "apibase": config.llm.api_base if config.llm else "",
        "model": config.llm.model if config.llm else "gpt-4o",
    }

    from agent.mcp_client import get_mcp_manager

    mcp_client = get_mcp_manager()

    # 使用 agent.runner 的全局 runner
    runner = get_runner(llm_config=llm_config, mcp_client=mcp_client)

    # 设置 MCP 工具 Schema
    if mcp_tools:
        runner.set_mcp_tools_schema(mcp_tools)


def get_or_create_runner() -> NiuRunner:
    """Get or create NiuRunner"""
    runner = get_runner()
    if runner is None:
        # 如果还没初始化，用空 MCP 工具列表
        init_runner(mcp_tools=[])
    return get_runner()


@router.post("/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    """
    Main chat endpoint - 使用 NiuRunner 流式响应
    """
    config = get_config()

    if not config.llm or not config.llm.api_key:
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
    config = get_config()

    if not config.llm or not config.llm.api_key:
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
