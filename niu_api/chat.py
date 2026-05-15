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


def notify_tool_status_sync(tool_name: str, status: str, summary: str = ""):
    """从同步线程推送工具调用状态到 SSE 事件总线

    Args:
        tool_name: 工具名称（短名，如 detect_faces）
        status: "start" 或 "end"
        summary: 可选的简短描述
    """
    event = {
        "type": "tool_status",
        "tool_name": tool_name,
        "status": status,
        "summary": summary,
    }
    loop = _main_loop
    if loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(_sync_broadcast, event)
    except RuntimeError:
        pass


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


async def persist_messages(store, messages: list, session_id: str):
    """将 agent_runner_loop 的 messages 持久化到数据库（双管道 DB 管道）。

    只持久化 tool 相关消息（user/assistant 已在流式过程中存储）：
    - role="tool" 的消息：存储 tool_call_id + content
    - role="assistant" 且有 tool_calls 的消息：存储 tool_calls 字段

    Args:
        store: MessageStore 实例
        messages: agent_runner_loop return value 中的 messages 列表
        session_id: 会话ID（当前未使用，预留）
    """
    persisted_count = 0
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls")
        tool_call_id = msg.get("tool_call_id", "")

        # 只持久化 tool 相关消息
        if role == "tool":
            # tool 消息必须关联到 assistant 的 tool_call
            if tool_call_id:
                await store.add_message(
                    role="tool",
                    content=content or "",
                    tool_call_id=tool_call_id,
                )
                persisted_count += 1
        elif role == "assistant" and tool_calls:
            # assistant(tool_calls) 消息：存储 tool_calls 字段
            # content 可能为空（纯工具调用时 LLM 不返回文本）
            await store.add_message(
                role="assistant",
                content=content or "",
                tool_calls=tool_calls,
            )
            persisted_count += 1

    if persisted_count > 0:
        logger.debug(f"[DB Pipeline] Persisted {persisted_count} tool-related messages")


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
        # 排队等待锁：最多等 60 秒
        try:
            await asyncio.wait_for(_chat_lock.acquire(), timeout=60.0)
        except asyncio.TimeoutError:
            logger.warning("[/chat] _chat_lock 60s timeout, request rejected")
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

            # 双管道 DB 管道：从 return value 持久化消息
            rv = getattr(runner, "last_return_value", None)
            if rv and isinstance(rv, dict) and rv.get("messages"):
                store = await get_message_store()
                # 持久化 user + assistant + tool 消息
                # 只持久化第一条 user 消息（真实用户输入），跳过 agent 内部的 next_prompt
                user_persisted = False
                last_assistant_id = None
                for msg in rv["messages"]:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    tool_calls = msg.get("tool_calls")
                    tool_call_id = msg.get("tool_call_id", "")

                    if role == "system":
                        continue
                    if role == "user":
                        if not user_persisted:
                            # 只持久化第一条 user 消息（真实用户输入）
                            user_msg_id = await store.add_message(role="user", content=content)
                            await notify_new_message(user_msg_id, "user", content)
                            user_persisted = True
                        continue  # 跳过后续 next_prompt user 消息
                    elif role == "tool" and tool_call_id:
                        await store.add_message(role="tool", content=content or "", tool_call_id=tool_call_id)
                    elif role == "assistant":
                        pid = await store.add_message(role="assistant", content=content or "", tool_calls=tool_calls)
                        last_assistant_id = pid

                # 推送最后一条 assistant 消息给 SSE 订阅者
                if last_assistant_id:
                    full_reply = "".join(reply_chunks)
                    await notify_new_message(last_assistant_id, "assistant", full_reply)
            else:
                # 回退：无 return_value 时，从 request 和 reply_chunks 持久化
                store = await get_message_store()
                user_msg_id = await store.add_message(role="user", content=request.message)
                await notify_new_message(user_msg_id, "user", request.message)
                full_reply = "".join(reply_chunks)
                if full_reply.strip():
                    msg_id = await store.add_message(role="assistant", content=full_reply)
                    await notify_new_message(msg_id, "assistant", full_reply)

            # 检测主 Agent 上下文溢出 → 同步触发 force 压缩（阻塞）
            if rv and isinstance(rv, dict) and rv.get("result") == "CONTEXT_OVERFLOW":
                overflow_data = rv.get("data", {})
                logger.warning(
                    f"[Chat SSE] Main agent CONTEXT_OVERFLOW at {overflow_data.get('tokens_used', 0)} tokens, "
                    f"triggering force compression (blocking)"
                )
                from niu_api.compat import _tidy_context_impl, _tidy_lock
                async with _tidy_lock:
                    tidy_result = await _tidy_context_impl(request={"session_id": session_id, "mode": "force"})
                logger.info(f"[Chat SSE] Force compression result: {tidy_result.get('status')}")
                yield f"data: {json.dumps({'force_compression_done': True, 'status': tidy_result.get('status')})}\n\n"
            else:
                # 正常：异步触发增量整理检查（不阻塞）
                full_reply = "".join(reply_chunks)
                if full_reply.strip():
                    from niu_api.compat import _check_and_trigger_auto_tidy
                    store = await get_message_store()
                    await _check_and_trigger_auto_tidy(store)

            # Send final message
            yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"
        finally:
            # 确保执行器线程完成后再释放锁，防止 runner.chat() 并发
            sf = locals().get("stream_future")
            if sf and not sf.done():
                try:
                    await asyncio.wait_for(sf, timeout=30.0)
                except (asyncio.TimeoutError, Exception):
                    logger.warning("[/chat] Executor thread did not finish after client disconnect")
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

    # 排队等待锁：最多等 60 秒
    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=60.0)
    except asyncio.TimeoutError:
        logger.warning("[/chat/sync] _chat_lock 60s timeout, request rejected")
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

        # 双管道架构：runner.chat() 已过滤非 reply 内容，无需再清理

        # 持久化消息到数据库
        message_id = None
        rv = getattr(runner, "last_return_value", None)

        if rv and isinstance(rv, dict) and rv.get("messages"):
            # DB 管道：从 return value 持久化 tool 相关消息
            # assistant 纯文本回复也在这里持久化（避免与 persist_messages 重复）
            persisted_ids = []
            for msg in rv["messages"]:
                role = msg.get("role", "")
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls")
                tool_call_id = msg.get("tool_call_id", "")

                if role == "system":
                    continue
                if role == "user":
                    continue  # user 消息已在上方持久化

                if role == "tool" and tool_call_id:
                    pid = await store.add_message(role="tool", content=content or "", tool_call_id=tool_call_id)
                    persisted_ids.append(pid)
                elif role == "assistant":
                    pid = await store.add_message(role="assistant", content=content or "", tool_calls=tool_calls)
                    persisted_ids.append(pid)

            # 找到最后一条 assistant 消息的 ID（persisted_ids 可能包含 tool 消息）
            last_assistant_id = None
            if persisted_ids:
                persisted_idx = 0
                for msg in rv["messages"]:
                    role = msg.get("role", "")
                    if role in ("system", "user"):
                        continue
                    if persisted_idx < len(persisted_ids):
                        if role == "assistant":
                            last_assistant_id = persisted_ids[persisted_idx]
                        persisted_idx += 1
            if last_assistant_id:
                message_id = last_assistant_id
                await notify_new_message(message_id, "assistant", full_reply)
            elif persisted_ids and full_reply.strip():
                # 回退：没有 assistant 消息（纯文本回复时 rv 中无 assistant 消息）
                message_id = await store.add_message(role="assistant", content=full_reply)
                await notify_new_message(message_id, "assistant", full_reply)
        elif full_reply.strip():
            # 回退：无 return_value 时只存 assistant 纯文本回复
            message_id = await store.add_message(role="assistant", content=full_reply)
            await notify_new_message(message_id, "assistant", full_reply)

        # 检测主 Agent 上下文溢出 → 同步触发 force 压缩（阻塞，压缩完再继续）
        if rv and isinstance(rv, dict) and rv.get("result") == "CONTEXT_OVERFLOW":
            overflow_data = rv.get("data", {})
            logger.warning(
                f"[Chat] Main agent CONTEXT_OVERFLOW at {overflow_data.get('tokens_used', 0)} tokens, "
                f"triggering force compression (blocking)"
            )
            from niu_api.compat import _tidy_context_impl, _tidy_lock
            async with _tidy_lock:
                tidy_result = await _tidy_context_impl(request={"session_id": session_id, "mode": "force"})
            logger.info(f"[Chat] Force compression result: {tidy_result.get('status')}")
            # 压缩完成后不触发 auto_tidy（force 已包含完整3步整理）
        else:
            # 正常：异步触发增量整理检查（不阻塞）
            if full_reply.strip():
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
