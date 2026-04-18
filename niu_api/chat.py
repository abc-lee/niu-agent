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
from niu_api.compat import _chat_lock

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    """Chat request model"""

    session_id: Optional[str] = None
    message: str
    system_prompt: Optional[str] = ""


class ChatResponse(BaseModel):
    """Chat response model"""

    reply: str
    session_id: Optional[str] = None


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


def init_runner(tool_registry):
    """
    初始化 Runner（从 API 启动时调用）

    Args:
        tool_registry: ToolRegistry 实例
    """
    llm_config = _load_llm_config()

    # 不再需要 mcp_client，handler 直接使用 ToolRegistry
    runner = get_runner(llm_config=llm_config, mcp_client=None)

    # 设置 MCP 工具 Schema（从 ToolRegistry 获取）
    mcp_tools_schema = tool_registry.get_schemas()
    if mcp_tools_schema:
        runner.set_mcp_tools_schema(mcp_tools_schema)


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
        from agent.tool_registry import get_registry
        init_runner(get_registry())

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
        # Non-blocking acquire: reject if lock already held
        try:
            await asyncio.wait_for(_chat_lock.acquire(), timeout=0.01)
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'error': 'Another request is in progress, please wait'})}\n\n"
            yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"
            return

        try:
            reply_chunks = []

            # Run streaming in executor thread, communicate chunks via queue
            import queue as _queue

            chunk_queue: _queue.Queue[str | None] = _queue.Queue()

            def sync_stream():
                try:
                    for chunk in runner.chat(session_id, request.message, stream=True):
                        if chunk:
                            chunk_queue.put(chunk)
                finally:
                    chunk_queue.put(None)  # sentinel

            loop = asyncio.get_running_loop()
            stream_future = loop.run_in_executor(None, sync_stream)

            # Drain queue in async context while executor produces chunks
            while not stream_future.done() or not chunk_queue.empty():
                try:
                    chunk = chunk_queue.get_nowait()
                except _queue.Empty:
                    await asyncio.sleep(0.01)
                    continue
                if chunk is None:
                    break
                reply_chunks.append(chunk)
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"

            # Ensure executor finished
            await stream_future

            # Send final message
            yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"
        finally:
            _chat_lock.release()

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

    # Non-blocking acquire: reject if lock already held
    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=0.01)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503, detail="Another request is in progress, please try again later."
        )

    try:
        loop = asyncio.get_running_loop()
        full_reply = await loop.run_in_executor(None, sync_chat)
    finally:
        _chat_lock.release()

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
