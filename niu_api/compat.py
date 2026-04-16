"""
Compatibility API endpoints - matches the original Go API paths

These endpoints are used by the Electron UI (main.js).
"""

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
    message_id: Optional[str] = None


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

    # Save tool lifecycle scores before shutdown
    try:
        from agent.runner import get_runner
        runner = get_runner()
        if runner and hasattr(runner, 'tool_lifecycle'):
            runner.tool_lifecycle._save_scores()
            logger.info("Tool lifecycle scores saved on shutdown")
    except Exception as e:
        logger.warning(f"Failed to save tool lifecycle scores: {e}")

    logger.info("Python API ready for shutdown")
    return {"status": "shutting down"}


@router.post("/api/chat/session")
async def chat_session(request: ChatRequest) -> ChatResponse:
    """
    Chat endpoint - uses GenericAgentRunner with original GenericAgent code

    Uses runner.py which correctly imports from agent/generic/
    """
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
    message_id = None
    if full_reply.strip():
        message_id = await store.add_message(role="assistant", content=full_reply)

    return ChatResponse(reply=full_reply, session_id="default", message_id=message_id)


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
            "freed_tokens": int
        }
    """
    message_indices = request.get("message_indices", [])
    reason = request.get("reason", "Context compression")

    if not message_indices:
        return {"deleted_count": 0, "freed_tokens": 0}

    logger.info(f"[Context] Deleting {len(message_indices)} messages, reason: {reason}")

    # Get message store
    store = await get_message_store()

    # Get all messages to calculate freed tokens and get IDs
    all_messages = await store.get_messages(limit=1000)

    # Calculate freed tokens and collect IDs to delete
    freed_tokens = 0
    message_ids = []
    for idx in message_indices:
        if 0 <= idx < len(all_messages):
            msg = all_messages[idx]
            # Use litellm to calculate tokens
            try:
                from litellm import token_counter
                t = token_counter(model="gpt-4o", messages=[{"role": msg.role, "content": msg.content or ""}])
            except Exception:
                t = max(1, len(msg.content or "") // 2) + 4
            freed_tokens += t
            message_ids.append(msg.id)

    # Delete messages by IDs
    if message_ids:
        deleted_count = await store.delete_messages_by_ids(message_ids)
        logger.info(f"[Context] Deleted {deleted_count} messages, freed {freed_tokens} tokens")
        return {
            "deleted_count": deleted_count,
            "freed_tokens": freed_tokens,
        }

    return {"deleted_count": 0, "freed_tokens": 0}


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

        # 重置工具生命周期状态
        if hasattr(runner, 'tool_lifecycle'):
            runner.tool_lifecycle.clear()

        # Note: LLM session history is managed by ContextManager,
        # which reloads from message store each call.
        # store.clear_messages() above already clears persistent history.

    return {"success": True, "deleted_count": count}


@router.get("/api/pending-alerts")
async def get_pending_alerts() -> list:
    """Get pending alerts - delegates to alerts module"""
    from niu_api.alerts import get_and_clear_pending_alerts
    return get_and_clear_pending_alerts()


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
            "freed_tokens": int (optional)
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

        # Calculate per-message token counts
        message_count = len(messages)
        msg_tokens = []
        try:
            from litellm import token_counter
            for msg in messages:
                try:
                    t = token_counter(model="gpt-4o", messages=[{"role": msg.role, "content": msg.content or ""}])
                except Exception:
                    t = max(1, len(msg.content or "") // 2) + 4
                msg_tokens.append(t)
        except ImportError:
            msg_tokens = [max(1, len(msg.content or "") // 2) + 4 for msg in messages]
        estimated_tokens = sum(msg_tokens)

        # 读取上下文窗口大小（tokens）
        context_window_tokens = 200000  # 默认值
        try:
            import json
            from pathlib import Path
            prefs_path = Path.home() / ".niu" / "preferences.json"
            if prefs_path.exists():
                prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
                context_window_tokens = prefs.get("context", {}).get("contextWindowSize", 200000)
        except Exception:
            pass
        usage_percent = (estimated_tokens / context_window_tokens) * 100

        logger.info(f"[Tidy] Current context: {message_count} messages, {estimated_tokens} tokens, {usage_percent:.1f}%")

        # Prepare input for context-manager subagent
        if mode == "sleep":
            # Sleep mode: non-forced tidy
            prompt = f"""系统进入睡眠状态。

当前上下文：{estimated_tokens} tokens（{usage_percent:.1f}%）

消息列表：
共 {message_count} 条消息（idx 从小到大 = 从旧到新）

"""
            # Add message details
            for idx, msg in enumerate(messages):
                tokens = msg_tokens[idx]
                prompt += f"[idx:{idx}] {tokens}tokens {msg.role}: {msg.content[:100]}\n"

            prompt += "\n请按照【模式一：睡眠整理（非强制）】的规则处理。"

        else:
            # Force mode: not implemented yet
            logger.warning("[Tidy] Force mode not implemented yet")
            return {"status": "skipped", "message": "Force mode not implemented"}

        from agent.subagent import call_subagent
        from niu_api.chat import get_or_create_runner

        runner = get_or_create_runner()
        if not runner:
            logger.warning("[Tidy] Runner not initialized")
            return {"status": "error", "message": "Runner not initialized"}

        # 直接使用 runner 的 llm_config（包含 type 等完整字段）
        llm_config = runner.llm_config

        # Run subagent in thread pool (avoid blocking event loop)
        import asyncio

        if mode == "sleep":
            # 1. 先调梦境进化（增量学习+KG写入）
            # 读取增量游标
            import json
            from pathlib import Path
            cursor_path = Path.home() / ".niu" / "last_dream_evolve.json"
            last_message_idx = 0
            if cursor_path.exists():
                try:
                    cursor_data = json.loads(cursor_path.read_text(encoding="utf-8"))
                    last_message_idx = cursor_data.get("last_message_idx", 0)
                except Exception as e:
                    logger.warning(f"[Tidy] Failed to read dream cursor: {e}")

            dream_prompt = f"""系统进入睡眠状态，触发梦境进化。

当前上下文：{estimated_tokens} tokens（{usage_percent:.1f}%）

增量游标：上次处理到 idx={last_message_idx}，只处理 idx > {last_message_idx} 的新消息。
如果所有消息 idx 都 <= {last_message_idx}，说明没有新消息，直接报告"无新增消息"即可。

消息列表：
共 {message_count} 条消息（idx 从小到大 = 从旧到新）

"""
            for idx, msg in enumerate(messages):
                tokens = msg_tokens[idx]
                dream_prompt += f"[idx:{idx}] {tokens}tokens {msg.role}: {msg.content[:100]}\n"

            dream_prompt += f"\n请按照工作项1-7的顺序处理新增消息（idx > {last_message_idx}）。处理完成后，在报告末尾用 JSON 格式报告处理到的最大 idx，格式：{{\"last_message_idx\": <最大idx>}}。禁止使用 code_run 工具。"

            def run_dream_evolver():
                return call_subagent(
                    agent_name="dream-evolver",
                    task=dream_prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                )

            dream_result = await asyncio.to_thread(run_dream_evolver)
            logger.info(f"[Tidy] Dream-evolver result: {dream_result[:200]}")

            # 更新增量游标
            try:
                import re
                # 用 re.DOTALL 处理 LLM 可能输出的多行 JSON
                match = re.search(r'\{"last_message_idx"\s*:\s*(\d+)\}', dream_result, re.DOTALL)
                if match:
                    new_last_idx = int(match.group(1))
                else:
                    # regex 未匹配时保留旧游标，避免跳过未处理的消息
                    new_last_idx = last_message_idx
                    logger.warning(f"[Tidy] Dream cursor regex not matched, preserving last_message_idx={last_message_idx}")
                cursor_path.parent.mkdir(parents=True, exist_ok=True)
                cursor_data = {
                    "last_message_idx": new_last_idx,
                    "last_evolve_at": __import__("datetime").datetime.now().isoformat(),
                }
                cursor_path.write_text(json.dumps(cursor_data, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info(f"[Tidy] Dream cursor updated: last_message_idx={new_last_idx}")
            except Exception as e:
                logger.warning(f"[Tidy] Failed to update dream cursor: {e}")

        # 2. 再调内容管理（压缩删除）
        def run_context_manager():
            return call_subagent(
                agent_name="context-manager",
                task=prompt,
                llm_config=llm_config,
                mcp_client=None,
            )

        result = await asyncio.to_thread(run_context_manager)
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
