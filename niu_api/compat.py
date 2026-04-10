"""
Compatibility API endpoints - matches the original Go API paths

These endpoints are used by the Electron UI (main.js).
"""

import os
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel
from fastapi import APIRouter
from loguru import logger

from agent.session import get_message_store

router = APIRouter(tags=["compat"])


class ChatRequest(BaseModel):
    """Chat request"""

    message: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    """Chat response"""

    reply: str
    session_id: Optional[str] = None


class MessageResponse(BaseModel):
    """Single message response"""

    id: str
    role: str
    content: str
    created_at: str


class MessagesResponse(BaseModel):
    """Messages list response"""

    messages: List[MessageResponse]
    total_in_db: int


class StatsResponse(BaseModel):
    """Stats response"""

    messages: int
    uptime: str


# Track startup time
_startup_time = datetime.now()

# Preload status
_preload_complete = False


def set_preload_complete():
    """Mark preload as complete"""
    global _preload_complete
    _preload_complete = True
    logger.info("Preload marked as complete")


@router.get("/api/llm-status")
async def get_llm_status() -> dict:
    """检测 LLM 是否已配置可用（直接从文件读取，不走缓存）"""
    import json
    from pathlib import Path

    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        llm = data.get("llm", {})
        api_key = llm.get("apiKey", "")
        api_base = llm.get("apiBase", "")
        model = llm.get("model", "")

        if not api_key:
            return {"ready": False, "error": "API key not configured"}
        if not api_base or not model:
            return {"ready": False, "error": "API base or model not configured"}
        return {"ready": True}
    except Exception as e:
        return {"ready": False, "error": str(e)}


@router.get("/api/preload-status")
async def get_preload_status():
    """Get preload status - used by Go launcher to wait before showing window"""
    return {"ready": _preload_complete, "uptime": str(datetime.now() - _startup_time).split(".")[0]}


@router.get("/api/stats")
async def get_stats() -> StatsResponse:
    """Get system stats"""
    store = await get_message_store()
    messages = await store.count_messages()
    uptime = str(datetime.now() - _startup_time).split(".")[0]
    return StatsResponse(messages=messages, uptime=uptime)


@router.post("/api/shutdown")
async def shutdown():
    """Shutdown the server gracefully"""
    logger.info("Shutdown requested via API")

    # Close vector search database connection
    from agent.vector_search import get_vector_search

    try:
        vector_search = get_vector_search()
        vector_search.close()
        logger.info("Vector search connection closed")
    except Exception as e:
        logger.warning(f"Failed to close vector search: {e}")

    # Stop embedding-service subprocess
    try:
        from niu_api.__main__ import stop_embedding_service

        stop_embedding_service()
        logger.info("Embedding-service stopped")
    except Exception as e:
        logger.warning(f"Failed to stop embedding-service: {e}")

    logger.info("Python API ready for shutdown")
    return {"status": "shutting down"}


@router.post("/api/chat/session")
async def chat_session(request: ChatRequest) -> ChatResponse:
    """
    Chat endpoint - uses GenericAgentRunner with original GenericAgent code

    Uses runner.py which correctly imports from agent/generic/
    """
    from agent.runner import get_runner
    from niu_api.config import get_config

    config = get_config()

    if not config.llm or not config.llm.api_key:
        return ChatResponse(reply="Error: LLM not configured, please set API Key first")

    # Get message store
    store = await get_message_store()

    # Store user message
    await store.add_message(role="user", content=request.message)

    # P1-1: 使用 ContextManager 加载历史（统一管理）
    from agent.context_manager import get_context_manager

    context_manager = await get_context_manager(store)
    history_for_runner = await context_manager.get_context_for_chat(exclude_last=True)

    logger.info(f"Loaded {len(history_for_runner)} history messages")

    # Get runner (uses original GenericAgent from agent/generic/)
    # Use the pre-initialized runner from niu_api/chat.py which has MCP tools
    from niu_api.chat import get_or_create_runner

    runner = get_or_create_runner()

    # Create a simple session_id (no session concept, but runner needs one)
    session_id = "default"

    # Run chat using asyncio.to_thread to avoid blocking event loop
    def sync_chat():
        chunks = []
        for chunk in runner.chat(session_id, request.message, stream=False, history=history_for_runner):
            chunks.append(chunk)
        return "".join(chunks)

    import asyncio

    try:
        full_reply = await asyncio.to_thread(sync_chat)
    except Exception as e:
        import traceback
        logger.error(f"Chat error: {e}\n{traceback.format_exc()}")
        full_reply = f"Error: {str(e)}"

    # Store assistant response
    if full_reply.strip():
        await store.add_message(role="assistant", content=full_reply)

    return ChatResponse(reply=full_reply, session_id="default")


@router.get("/api/context/messages")
async def get_context_messages(
    limit: int = 100, before_id: Optional[str] = None, full: bool = False, session_id: Optional[str] = None
) -> MessagesResponse:
    """Get messages

    Args:
        limit: Number of messages to return (default 100)
        before_id: Get messages before this ID (for pagination)
        full: If True, return full content (for context-manager)
        session_id: Ignored (kept for compatibility with session-manager)
    """
    store = await get_message_store()
    messages = await store.get_messages(limit, before_id)
    total = await store.count_messages()

    return MessagesResponse(
        messages=[
            MessageResponse(
                id=msg.id, role=msg.role, content=msg.content, created_at=msg.created_at
            )
            for msg in messages
        ],
        total_in_db=total,
    )


@router.post("/api/context/messages/delete")
async def delete_context_messages(request: dict) -> dict:
    """Delete messages by indices

    Args:
        request: {
            "session_id": str (ignored),
            "message_indices": [int],
            "reason": str (optional)
        }

    Returns:
        {
            "deleted_count": int,
            "freed_kb": float
        }
    """
    message_indices = request.get("message_indices", [])
    reason = request.get("reason", "Context compression")

    if not message_indices:
        return {"deleted_count": 0, "freed_kb": 0}

    logger.info(f"[Context] Deleting {len(message_indices)} messages, reason: {reason}")

    # Get message store
    store = await get_message_store()

    # Get all messages to calculate freed KB and get IDs
    all_messages = await store.get_messages(limit=1000)

    # Calculate freed KB and collect IDs to delete
    freed_kb = 0.0
    message_ids = []
    for idx in message_indices:
        if 0 <= idx < len(all_messages):
            msg = all_messages[idx]
            freed_kb += len(msg.content or "") / 1024
            message_ids.append(msg.id)

    # Delete messages by IDs
    if message_ids:
        deleted_count = await store.delete_messages_by_ids(message_ids)
        logger.info(f"[Context] Deleted {deleted_count} messages, freed {freed_kb:.1f} KB")
        return {
            "deleted_count": deleted_count,
            "freed_kb": round(freed_kb, 1),
        }

    return {"deleted_count": 0, "freed_kb": 0}


@router.post("/api/chat/clear")
async def clear_chat() -> dict:
    """Clear all messages (for /new command)"""
    store = await get_message_store()
    count = await store.clear_messages()

    # 重置 runner 的所有状态
    from niu_api.chat import get_or_create_runner

    runner = get_or_create_runner()
    if runner:
        # 重置 handler 的工作记忆
        if runner.handler:
            runner.handler.reset_working_memory()

        # 重置 LLM session 的 history（内存缓存）
        if runner.client and hasattr(runner.client, 'backend'):
            if hasattr(runner.client.backend, 'history'):
                runner.client.backend.history = []
                logger.info("Cleared LLM session history")

    return {"success": True, "deleted_count": count}


@router.get("/api/pending-alerts")
async def get_pending_alerts() -> dict:
    """Get pending alerts - placeholder for now"""
    return {"alerts": []}


@router.post("/api/context/tidy")
async def tidy_context(request: dict):
    """
    Tidy context when entering sleep mode or forced compression

    Args:
        request: {
            "session_id": str,
            "mode": "sleep" | "force"
        }

    Returns:
        {
            "status": "success",
            "message": str,
            "freed_kb": int (optional)
        }
    """
    session_id = request.get("session_id", "default")
    mode = request.get("mode", "sleep")

    logger.info(f"[Tidy] Context tidy triggered: session={session_id}, mode={mode}")

    try:
        # Get message store
        store = await get_message_store()
        messages = await store.get_messages(limit=100)

        if not messages:
            logger.info("[Tidy] No messages to tidy")
            return {"status": "success", "message": "No messages to tidy"}

        # Calculate approximate context size (in KB)
        total_kb = sum(len(msg.content or "") for msg in messages) / 1024
        message_count = len(messages)

        logger.info(f"[Tidy] Current context: {message_count} messages, {total_kb:.1f} KB")

        # Prepare input for context-manager subagent
        if mode == "sleep":
            # Sleep mode: non-forced tidy
            prompt = f"""系统进入睡眠状态。

当前上下文：{total_kb:.1f} KB

消息列表：
共 {message_count} 条消息（idx 从小到大 = 从旧到新）

"""
            # Add message details
            for idx, msg in enumerate(messages):
                kb = len(msg.content or "") / 1024
                prompt += f"[idx:{idx}] {kb:.1f}KB {msg.role}: {msg.content[:100]}\n"

            prompt += "\n请按照【模式一：睡眠整理（非强制）】的规则处理。"

        else:
            # Force mode: not implemented yet
            logger.warning("[Tidy] Force mode not implemented yet")
            return {"status": "skipped", "message": "Force mode not implemented"}

        # Call context-manager subagent
        from agent.subagent import call_subagent
        from niu_api.chat import get_or_create_runner
        from niu_api.config import get_config

        runner = get_or_create_runner()
        if not runner:
            logger.warning("[Tidy] Runner not initialized")
            return {"status": "error", "message": "Runner not initialized"}

        config = get_config()
        llm_config = {
            "apikey": config.llm.api_key if config.llm else "",
            "apibase": config.llm.api_base if config.llm else "",
            "model": config.llm.model if config.llm else "",
        }

        # Run subagent in thread pool (avoid blocking event loop)
        import asyncio

        def run_subagent():
            return call_subagent(
                agent_name="context-manager",
                task=prompt,
                llm_config=llm_config,
                mcp_client=runner.mcp_client,
            )

        result = await asyncio.to_thread(run_subagent)

        logger.info(f"[Tidy] Context-manager result: {result[:200]}")

        return {
            "status": "success",
            "message": f"Context tidied: {message_count} messages processed",
            "result": result,
        }

    except Exception as e:
        import traceback
        logger.error(f"[Tidy] Error: {e}\n{traceback.format_exc()}")
        return {"status": "error", "message": str(e)}


@router.post("/api/vector/cleanup")
async def trigger_vector_cleanup():
    """手动触发向量库清理"""
    from agent.vector_cleanup import get_cleanup_service

    try:
        cleanup = get_cleanup_service()
        cleanup.run_full_cleanup()
        return {"status": "success", "message": "Vector database cleanup completed"}
    except Exception as e:
        logger.error(f"Vector cleanup failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/api/vector/stats")
async def get_vector_stats():
    """获取向量库统计信息"""
    import json
    import os
    from agent.vector_search import get_vector_search

    vs = get_vector_search()
    conn = vs._get_connection()
    if conn is None:
        return {"error": "Vector database not initialized"}

    cursor = conn.cursor()

    # 总数
    cursor.execute("SELECT COUNT(*) FROM documents")
    total = cursor.fetchone()[0]

    # 按类别统计
    cursor.execute(
        """
        SELECT json_extract(metadata, '$.category') as category, COUNT(*) as count
        FROM documents
        GROUP BY category
        """
    )
    by_category = {row[0] or "unknown": row[1] for row in cursor.fetchall()}

    # 按层级统计
    cursor.execute(
        """
        SELECT json_extract(metadata, '$.level') as level, COUNT(*) as count
        FROM documents
        GROUP BY level
        """
    )
    by_level = {row[0] or "unknown": row[1] for row in cursor.fetchall()}

    # 数据库大小
    db_size_mb = os.path.getsize(vs.db_path) / (1024 * 1024) if os.path.exists(vs.db_path) else 0

    return {
        "total": total,
        "by_category": by_category,
        "by_level": by_level,
        "db_size_mb": round(db_size_mb, 2),
        "db_path": vs.db_path,
    }
