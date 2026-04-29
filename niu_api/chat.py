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
from loguru import logger

from agent.runner import NiuRunner, get_runner
from agent.session import get_message_store
from niu_api.compat import _chat_lock

router = APIRouter(tags=["chat"])


# ============== SSE 事件总线（发布-订阅模式） ==============

# 每个 SSE 连接拥有自己的 Queue，notify_new_message 广播到所有订阅者
_event_subscribers: list[asyncio.Queue] = []
_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_event_loop(loop: asyncio.AbstractEventLoop):
    """在 uvicorn 启动时调用，保存主事件循环引用"""
    global _main_loop
    _main_loop = loop


async def notify_new_message(message_id: str, role: str, content: str):
    """新消息写入数据库后调用，广播给所有 SSE 订阅者"""
    event = {
        "type": "new_message",
        "id": message_id,
        "role": role,
        "content": content,
    }
    for q in _event_subscribers[:]:  # 复制列表，避免迭代中修改
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("[SSE] Subscriber queue full, skipping event")


def notify_new_message_sync(message_id: str, role: str, content: str):
    """同步版本 — 从非 async 上下文（如 scheduler 线程）调用"""
    event = {
        "type": "new_message",
        "id": message_id,
        "role": role,
        "content": content,
    }
    loop = _main_loop
    if loop is None or loop.is_closed():
        return
    # 用 call_soon_threadsafe 安全注入到 FastAPI 的事件循环
    try:
        loop.call_soon_threadsafe(_sync_broadcast, event)
    except RuntimeError:
        pass  # 循环已关闭


def _sync_broadcast(event: dict):
    """在 FastAPI 事件循环中执行广播"""
    for q in _event_subscribers[:]:  # 复制列表，避免迭代中修改
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # 队列满说明客户端处理慢，跳过但不移除
            logger.warning("[SSE] Subscriber queue full, skipping event")


class ChatRequest(BaseModel):
    """Chat request model"""

    session_id: Optional[str] = None
    message: str
    system_prompt: Optional[str] = ""


class ChatResponse(BaseModel):
    """Chat response model"""

    reply: str
    session_id: Optional[str] = None
    message_id: Optional[str] = None


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
            stream_error = None

            # Run streaming in executor thread, communicate chunks via queue
            import queue as _queue

            chunk_queue: _queue.Queue[str | None] = _queue.Queue()

            def sync_stream():
                nonlocal stream_error
                try:
                    for chunk in runner.chat(session_id, request.message, stream=True):
                        if chunk:
                            chunk_queue.put(chunk)
                except Exception as e:
                    stream_error = str(e)
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
            try:
                await stream_future
            except Exception as e:
                stream_error = stream_error or str(e)

            # Send error if streaming failed
            if stream_error:
                yield f"data: {json.dumps({'error': stream_error})}\n\n"

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
    Synchronous chat endpoint - waits for complete response.
    Persists both user and assistant messages to the database.
    """
    llm_cfg = _load_llm_config()

    if not llm_cfg["apikey"]:
        raise HTTPException(
            status_code=400, detail="LLM not configured. Please set up API key first."
        )

    # Non-blocking acquire: reject if lock already held
    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=0.01)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503, detail="Another request is in progress, please try again later."
        )

    try:
        # Persist user message to database
        store = await get_message_store()
        user_msg_id = await store.add_message(role="user", content=request.message)
        # /chat/sync 由 scheduler 调用，用户消息不在前端本地渲染，需要 SSE 推送
        await notify_new_message(user_msg_id, "user", request.message)

        # Load conversation history via ContextManager (same as /api/chat/session)
        from agent.context_manager import get_context_manager

        context_manager = await get_context_manager(store)
        history_for_runner = await context_manager.get_context_for_chat(exclude_last=True)

        runner = get_or_create_runner()
        session_id = request.session_id or "default"

        # Run chat (non-streaming)
        def sync_chat():
            full_reply = ""
            for chunk in runner.chat(session_id, request.message, stream=True, history=history_for_runner):
                full_reply += chunk
            return full_reply

        try:
            loop = asyncio.get_running_loop()
            full_reply = await loop.run_in_executor(None, sync_chat)
        except Exception as e:
            import traceback
            from loguru import logger
            logger.error(f"Chat sync error: {e}\n{traceback.format_exc()}")
            full_reply = f"Error: {str(e)}"

        # Persist assistant response to database
        message_id = None
        if full_reply.strip():
            message_id = await store.add_message(role="assistant", content=full_reply)
            await notify_new_message(message_id, "assistant", full_reply)

            # 自动增量整理检查
            from niu_api.compat import _check_and_trigger_auto_tidy
            await _check_and_trigger_auto_tidy(store)

        return ChatResponse(session_id=session_id, reply=full_reply, message_id=message_id)
    finally:
        _chat_lock.release()


@router.get("/api/events/stream")
async def events_stream():
    """
    SSE 事件流 — 实时推送新消息给前端

    前端（main.js）订阅此端点，收到 new_message 事件后
    通过 Electron IPC 推送给 chat.html 渲染。
    """
    async def generate():
        # 每个连接拥有自己的队列
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        _event_subscribers.append(q)
        logger.info(f"[SSE] Client connected (total: {len(_event_subscribers)})")
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # 心跳，保持连接
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            logger.info("[SSE] Client disconnected")
            raise
        finally:
            try:
                _event_subscribers.remove(q)
            except ValueError:
                pass  # already removed (double-disconnect edge case)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
