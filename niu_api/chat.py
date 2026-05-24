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
from niu_api.chat_queue import get_chat_queue

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
    # 双管道分离：tool 消息只走 DB 管道，不推送给前端
    if role == "tool":
        return
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
    # 双管道分离：tool 消息只走 DB 管道，不推送给前端
    if role == "tool":
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


async def persist_agent_reply(
    store, rv, history_len: int, full_reply: str
) -> tuple[str | None, str]:
    """持久化 Agent 回复消息（从 rv["messages"] 双管道），过滤 working_memory，通知前端。

    从 runner.last_return_value 的 messages 中持久化新增的 tool/assistant 消息，
    跳过 working_memory 虚拟调用，处理纯文本回复回退，并推送 SSE 通知。

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
        # 收集需要跳过的 tool_call_id（working_memory 虚拟调用）
        _wm_tool_call_ids = set()
        for msg in rv["messages"][history_len + 1:]:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if tc.get("function", {}).get("name") == "working_memory":
                        _wm_tool_call_ids.add(tc.get("id", ""))

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

            # 跳过 working_memory 虚拟消息
            if role == "assistant" and tool_calls:
                if any(tc.get("function", {}).get("name") == "working_memory" for tc in tool_calls):
                    continue
            if role == "tool" and tool_call_id in _wm_tool_call_ids:
                continue

            if role == "tool" and tool_call_id:
                await store.add_message(role="tool", content=content or "", tool_call_id=tool_call_id)
            elif role == "assistant":
                pid = await store.add_message(role="assistant", content=content or "", tool_calls=tool_calls)
                last_assistant_id = pid
                last_assistant_content = content or ""

        # 纯文本回复不在 rv["messages"] 中，需要从 full_reply 持久化
        if full_reply.strip() and full_reply.strip() != last_assistant_content.strip():
            pid = await store.add_message(role="assistant", content=full_reply)
            last_assistant_id = pid

        # 推送最后一条 assistant 消息给 SSE 订阅者
        if last_assistant_id:
            message_id = last_assistant_id
            await notify_new_message(message_id, "assistant", full_reply)
    elif full_reply.strip():
        # 回退：无 return_value 时，从 full_reply 持久化 assistant 消息
        message_id = await store.add_message(role="assistant", content=full_reply)
        await notify_new_message(message_id, "assistant", full_reply)

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
    Main chat endpoint - 入队后 SSE 心跳 keepalive，回复通过 notify_new_message 推送
    """
    llm_cfg = _load_llm_config()

    if not llm_cfg["apikey"]:
        raise HTTPException(
            status_code=400, detail="LLM not configured. Please set up API key first."
        )

    session_id = request.session_id or "default"
    q = get_chat_queue()
    result = await q.enqueue(
        content=request.message,
        source="frontend",
        session_id=session_id,
    )

    async def event_generator():
        # 立即发送入队确认
        yield f"data: {json.dumps({'type': 'queued', 'request_id': result.request_id})}\n\n"

        # 心跳 keepalive，直到 SSE 推送到达或超时
        heartbeat = 0
        while heartbeat < 240:  # 最多 240 次 * 0.5s = 120s
            await asyncio.sleep(0.5)
            heartbeat += 1
            yield ": heartbeat\n\n"

    return StreamingResponse(
        event_generator(),
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
    Sync chat endpoint - 使用 ChatQueue 排队等待回复
    """
    llm_cfg = _load_llm_config()

    if not llm_cfg["apikey"]:
        raise HTTPException(
            status_code=400, detail="LLM not configured. Please set up API key first."
        )

    session_id = request.session_id or "default"
    q = get_chat_queue()
    reply = await q.enqueue_and_wait(
        content=request.message,
        source="frontend",
        session_id=session_id,
    )
    return ChatResponse(reply=reply)


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
