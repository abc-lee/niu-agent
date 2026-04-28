"""
Compatibility API endpoints - matches the original Go API paths

These endpoints are used by the Electron UI (main.js).
"""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel
from fastapi import APIRouter
from loguru import logger
import asyncio

from agent.session import get_message_store

router = APIRouter(tags=["compat"])

# 并发锁：串行化所有 chat 请求，防止并发调用 runner.chat() 导致共享状态损坏
_chat_lock = asyncio.Lock()


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

    # Non-blocking acquire: reject if lock already held
    import asyncio

    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=0.01)
    except asyncio.TimeoutError:
        return ChatResponse(reply="系统正忙，请稍后再试", session_id="default")

    try:
        # Get message store
        store = await get_message_store()

        # Store user message
        user_msg_id = await store.add_message(role="user", content=request.message)
        # 通知 SSE 推送用户消息（前端用此 ID 给本地渲染的 user 气泡补上 data-id）
        from niu_api.chat import notify_new_message
        await notify_new_message(user_msg_id, "user", request.message)

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
            # 通知 SSE 端点推送给前端
            from niu_api.chat import notify_new_message
            await notify_new_message(message_id, "assistant", full_reply)

        return ChatResponse(reply=full_reply, session_id="default", message_id=message_id)
    finally:
        _chat_lock.release()


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
    """Delete messages by IDs

    Args:
        request: {
            "session_id": str (ignored),
            "message_ids": [str],
            "reason": str (optional)
        }

    Returns:
        {
            "deleted_count": int,
            "freed_tokens": int
        }
    """
    message_ids = request.get("message_ids", [])
    reason = request.get("reason", "Context compression")

    if not message_ids:
        return {"deleted_count": 0, "freed_tokens": 0}

    logger.info(f"[Context] Deleting {len(message_ids)} messages, reason: {reason}")

    store = await get_message_store()
    result = await store.delete_messages_by_ids(message_ids)
    logger.info(f"[Context] Deleted {result['deleted_count']} messages, freed {result['freed_tokens']} tokens")

    return result


@router.post("/api/context/messages/update")
async def update_context_message(request: dict) -> dict:
    """Update message content by ID.

    Args:
        request: {
            "session_id": str (ignored),
            "message_id": str,
            "content": str
        }

    Returns:
        {"status": "ok"} or {"status": "error", "message": str}
    """
    message_id = request.get("message_id")
    content = request.get("content")

    if not message_id or not content:
        return {"status": "error", "message": "message_id and content are required"}

    store = await get_message_store()
    updated = await store.update_message(message_id, content)

    if updated:
        logger.info(f"[Context] Updated message id={message_id}")
        return {"status": "ok"}
    return {"status": "error", "message": f"Message {message_id} not found"}


@router.post("/api/context/messages/add")
async def add_context_message(request: dict) -> dict:
    """Add a message to the session.

    Args:
        request: {
            "session_id": str (ignored),
            "role": str,
            "content": str
        }

    Returns:
        {"status": "ok", "message_id": str}
    """
    role = request.get("role")
    content = request.get("content")

    if not role or not content:
        return {"status": "error", "message": "role and content are required"}

    store = await get_message_store()
    msg_id = await store.add_message(role=role, content=content)

    return {"status": "ok", "message_id": msg_id}


@router.post("/api/chat/clear")
async def clear_chat() -> dict:
    """Clear all messages (for /new command)"""
    # 获取锁，防止与正在进行的 chat 冲突
    import asyncio

    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=0.01)
    except asyncio.TimeoutError:
        # 有 chat 正在进行，等它完成后再清
        await asyncio.sleep(1)
        try:
            await asyncio.wait_for(_chat_lock.acquire(), timeout=5.0)
        except asyncio.TimeoutError:
            return {"success": False, "error": "系统正忙，请稍后再试"}

    try:
        store = await get_message_store()
        count = await store.clear_messages()

        # 重置 runner 的所有状态
        from niu_api.chat import get_or_create_runner

        runner = get_or_create_runner()
        if runner:
            # 重置 handler 的工作记忆
            if runner.handler:
                runner.handler.reset_working_memory()

            # Note: LLM session history is managed by ContextManager,
            # which reloads from message store each call.
            # store.clear_messages() above already clears persistent history.

        # 清空临时目录（画框图片等）
        from agent.tmp_dir import cleanup_all_tmp
        cleaned_tmp = cleanup_all_tmp()

        return {"success": True, "deleted_count": count, "cleaned_tmp": cleaned_tmp}
    finally:
        _chat_lock.release()


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

        from agent.subagent import call_subagent
        from niu_api.chat import get_or_create_runner

        runner = get_or_create_runner()
        if not runner:
            logger.warning("[Tidy] Runner not initialized")
            return {"status": "error", "message": "Runner not initialized"}

        llm_config = runner.llm_config

        import json
        import re
        from pathlib import Path

        # 读取双游标（UUID 基准）
        dream_cursor_path = Path.home() / ".niu" / "last_dream_evolve.json"
        last_dream_evolve_id = ""
        if dream_cursor_path.exists():
            try:
                cursor_data = json.loads(dream_cursor_path.read_text(encoding="utf-8"))
                # 兼容旧格式（idx-based）和新格式（UUID-based）
                last_dream_evolve_id = cursor_data.get("last_dream_evolve_id", "")
                if not last_dream_evolve_id:
                    # 旧格式 fallback：last_message_idx → 留空，全量处理
                    logger.info("[Tidy] Old idx-based cursor detected, will do full processing")
            except Exception as e:
                logger.warning(f"[Tidy] Failed to read dream cursor: {e}")

        compress_cursor_path = Path.home() / ".niu" / "last_compress.json"
        last_compress_id = ""
        if compress_cursor_path.exists():
            try:
                cursor_data = json.loads(compress_cursor_path.read_text(encoding="utf-8"))
                last_compress_id = cursor_data.get("last_compress_id", "")
            except Exception as e:
                logger.warning(f"[Tidy] Failed to read compress cursor: {e}")

        # 构建消息列表（包含 UUID）
        msg_lines = []
        for idx, msg in enumerate(messages, 1):
            tokens = msg_tokens[idx - 1]
            msg_id = getattr(msg, "id", "") or ""
            msg_lines.append(f"[id:{msg_id}] [idx:{idx}] {tokens}tokens {msg.role}: {msg.content[:100]}")

        msg_list_text = "\n".join(msg_lines)

        if mode == "sleep":
            # Sleep mode: dream-evolver (增量) → context-manager (增量)

            # 1. dream-evolver prompt（UUID 游标，idx 判断时间顺序）
            if last_dream_evolve_id:
                dream_prompt = f"""系统进入睡眠状态，触发梦境进化。

当前上下文：{estimated_tokens} tokens（{usage_percent:.1f}%）

增量游标：上次处理到消息UUID={last_dream_evolve_id}，只处理该UUID对应idx之后的新消息。
游标用 id（UUID）存储（持久化），时间顺序用 idx 判断（idx 是动态位置索引，删除消息后会变，不能当游标存储）。UUID v4 字典序不代表时间先后。
如果在消息列表中找不到该UUID，或所有消息idx都 <= 游标idx，说明没有新消息，直接报告"无新增消息"即可。

消息列表：
共 {message_count} 条消息

{msg_list_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_dream_evolve_id": "<操作范围内 idx 最大的、且仍存在的消息的 id（UUID）>"}}
禁止使用 code_run 工具。"""
            else:
                dream_prompt = f"""系统进入睡眠状态，触发梦境进化。

当前上下文：{estimated_tokens} tokens（{usage_percent:.1f}%）

全量处理所有消息（无增量游标）。

消息列表：
共 {message_count} 条消息

{msg_list_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_dream_evolve_id": "<最后处理的消息UUID>"}}
禁止使用 code_run 工具。"""

            def run_dream_evolver():
                return call_subagent(
                    agent_name="dream-evolver",
                    task=dream_prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                )

            dream_result = await asyncio.to_thread(run_dream_evolver)
            logger.info(f"[Tidy] Dream-evolver result: {dream_result[:200]}")

            # 提取并写入 dream 游标（UUID）
            match = re.search(r'\{"last_dream_evolve_id"\s*:\s*"([^"]+)"\}', dream_result, re.DOTALL)
            new_dream_id = match.group(1) if match else last_dream_evolve_id
            if not match:
                logger.warning("[Tidy] Dream cursor UUID regex not matched, preserving old cursor")
            dream_cursor_path.parent.mkdir(parents=True, exist_ok=True)
            dream_cursor_path.write_text(json.dumps({
                "last_dream_evolve_id": new_dream_id,
                "last_evolve_at": datetime.now().isoformat(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"[Tidy] Dream cursor updated: last_dream_evolve_id={new_dream_id}")

            # 2. context-manager prompt（双游标，UUID 存储 + idx 判断时间顺序）
            prompt = f"""系统进入睡眠状态。

当前上下文：{estimated_tokens} tokens（{usage_percent:.1f}%）

双游标：last_compress_id={last_compress_id}，last_dream_evolve_id={new_dream_id}
操作范围：先从消息列表中找到游标UUID对应的idx，再处理 last_compress_idx < idx ≤ last_dream_evolve_idx 的消息。
游标用 id（UUID）存储（持久化），时间顺序用 idx 判断（idx 是动态位置索引，删除消息后会变，不能当游标存储）。UUID v4 字典序不代表时间先后。

消息列表：
共 {message_count} 条消息

{msg_list_text}

请按照【模式一：睡眠整理】的规则处理。处理完成后，在报告末尾用 JSON 格式报告：{{"last_compress_id": "<操作范围内 idx 最大的、且仍存在的消息的 id（UUID）>"}}"""

            def run_context_manager():
                return call_subagent(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                )

            result = await asyncio.to_thread(run_context_manager)
            logger.info(f"[Tidy] Context-manager result: {result[:200]}")

            # 提取并写入 compress 游标（UUID）
            match = re.search(r'\{"last_compress_id"\s*:\s*"([^"]+)"\}', result, re.DOTALL)
            if match:
                new_compress_id = match.group(1)
                compress_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                compress_cursor_path.write_text(json.dumps({
                    "last_compress_id": new_compress_id,
                    "last_compress_at": datetime.now().isoformat(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info(f"[Tidy] Compress cursor updated: last_compress_id={new_compress_id}")
            else:
                logger.warning("[Tidy] Sleep: Compress cursor UUID regex not matched, cursor not updated")

            return {
                "status": "success",
                "message": f"Context tidied: {message_count} messages processed",
                "result": result,
            }

        elif mode == "force":
            # Force mode: dream-evolver 全量 → context-manager 强制压缩
            logger.info("[Tidy] Force mode: starting dream-evolver (full processing)")

            dream_prompt = f"""系统上下文超过阈值，触发强制压缩。

当前上下文：{estimated_tokens} tokens（{usage_percent:.1f}%）

全量处理所有消息（不使用增量游标）。

消息列表：
共 {message_count} 条消息

{msg_list_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_dream_evolve_id": "<最后处理的消息UUID>"}}。禁止使用 code_run 工具。"""

            def run_dream_evolver_force():
                return call_subagent(
                    agent_name="dream-evolver",
                    task=dream_prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                )

            dream_result = await asyncio.to_thread(run_dream_evolver_force)
            logger.info(f"[Tidy] Force: dream-evolver completed, length={len(dream_result)}")

            # 提取并写入 dream 游标
            match = re.search(r'\{"last_dream_evolve_id"\s*:\s*"([^"]+)"\}', dream_result, re.DOTALL)
            new_dream_id = match.group(1) if match else last_dream_evolve_id
            if not match:
                logger.warning("[Tidy] Force: Dream cursor UUID regex not matched, preserving old cursor")
            if new_dream_id:
                dream_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                dream_cursor_path.write_text(json.dumps({
                    "last_dream_evolve_id": new_dream_id,
                    "last_evolve_at": datetime.now().isoformat(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")

            # context-manager force prompt
            # 重新读取 compress 游标
            last_compress_id = ""
            if compress_cursor_path.exists():
                try:
                    cdata = json.loads(compress_cursor_path.read_text(encoding="utf-8"))
                    last_compress_id = cdata.get("last_compress_id", "")
                except Exception:
                    pass

            target_tokens = int(estimated_tokens * 0.5)
            prompt = f"""系统上下文超过阈值，触发强制压缩。

当前上下文：{estimated_tokens} tokens（{usage_percent:.1f}%）
目标上下文：{target_tokens} tokens（需要删除至少 {estimated_tokens - target_tokens} tokens）

强制压缩不受双游标范围限制，可以操作所有消息。
安全边界：先从消息列表中找到 last_dream_evolve_id={new_dream_id} 对应的 idx，idx > 该idx 的消息（dream-evolver 未提取知识），不得直接删除，必须用 update_message 压缩为L0摘要后保留（不删除）。
保护规则：操作开始时记录 idx 最大的 10 条消息的 id（UUID），这些消息绝不删除（按 id 判断，不受后续 idx 变化影响）。
游标用 id（UUID）存储（持久化），时间顺序用 idx 判断（idx 是动态位置索引，删除消息后会变，不能当游标存储）。UUID v4 字典序不代表时间先后。

消息列表：
共 {message_count} 条消息

{msg_list_text}

请按照【模式三：强制压缩】的规则处理。处理完成后，在报告末尾用 JSON 格式报告：{{"last_compress_id": "<操作范围内 idx 最大的、且仍存在的消息的 id（UUID）>"}}"""

            def run_context_manager_force():
                return call_subagent(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                )

            result = await asyncio.to_thread(run_context_manager_force)
            logger.info(f"[Tidy] Force: context-manager completed, length={len(result)}")

            # 提取并写入 compress 游标
            match = re.search(r'\{"last_compress_id"\s*:\s*"([^"]+)"\}', result, re.DOTALL)
            if match:
                new_compress_id = match.group(1)
                compress_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                compress_cursor_path.write_text(json.dumps({
                    "last_compress_id": new_compress_id,
                    "last_compress_at": datetime.now().isoformat(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                logger.warning("[Tidy] Force: Compress cursor UUID regex not matched, cursor not updated")

            return {"status": "ok", "mode": "force", "tokens_before": estimated_tokens}

        else:
            logger.warning(f"[Tidy] Unknown mode: {mode}, skipping")
            return {"status": "error", "message": f"Unknown mode: {mode}. Use 'sleep' or 'force'."}

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
