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


async def notify_new_message(message_id: str, role: str, content: str, source: str = "electron"):
    """新消息写入数据库后调用，广播给所有 SSE 订阅者"""
    # 双管道分离：tool 消息只走 DB 管道，不推送给前端
    if role == "tool":
        return
    if source != "electron":
        return  # 非electron通道不走SSE，前端零感知
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


def notify_new_message_sync(message_id: str, role: str, content: str, source: str = "electron"):
    """同步版本 — 从非 async 上下文（如 scheduler 线程）调用"""
    # 双管道分离：tool 消息只走 DB 管道，不推送给前端
    if role == "tool":
        return
    if source != "electron":
        return
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


async def push_ingest_result(file_path: str, error: str = ""):
    """将入库异常写入 message.db 并推送 SSE 通知。

    仅在入库异常时调用（管道失败或质量异常）。正常入库不调用。
    设计为 async 函数，在 LightRAG 事件循环中调用。

    Args:
        file_path: 入库文件路径
        error: 错误信息
    """
    import os
    file_name = os.path.basename(file_path) if file_path else "未知文件"
    content = f"文件入库异常：{file_name}" + (f"（{error}）" if error else "")

    try:
        from agent.session import MessageStore
        store = MessageStore()
        msg_id = await store.add_message(role="system", content=content)
        if msg_id:
            notify_new_message_sync(msg_id, "system", content)
    except Exception:
        pass


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


def notify_compact_status_sync(status: str, mode: str = "") -> None:
    """广播压缩状态事件到 /api/events/stream。

    跨线程安全：可在 executor 工作线程或后台 asyncio task 中调用。
    status: "started" | "done"
    mode: "force" | "sleep" | "auto"（可选，用于日志和前端提示）
    """
    if _main_loop is None:
        return
    event = {"type": "compact_status", "status": status, "mode": mode}
    try:
        _main_loop.call_soon_threadsafe(_sync_broadcast, event)
    except RuntimeError:
        # loop 已关闭，忽略
        pass


def _sync_broadcast(event: dict):
    """在 FastAPI 事件循环中执行广播"""
    for q in _event_subscribers[:]:  # 复制列表，避免迭代中修改
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # 队列满说明客户端处理慢，跳过但不移除
            logger.warning("[SSE] Subscriber queue full, skipping event")


def _msg_fingerprint(msg: dict) -> str | None:
    """生成消息指纹，用于去重判断

    指纹规则：
    - assistant(tool_calls): role + tool_calls[0].id（唯一标识一次工具调用）
    - assistant(纯文本): role + content[:50]（短文本前缀，避免长文本哈希）
    - tool: role + tool_call_id（唯一标识一次工具结果）
    - 其他: None（不参与去重）
    """
    role = msg.get("role", "")
    if role == "assistant":
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            first_id = tool_calls[0].get("id", "") if tool_calls else ""
            return f"assistant:tc:{first_id}"
        else:
            content = (msg.get("content", "") or "")[:50]
            return f"assistant:text:{content}"
    elif role == "tool":
        tool_call_id = msg.get("tool_call_id", "")
        return f"tool:{tool_call_id}"
    return None


async def persist_agent_reply(
    store, rv, history_len: int, full_reply: str, source: str = "electron",
    persisted_msgs: list[dict] | None = None
) -> tuple[str | None, str]:
    """持久化 Agent 回复消息（从 rv["messages"] 双管道），通知前端。

    从 runner.last_return_value 的 messages 中持久化新增的 tool/assistant 消息，
    处理纯文本回复回退，并推送 SSE 通知。

    Args:
        store: SessionStore 实例
        rv: runner.last_return_value（dict or None）
        history_len: 历史 messages 长度（用于切片 rv["messages"]）
        full_reply: Agent 完整回复文本（reply_chunks 拼接）

    Returns:
        (message_id, full_reply) — message_id 为最后一条 assistant 消息的 ID，
        full_reply 为原始回复文本（不过滤，保持原样）
    """
    message_id = None

    # DEBUG: Log rv structure for diagnosing tool_calls persistence
    if rv and isinstance(rv, dict) and rv.get("messages"):
        _debug_msgs = rv["messages"]
        logger.info(f"[persist DEBUG] rv has {len(_debug_msgs)} messages, history_len={history_len}, slice_start={history_len+1}")
        for i, m in enumerate(_debug_msgs[history_len+1:], start=history_len+1):
            _tc = m.get("tool_calls")
            _tci = m.get("tool_call_id", "")
            _role = m.get("role", "")
            _content_preview = (m.get("content", "") or "")[:50]
            logger.info(f"[persist DEBUG]   [{i}] role={_role} has_tool_calls={bool(_tc)} tool_call_id={_tci[:20]} content={_content_preview!r}")

    if rv and isinstance(rv, dict) and rv.get("messages"):
        # V4: 构建"已持久化消息"指纹集合，用于兜底去重
        _persisted_fingerprints = set()
        if persisted_msgs:
            for pm in persisted_msgs:
                fp = _msg_fingerprint(pm)
                if fp:
                    _persisted_fingerprints.add(fp)

        last_assistant_id = None
        last_assistant_content = ""
        for msg in rv["messages"][history_len + 1:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls")
            tool_call_id = msg.get("tool_call_id", "")

            if role == "system":
                continue
            if role == "user":
                continue

            # V4: 兜底去重——如果此消息已被逐条推送写入 DB，跳过
            fp = _msg_fingerprint(msg)
            if fp and fp in _persisted_fingerprints:
                continue

            if role == "tool" and tool_call_id:
                await store.add_message(role="tool", content=content or "", tool_call_id=tool_call_id)
            elif role == "assistant":
                pid = await store.add_message(role="assistant", content=content or "", tool_calls=tool_calls)
                last_assistant_id = pid
                last_assistant_content = content or ""

        # 纯文本回复不在 rv["messages"] 中，仅在逐条推送未执行时从 full_reply 持久化
        if not persisted_msgs and full_reply.strip() and full_reply.strip() != last_assistant_content.strip():
            pid = await store.add_message(role="assistant", content=full_reply)
            last_assistant_id = pid

        # V4: SSE通知 + message_id返回
        # 逐条推送已执行时，从persisted_msgs获取最后一条assistant消息的ID
        if persisted_msgs:
            for pm in reversed(persisted_msgs):
                if pm.get("role") == "assistant" and pm.get("_persisted_id"):
                    message_id = pm["_persisted_id"]
                    break
        elif last_assistant_id:
            # 兜底路径：逐条推送未执行，从rv["messages"]遍历获取
            message_id = last_assistant_id
            await notify_new_message(message_id, "assistant", full_reply, source=source)
    elif full_reply.strip():
        # 回退：无 return_value 时，从 full_reply 持久化 assistant 消息
        message_id = await store.add_message(role="assistant", content=full_reply)
        await notify_new_message(message_id, "assistant", full_reply, source=source)

    return message_id, full_reply


class ChatRequest(BaseModel):
    """Chat request model"""

    session_id: Optional[str] = None
    message: str
    system_prompt: Optional[str] = ""
    resources: list = []


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
        config.setdefault("provider", "")
        config.setdefault("litellm_kwargs", {})

        return config
    except Exception:
        return {"type": "openai", "apikey": "", "apibase": "", "model": "", "reasoning_effort": "", "provider": "", "litellm_kwargs": {}}


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
    if mcp_tools_schema and runner is not None:
        runner.set_mcp_tools_schema(mcp_tools_schema)


def get_or_create_runner() -> Optional["NiuRunner"]:
    """Get or create NiuRunner，配置变更后自动重新初始化"""
    from agent import runner as runner_module

    existing = get_runner()
    current = _load_llm_config()

    if existing is not None and current["apikey"] and current["model"]:
        # Runner 已存在，检查配置是否变更
        runner_llm = getattr(existing, "llm_config", {})
        if runner_llm.get("apikey") != current["apikey"] or runner_llm.get("model") != current["model"] or runner_llm.get("reasoning_effort") != current.get("reasoning_effort") or runner_llm.get("provider") != current.get("provider") or runner_llm.get("litellm_kwargs") != current.get("litellm_kwargs"):
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
    [DEPRECATED] Main chat endpoint - 使用 NiuRunner 流式响应
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
        # 排队等待锁：压缩管道可能执行数分钟，必须等够
        try:
            await asyncio.wait_for(_chat_lock.acquire(), timeout=600.0)
        except asyncio.TimeoutError:
            logger.warning("[/chat] _chat_lock 600s timeout, request rejected")
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

            # 方案 A：stream_error 时不进 DB（避免错误文本被下一轮 _inject_dynamic_resources 当 query 反复查 lightrag）
            if not stream_error:
                # 流式完成后持久化 Agent 回复（使用 persist_agent_reply 双管道）
                full_reply = "".join(reply_chunks)
                store = await get_message_store()
                rv = getattr(runner, "last_return_value", None)
                history_len = 0  # /chat 端点不加载历史，rv 包含完整 messages
                persisted_msgs = getattr(runner, "_persisted_msgs", None)  # V4: 已逐条持久化的消息
                message_id, full_reply = await persist_agent_reply(store, rv, history_len, full_reply, source="electron", persisted_msgs=persisted_msgs)
            else:
                full_reply = "".join(reply_chunks)
                store = await get_message_store()
                rv = getattr(runner, "last_return_value", None)
                message_id = None
                logger.warning(f"[Chat SSE] Skipped persist due to stream_error: {stream_error}")

            # 检测主 Agent 上下文溢出 → 同步触发 force 压缩（阻塞）
            if rv and isinstance(rv, dict) and rv.get("result") == "CONTEXT_OVERFLOW":
                overflow_data = rv.get("data", {})
                logger.warning(
                    f"[Chat SSE] Main agent CONTEXT_OVERFLOW at {overflow_data.get('tokens_used', 0)} tokens, "
                    f"triggering force compression (blocking)"
                )
                from niu_api.compat import _tidy_context_impl, _tidy_lock
                # 使用带超时的acquire避免AB-BA死锁：
                # SSE /chat 持有 _chat_lock → 等待 _tidy_lock，
                # 模式2压缩持有 _tidy_lock → 等待 _chat_lock
                _tidy_acquired = False
                try:
                    await asyncio.wait_for(_tidy_lock.acquire(), timeout=10.0)
                    _tidy_acquired = True
                except asyncio.TimeoutError:
                    logger.warning("[Chat SSE] Tidy lock busy, skipping force compression")
                if _tidy_acquired:
                    try:
                        tidy_result = await _tidy_context_impl(request={"session_id": session_id, "mode": "force"}, chat_lock_already_held=True)
                    finally:
                        _tidy_lock.release()
                    logger.info(f"[Chat SSE] Force compression result: {tidy_result.get('status')}")
            # Send final message
            yield f"data: {json.dumps({'done': True, 'session_id': session_id, 'message_id': message_id})}\n\n"
        finally:
            # 确保执行器线程完成后再释放锁，防止 runner.chat() 并发
            # 注意：finally 块中的 await 可能被 CancelledError（BaseException）中断，
            # 必须用嵌套 try/finally 保证 _chat_lock.release() 始终执行
            try:
                sf = locals().get("stream_future")
                if sf and not sf.done():
                    try:
                        await asyncio.wait_for(sf, timeout=30.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                        logger.warning("[/chat] Executor thread did not finish after client disconnect")
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

    # 排队等待锁：压缩管道可能执行数分钟，必须等够
    # 锁获取后立即进入 try/finally，确保 CancelledError 不会导致锁泄漏
    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=600.0)
    except asyncio.TimeoutError:
        logger.warning("[/chat/sync] _chat_lock 600s timeout, request rejected")
        raise HTTPException(
            status_code=503, detail="Another request is in progress, please try again later."
        )

    try:
        # Persist user message to database
        store = await get_message_store()
        user_msg_id = await store.add_message(role="user", content=request.message)
        # /chat/sync 当前只被 Electron 前端调用（scheduler 已走 ChatQueue 入队）
        await notify_new_message(user_msg_id, "user", request.message, source="electron")

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

        chat_error = None
        try:
            loop = asyncio.get_running_loop()
            full_reply = await loop.run_in_executor(None, sync_chat)
        except Exception as e:
            import traceback
            logger.error(f"Chat sync error: {e}\n{traceback.format_exc()}")
            chat_error = str(e)
            full_reply = f"Error: {str(e)}"

        # 方案 A：异常时不进 DB（避免错误文本被下一轮 _inject_dynamic_resources 当 query 反复查 lightrag）
        if chat_error is None:
            # 持久化 Agent 回复（使用 persist_agent_reply 双管道）
            rv = getattr(runner, "last_return_value", None)
            history_len = len(history_for_runner) if history_for_runner else 0
            persisted_msgs = getattr(runner, "_persisted_msgs", None)  # V4: 已逐条持久化的消息
            message_id, full_reply = await persist_agent_reply(store, rv, history_len, full_reply, source="electron", persisted_msgs=persisted_msgs)
        else:
            rv = getattr(runner, "last_return_value", None)
            message_id = None
            logger.warning(f"[Chat Sync] Skipped persist due to chat error: {chat_error}")

        # 检测主 Agent 上下文溢出 → 同步触发 force 压缩（阻塞，压缩完再继续）
        if rv and isinstance(rv, dict) and rv.get("result") == "CONTEXT_OVERFLOW":
            overflow_data = rv.get("data", {})
            logger.warning(
                f"[Chat] Main agent CONTEXT_OVERFLOW at {overflow_data.get('tokens_used', 0)} tokens, "
                f"triggering force compression (blocking)"
            )
            from niu_api.compat import _tidy_context_impl, _tidy_lock
            # 使用带超时的acquire避免AB-BA死锁（同SSE /chat路径）
            _tidy_acquired = False
            try:
                await asyncio.wait_for(_tidy_lock.acquire(), timeout=10.0)
                _tidy_acquired = True
            except asyncio.TimeoutError:
                logger.warning("[Chat] Tidy lock busy, skipping force compression")
            if _tidy_acquired:
                try:
                    tidy_result = await _tidy_context_impl(request={"session_id": session_id, "mode": "force"}, chat_lock_already_held=True)
                finally:
                    _tidy_lock.release()
                logger.info(f"[Chat] Force compression result: {tidy_result.get('status')}")
            # 压缩完成后不触发 auto_tidy（force 已包含完整3步整理）
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


@router.get("/api/chat/status")
async def chat_status():
    """返回当前 Agent 是否忙碌。用于前端窗口恢复时同步停止按钮状态。"""
    return {"busy": _chat_lock.locked()}


@router.post("/api/stop_all")
async def stop_all_subagents():
    """停止所有在跑的子 Agent（双击停止按钮触发）。

    停主 Agent 由前端单独发 /stop 处理（现有机制）。
    """
    from agent.runner import request_stop_all_subagents
    request_stop_all_subagents()
    return {"status": "ok"}


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
    runner = get_or_create_runner()
    if runner and runner.handler:
        runner.handler._last_prompt_tokens = 0
    return {"status": "ok", "session_id": session_id}
