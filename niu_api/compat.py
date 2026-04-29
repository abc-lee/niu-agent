"""
Compatibility API endpoints - matches the original Go API paths

These endpoints are used by the Electron UI (main.js).
"""

import asyncio
import json
import os
import re
from datetime import datetime

from agent.session import get_message_store
from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel


def _extract_cursor_id(text: str, field_name: str, valid_ids: set) -> str | None:
    """
    从文本中提取游标 UUID 并验证其存在于消息列表中。

    支持多种 JSON 格式变体：
    - {"field": "uuid"}
    - {"field":"uuid"}（无空格）
    - {"field" : "uuid"}（多空格）
    - 带换行符的 JSON

    Args:
        text: 待搜索的文本（子 Agent 结果或 partial_result）
        field_name: 游标字段名（如 "last_entity_extract_id"）
        valid_ids: 当前消息列表中有效的 UUID 集合

    Returns:
        验证通过的 UUID，或 None（未找到或无效）
    """
    if not text:
        return None
    # 宽松匹配：允许各种空白格式
    pattern = rf'\{{\s*"{re.escape(field_name)}"\s*:\s*"([^"]+)"\s*'
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return None
    candidate = match.group(1)
    if valid_ids is not None and candidate not in valid_ids:
        logger.warning(f"[Tidy] Extracted {field_name}={candidate} not in message list, discarding")
        return None
    return candidate


def _is_subagent_overflow(result: str) -> bool:
    """检测子 Agent 是否因上下文溢出而退出（需匹配 overflow + agent + tokens_used 三个特征键）"""
    if not result or not result.strip().startswith("{"):
        return False
    try:
        data = json.loads(result)
        return (
            isinstance(data, dict)
            and data.get("overflow") is True
            and "agent" in data
            and "tokens_used" in data
        )
    except (json.JSONDecodeError, ValueError):
        return False


def _extract_overflow_info(result: str) -> dict:
    """从子 Agent 溢出报告中提取信息"""
    try:
        return json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return {"overflow": True, "raw": result}


def _build_incremental_msg_text(messages, last_cursor_id: str, out_msg_ids: list, msg_tokens: list | None = None) -> str:
    """
    构建增量消息文本：只包含游标之后的新消息。

    Args:
        messages: 全量消息列表
        last_cursor_id: 上次处理到的消息 UUID（空字符串表示全量）
        out_msg_ids: 输出参数，收集增量消息的 UUID 列表
        msg_tokens: 每条消息的 token 数列表（与 messages 等长），None 则不注解

    Returns:
        格式化的消息文本
    """
    # 找到游标位置
    cursor_idx = -1
    if last_cursor_id:
        for i, msg in enumerate(messages):
            msg_id = getattr(msg, "id", "") or ""
            if msg_id == last_cursor_id:
                cursor_idx = i
                break
        if cursor_idx < 0:
            logger.warning(f"[Tidy] Cursor UUID {last_cursor_id} not found in message list, degrading to full processing")

    # 只取游标之后的消息
    start = cursor_idx + 1 if cursor_idx >= 0 else 0
    lines = []
    for i, msg in enumerate(messages[start:]):
        idx = start + i + 1  # 1-based display index
        msg_id = getattr(msg, "id", "") or ""
        out_msg_ids.append(msg_id)
        content = msg.content or ""
        token_annotation = ""
        if msg_tokens and (start + i) < len(msg_tokens):
            token_annotation = f"{msg_tokens[start + i]}tokens "
        lines.append(f"[id:{msg_id}] [idx:{idx}] {token_annotation}{msg.role}: {content}")

    if not lines:
        return "（无新增消息）"

    return f"共 {len(lines)} 条新消息\n\n" + "\n".join(lines)


def truncate_message_content(content: str, max_chars: int = 500) -> str:
    """
    截断单条消息内容（用于雪球式压缩的 force 模式）。

    保留前 max_chars 个字符，附加截断标记和原始长度信息。
    子 Agent 可通过 get_messages 工具查看完整内容。

    Args:
        content: 原始消息内容
        max_chars: 保留的最大字符数

    Returns:
        截断后的内容（短于 max_chars 的内容不截断）
    """
    if not content:
        return ""
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + f"...[截断，原内容{len(content)}字符，可用get_messages查看]"


def build_truncated_msg_list_text(
    messages,
    truncate: bool = False,
    max_chars: int = 500,
    max_messages: int = 0,
) -> str:
    """
    构建消息列表文本，可选截断单条消息内容和限制消息数量。

    Args:
        messages: 消息对象列表
        truncate: 是否截断消息内容（force 模式用 True）
        max_chars: 截断时保留的最大字符数
        max_messages: 最大消息数量。0 = 包含全部。设置后只保留最近 N 条，
            并在开头注明省略了多少条早期消息。

    Returns:
        格式化的消息列表文本
    """
    total = len(messages)
    omitted = 0
    if max_messages > 0 and total > max_messages:
        omitted = total - max_messages
        messages = messages[-max_messages:]

    lines = []
    if omitted > 0:
        lines.append(f"[省略了前 {omitted} 条消息，仅显示最近 {max_messages} 条]")
        lines.append("")

    for idx, msg in enumerate(messages, 1):
        msg_id = getattr(msg, "id", "") or ""
        content = msg.content or ""
        if truncate:
            content = truncate_message_content(content, max_chars=max_chars)
        lines.append(f"[id:{msg_id}] [idx:{idx}] {msg.role}: {content}")
    return "\n".join(lines)


def _should_auto_tidy(current_tokens: int, last_tidy_tokens: int, threshold: int = 50000) -> bool:
    """
    判断是否应该触发自动增量整理。

    Args:
        current_tokens: 当前消息总 token 数
        last_tidy_tokens: 上次整理时的总 token 数（0 表示从未整理）
        threshold: 触发阈值（增量 token 数）

    Returns:
        True 表示应该触发整理
    """
    if current_tokens <= 0:
        return False
    increment = current_tokens - last_tidy_tokens
    # 从未整理过：总量超阈值就触发
    if last_tidy_tokens == 0:
        return current_tokens >= threshold
    return increment >= threshold


async def _check_and_trigger_auto_tidy(store):
    """
    检查是否需要自动增量整理，如需要则异步触发。

    在每次 assistant 消息写入后调用。
    整理以 sleep 模式异步执行，不阻塞 chat 响应。
    不做 locked() 前置检查 — _run_auto_tidy 内部的锁机制处理重入保护。
    """
    try:
        messages = await store.get_messages()
        if not messages:
            return

        from agent.subagent import count_tokens_for_text
        total_content = "".join(getattr(m, "content", "") or "" for m in messages)
        current_tokens = count_tokens_for_text(total_content)

        last_tidy_tokens = _read_last_tidy_tokens()

        if not _should_auto_tidy(current_tokens, last_tidy_tokens):
            return

        logger.info(f"[AutoTidy] Increment {current_tokens - last_tidy_tokens} tokens exceeds threshold, triggering sleep tidy")

        # 异步触发 sleep 模式整理（_run_auto_tidy 内部有 _tidy_lock 防重入）
        asyncio.create_task(_run_auto_tidy())
    except Exception as e:
        logger.warning(f"[AutoTidy] Check failed: {e}")


def _read_last_tidy_tokens() -> int:
    """读取上次整理时的总 token 数。"""
    try:
        from pathlib import Path
        path = Path.home() / ".niu" / "last_tidy_tokens.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("total_tokens", 0)
    except Exception as e:
        logger.warning(f"[AutoTidy] Failed to read last_tidy_tokens: {e}")
    return 0


def _write_last_tidy_tokens(total_tokens: int):
    """写入当前整理时的总 token 数。"""
    try:
        from pathlib import Path
        path = Path.home() / ".niu" / "last_tidy_tokens.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "total_tokens": total_tokens,
            "updated_at": datetime.now().isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[AutoTidy] Failed to write last_tidy_tokens: {e}")


_tidy_lock = asyncio.Lock()


async def _run_auto_tidy():
    """自动整理：非阻塞获取锁，避免与手动触发竞争或无限阻塞。"""
    try:
        # 前置检查：锁已被占用则直接跳过
        if _tidy_lock.locked():
            logger.info("[AutoTidy] Tidy already running, skipping")
            return
        # 极小概率竞态：locked() 返回 False 但获取前被抢占
        # 此时 async with 会阻塞直到锁释放，但不会死锁
        async with _tidy_lock:
            result = await _tidy_context_impl(request={"session_id": "default", "mode": "sleep"})
            if result.get("status") != "error":
                store = await get_message_store()
                messages = await store.get_messages()
                from agent.subagent import count_tokens_for_text
                total_content = "".join(getattr(m, "content", "") or "" for m in messages)
                current_tokens = count_tokens_for_text(total_content)
                _write_last_tidy_tokens(current_tokens)
                logger.info(f"[AutoTidy] Completed, last_tidy_tokens updated to {current_tokens}")
            else:
                logger.warning(f"[AutoTidy] tidy_context returned error: {result}")
    except Exception as e:
        logger.warning(f"[AutoTidy] Failed: {e}")


router = APIRouter(tags=["compat"])

# 并发锁：串行化所有 chat 请求，防止并发调用 runner.chat() 导致共享状态损坏
_chat_lock = asyncio.Lock()


class ChatRequest(BaseModel):
    """Chat request"""

    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    """Chat response"""

    reply: str
    session_id: str | None = None
    message_id: str | None = None


class MessageResponse(BaseModel):
    """Single message response"""

    id: str
    role: str
    content: str
    created_at: str


class MessagesResponse(BaseModel):
    """Messages list response"""

    messages: list[MessageResponse]
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
    except TimeoutError:
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

        # 检测主 Agent 上下文溢出 → 同步触发 force 压缩（阻塞）
        rv = getattr(runner, "last_return_value", None)
        if rv and isinstance(rv, dict) and rv.get("result") == "CONTEXT_OVERFLOW":
            overflow_data = rv.get("data", {})
            logger.warning(
                f"[Chat Session] Main agent CONTEXT_OVERFLOW at {overflow_data.get('tokens_used', 0)} tokens, "
                f"triggering force compression (blocking)"
            )
            async with _tidy_lock:
                tidy_result = await _tidy_context_impl(request={"session_id": session_id, "mode": "force"})
            logger.info(f"[Chat Session] Force compression result: {tidy_result.get('status')}")
        else:
            # 正常：异步触发增量整理检查（不阻塞）
            if full_reply.strip():
                await _check_and_trigger_auto_tidy(store)

        return ChatResponse(reply=full_reply, session_id="default", message_id=message_id)
    finally:
        _chat_lock.release()


@router.get("/api/context/messages")
async def get_context_messages(
    limit: int = 100, before_id: str | None = None, full: bool = False, session_id: str | None = None
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
    except TimeoutError:
        # 有 chat 正在进行，等它完成后再清
        await asyncio.sleep(1)
        try:
            await asyncio.wait_for(_chat_lock.acquire(), timeout=5.0)
        except TimeoutError:
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
    # 加锁防止并发：手动触发和自动触发互斥
    async with _tidy_lock:
        return await _tidy_context_impl(request)


async def _tidy_context_impl(request: dict):
    """tidy_context 的内部实现（不加锁，由调用方负责并发控制）。"""
    session_id = request.get("session_id", "default")
    mode = request.get("mode", "sleep")

    logger.info(f"[Tidy] Context tidy triggered: session={session_id}, mode={mode}")

    try:
        # Get message store
        store = await get_message_store()
        messages = await store.get_messages()

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
        except Exception as e:
            logger.warning(f"[Tidy] Failed to read preferences for context window size: {e}")
            # 保留默认 context_window_tokens = 200000，不影响游标
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
        from pathlib import Path

        # 读取三游标（UUID 基准）
        entity_cursor_path = Path.home() / ".niu" / "last_entity_extract.json"
        last_entity_extract_id = ""
        if entity_cursor_path.exists():
            try:
                cursor_data = json.loads(entity_cursor_path.read_text(encoding="utf-8"))
                last_entity_extract_id = cursor_data.get("last_entity_extract_id", "")
            except Exception as e:
                logger.warning(f"[Tidy] Failed to read entity cursor: {e}")

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

        # 构建消息列表（包含 UUID，完整内容不截断）
        # 真实环境下 force 模式触发时上下文约 170K tokens（85%阈值）
        # 全量消息列表 ≤ 190K tokens，子 Agent 200K 窗口有 15% 输出空间，不会溢出
        msg_lines = []
        msg_ids = []
        for idx, msg in enumerate(messages, 1):
            tokens = msg_tokens[idx - 1]
            msg_id = getattr(msg, "id", "") or ""
            msg_ids.append(msg_id)
            msg_lines.append(f"[id:{msg_id}] [idx:{idx}] {tokens}tokens {msg.role}: {msg.content}")

        msg_list_text = "\n".join(msg_lines)
        msg_id_set = set(msg_ids)  # 用于游标 ID 有效性校验

        if mode == "sleep":
            # Sleep mode: entity-extractor (增量) → dream-evolver (增量) → context-manager (增量)

            # 1/3. entity-extractor（增量，非破坏性）
            entity_msg_ids = []
            entity_incremental_text = _build_incremental_msg_text(messages, last_entity_extract_id, entity_msg_ids, msg_tokens)
            entity_prompt = f"""请从以下消息中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

{entity_incremental_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_entity_extract_id": "<操作范围内 idx 最大的、且仍存在的消息的 id（UUID）>"}}"""
            if entity_msg_ids:
                logger.info(f"[Tidy] entity-extractor: {len(entity_msg_ids)} new messages since cursor")

                def run_entity_extractor():
                    return call_subagent(
                        agent_name="entity-extractor",
                        task=entity_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                    )

                entity_result = await asyncio.to_thread(run_entity_extractor)
                logger.info(f"[Tidy] entity-extractor result: {entity_result[:200]}")

                if _is_subagent_overflow(entity_result):
                    overflow_info = _extract_overflow_info(entity_result)
                    logger.warning(f"[Tidy] entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # 溢出时尝试从 partial_result 提取游标，避免游标停滞导致无限重复处理
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_entity_extract_id", msg_id_set)
                    if recovered:
                        new_entity_id = recovered
                        logger.info(f"[Tidy] Entity cursor recovered from partial_result: {new_entity_id}")
                    else:
                        # 无法提取游标 → 保留旧游标（宁可重复处理也不丢失知识）
                        new_entity_id = last_entity_extract_id
                        logger.warning(f"[Tidy] Entity cursor preserved at {last_entity_extract_id} to prevent knowledge loss")
                else:
                    # 成功：从子 Agent 输出提取实际处理位置
                    extracted = _extract_cursor_id(entity_result, "last_entity_extract_id", msg_id_set)
                    if extracted:
                        new_entity_id = extracted
                    else:
                        # 提取失败 → 保留旧游标（重复处理优于知识丢失，与 force 模式一致）
                        new_entity_id = last_entity_extract_id
                        logger.warning("[Tidy] Entity cursor regex not matched, preserving old cursor to prevent knowledge loss")

                if new_entity_id:
                    entity_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                    entity_cursor_path.write_text(json.dumps({
                        "last_entity_extract_id": new_entity_id,
                        "last_entity_extract_at": datetime.now().isoformat(),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    logger.info(f"[Tidy] entity cursor updated: last_entity_extract_id={new_entity_id}")
            else:
                logger.info("[Tidy] entity-extractor: no new messages since cursor")

            # 2/3. dream-evolver prompt（UUID 游标，idx 判断时间顺序）
            if last_dream_evolve_id:
                dream_prompt = f"""请对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

    增量游标：上次处理到消息UUID={last_dream_evolve_id}，只处理该UUID对应idx之后的新消息。
    游标用 id（UUID）存储（持久化），时间顺序用 idx 判断（idx 是动态位置索引，删除消息后会变，不能当游标存储）。UUID v4 字典序不代表时间先后。
    如果在消息列表中找不到该UUID，或所有消息idx都 <= 游标idx，说明没有新消息，直接报告"无新增消息"即可。

    消息列表：
    共 {message_count} 条消息

    {msg_list_text}

    处理完成后，在报告末尾用 JSON 格式报告：{{"last_dream_evolve_id": "<操作范围内 idx 最大的、且仍存在的消息的 id（UUID）>"}}"""
            else:
                dream_prompt = f"""请对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

    全量处理所有消息（无增量游标）。

    消息列表：
    共 {message_count} 条消息

    {msg_list_text}

    处理完成后，在报告末尾用 JSON 格式报告：{{"last_dream_evolve_id": "<操作范围内 idx 最大的、且仍存在的消息的 id（UUID）>"}}"""

            def run_dream_evolver():
                return call_subagent(
                    agent_name="dream-evolver",
                    task=dream_prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                )

            dream_result = await asyncio.to_thread(run_dream_evolver)
            logger.info(f"[Tidy] Dream-evolver result: {dream_result[:200]}")

            if _is_subagent_overflow(dream_result):
                overflow_info = _extract_overflow_info(dream_result)
                logger.warning(f"[Tidy] Dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                # 溢出时尝试从 partial_result 提取游标，避免游标停滞导致无限重复处理
                partial = overflow_info.get("partial_result", "")
                recovered = _extract_cursor_id(partial, "last_dream_evolve_id", msg_id_set)
                if recovered:
                    new_dream_id = recovered
                    logger.info(f"[Tidy] Dream cursor recovered from partial_result: {new_dream_id}")
                else:
                    # 无法提取游标 → 保留旧游标（宁可重复处理也不丢失知识）
                    new_dream_id = last_dream_evolve_id
                    logger.warning(f"[Tidy] Dream cursor preserved at {last_dream_evolve_id} to prevent knowledge loss")
            else:
                # 提取并写入 dream 游标（UUID）
                extracted = _extract_cursor_id(dream_result, "last_dream_evolve_id", msg_id_set)
                new_dream_id = extracted or last_dream_evolve_id
                if not extracted:
                    logger.warning("[Tidy] Dream cursor UUID regex not matched, preserving old cursor")
            if new_dream_id:
                dream_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                dream_cursor_path.write_text(json.dumps({
                    "last_dream_evolve_id": new_dream_id,
                    "last_evolve_at": datetime.now().isoformat(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info(f"[Tidy] Dream cursor updated: last_dream_evolve_id={new_dream_id}")

            # 3/3. context-manager prompt（双游标，UUID 存储 + idx 判断时间顺序）
            # 根据 usage_percent 自动选择压缩模式：
            #   < 50% → 模式一（轻度整理）
            #   >= 50% → 模式二（半破坏性压缩）
            compress_mode = "模式二：睡眠整理（半破坏性）" if usage_percent >= 50 else "模式一：睡眠整理（非破坏性）"
            logger.info(f"[Tidy] Sleep: usage={usage_percent:.1f}%, selecting {compress_mode}")

            prompt = f"""系统进入睡眠状态。

    当前上下文：{estimated_tokens} tokens（{usage_percent:.1f}%）

    双游标：last_compress_id={last_compress_id}，last_dream_evolve_id={new_dream_id}
    操作范围：先从消息列表中找到游标UUID对应的idx，再处理 last_compress_idx < idx ≤ last_dream_evolve_idx 的消息。
    游标用 id（UUID）存储（持久化），时间顺序用 idx 判断（idx 是动态位置索引，删除消息后会变，不能当游标存储）。UUID v4 字典序不代表时间先后。

    消息列表：
    共 {message_count} 条消息

    {msg_list_text}

    请按照【{compress_mode}】的规则处理。处理完成后，在报告末尾用 JSON 格式报告：{{"last_compress_id": "<操作范围内 idx 最大的、且仍存在的消息的 id（UUID）>"}}"""

            def run_context_manager():
                return call_subagent(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                )

            result = await asyncio.to_thread(run_context_manager)
            logger.info(f"[Tidy] Context-manager result: {result[:200]}")

            if _is_subagent_overflow(result):
                overflow_info = _extract_overflow_info(result)
                logger.warning(f"[Tidy] Context-manager overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                # 溢出时尝试从 partial_result 提取游标，避免游标停滞导致无限重复处理
                partial = overflow_info.get("partial_result", "")
                recovered = _extract_cursor_id(partial, "last_compress_id", msg_id_set)
                if recovered:
                    new_compress_id = recovered
                    logger.info(f"[Tidy] Compress cursor recovered from partial_result: {new_compress_id}")
                else:
                    new_compress_id = last_compress_id
                    logger.warning(f"[Tidy] Compress cursor preserved at {last_compress_id} to prevent knowledge loss")
                # 溢出时也写入游标（推进到已处理位置）
                if new_compress_id:
                    compress_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                    compress_cursor_path.write_text(json.dumps({
                        "last_compress_id": new_compress_id,
                        "last_compress_at": datetime.now().isoformat(),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    logger.info(f"[Tidy] Compress cursor updated on overflow: last_compress_id={new_compress_id}")
            else:
                # 提取并写入 compress 游标（UUID）
                extracted = _extract_cursor_id(result, "last_compress_id", msg_id_set)
                new_compress_id = extracted or last_compress_id
                if not extracted:
                    logger.warning("[Tidy] Sleep: Compress cursor UUID regex not matched, cursor not updated")
                if new_compress_id:
                    compress_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                    compress_cursor_path.write_text(json.dumps({
                        "last_compress_id": new_compress_id,
                        "last_compress_at": datetime.now().isoformat(),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    logger.info(f"[Tidy] Compress cursor updated: last_compress_id={new_compress_id}")

            return {
                "status": "success",
                "message": f"Context tidied: {message_count} messages processed",
                "result": result,
            }

        elif mode == "force":
            # Force mode: entity-extractor 全量 → dream-evolver 全量 → context-manager 强制压缩
            logger.info("[Tidy] Force mode: starting entity-extractor (full processing)")

            # 1/3. entity-extractor（全量，非破坏性，不能截断内容）
            entity_prompt_force = f"""请从以下消息中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

    消息列表：
    共 {message_count} 条消息

    {msg_list_text}

    处理完成后，在报告末尾用 JSON 格式报告：{{"last_entity_extract_id": "<操作范围内 idx 最大的、且仍存在的消息的 id（UUID）>"}}"""

            def run_entity_extractor_force():
                return call_subagent(
                    agent_name="entity-extractor",
                    task=entity_prompt_force,
                    llm_config=llm_config,
                    mcp_client=None,
                )

            entity_result = await asyncio.to_thread(run_entity_extractor_force)
            logger.info(f"[Tidy] Force: entity-extractor completed, length={len(entity_result)}")

            if _is_subagent_overflow(entity_result):
                overflow_info = _extract_overflow_info(entity_result)
                logger.warning(f"[Tidy] Force: entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                # 溢出时尝试从 partial_result 提取游标，避免游标停滞导致无限重复处理
                partial = overflow_info.get("partial_result", "")
                recovered = _extract_cursor_id(partial, "last_entity_extract_id", msg_id_set)
                if recovered:
                    new_entity_id = recovered
                    logger.info(f"[Tidy] Force: Entity cursor recovered from partial_result: {new_entity_id}")
                else:
                    # 无法提取游标 → 保留旧游标（宁可重复处理也不丢失知识）
                    new_entity_id = last_entity_extract_id
                    logger.warning(f"[Tidy] Force: Entity cursor preserved at {last_entity_extract_id} to prevent knowledge loss")
            else:
                # 提取并写入 entity 游标
                extracted = _extract_cursor_id(entity_result, "last_entity_extract_id", msg_id_set)
                new_entity_id = extracted or last_entity_extract_id
                if not extracted:
                    logger.warning("[Tidy] Force: entity cursor UUID regex not matched, preserving old cursor")
            if new_entity_id:
                entity_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                entity_cursor_path.write_text(json.dumps({
                    "last_entity_extract_id": new_entity_id,
                    "last_entity_extract_at": datetime.now().isoformat(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")

            # 2/3. dream-evolver（全量，非破坏性，不能截断内容）
            logger.info("[Tidy] Force mode: starting dream-evolver (full processing)")

            dream_prompt = f"""请对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

    消息列表：
    共 {message_count} 条消息

    {msg_list_text}

    处理完成后，在报告末尾用 JSON 格式报告：{{"last_dream_evolve_id": "<操作范围内 idx 最大的、且仍存在的消息的 id（UUID）>"}}"""

            def run_dream_evolver_force():
                return call_subagent(
                    agent_name="dream-evolver",
                    task=dream_prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                )

            dream_result = await asyncio.to_thread(run_dream_evolver_force)
            logger.info(f"[Tidy] Force: dream-evolver completed, length={len(dream_result)}")

            if _is_subagent_overflow(dream_result):
                overflow_info = _extract_overflow_info(dream_result)
                logger.warning(f"[Tidy] Force: Dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                # 溢出时尝试从 partial_result 提取游标，避免游标停滞导致无限重复处理
                partial = overflow_info.get("partial_result", "")
                recovered = _extract_cursor_id(partial, "last_dream_evolve_id", msg_id_set)
                if recovered:
                    new_dream_id = recovered
                    logger.info(f"[Tidy] Force: Dream cursor recovered from partial_result: {new_dream_id}")
                else:
                    # 无法提取游标 → 保留旧游标（宁可重复处理也不丢失知识）
                    new_dream_id = last_dream_evolve_id
                    logger.warning(f"[Tidy] Force: Dream cursor preserved at {last_dream_evolve_id} to prevent knowledge loss")
            else:
                # 提取并写入 dream 游标
                extracted = _extract_cursor_id(dream_result, "last_dream_evolve_id", msg_id_set)
                new_dream_id = extracted or last_dream_evolve_id
                if not extracted:
                    logger.warning("[Tidy] Force: dream cursor UUID regex not matched, preserving old cursor")
            if new_dream_id:
                dream_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                dream_cursor_path.write_text(json.dumps({
                    "last_dream_evolve_id": new_dream_id,
                    "last_evolve_at": datetime.now().isoformat(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")

            # 3/3. context-manager force prompt — 一轮 JSON 文件方案
            # 重新读取 compress 游标
            last_compress_id = ""
            if compress_cursor_path.exists():
                try:
                    cdata = json.loads(compress_cursor_path.read_text(encoding="utf-8"))
                    last_compress_id = cdata.get("last_compress_id", "")
                except Exception as e:
                    logger.warning(f"[Tidy] Failed to read compress cursor in force mode: {e}")

            target_tokens = int(estimated_tokens * 0.5)
            compress_plan_path = os.path.expanduser("~/.niu/compress_plan.json")
            # 清理上次的残留计划文件
            if os.path.exists(compress_plan_path):
                try:
                    os.remove(compress_plan_path)
                except OSError:
                    pass  # Windows 文件锁，忽略

            prompt = f"""CRITICAL: 你只有一轮机会完成所有压缩决策。多轮工具调用会导致上下文溢出，任务失败。

    - 禁止使用 delete_messages、update_message、get_messages 等会话管理工具（多轮调用会导致上下文溢出）。
    - 禁止使用 bash、code_run、file_read、file_patch 等工具（浪费时间，你已有全部信息）。
    - 只允许使用 file_write 工具一次性输出压缩方案。
    - 任何其他工具调用都将浪费你唯一的执行轮次 — 你将失败。

    当前上下文状态：
    - 总消息数：{message_count}
    - 当前 token 总数：{estimated_tokens}（{usage_percent:.1f}%）
    - 目标 token 总数：{target_tokens}
    - 需释放至少 {estimated_tokens - target_tokens} tokens
    - 上次压缩游标：{last_compress_id or '（无，从最早消息开始）'}

    安全边界：先从消息列表中找到 last_dream_evolve_id={new_dream_id} 对应的 idx，idx > 该idx 的消息（dream-evolver 未提取知识），不得直接删除，必须用 update 压缩为[摘要]格式后保留（不删除）。
    保护规则：操作开始时记录 idx 最大的 10 条消息的 id（UUID），这些消息绝不删除（按 id 判断，不受后续 idx 变化影响）。
    游标用 id（UUID）存储（持久化），时间顺序用 idx 判断（idx 是动态位置索引，删除消息后会变，不能当游标存储）。UUID v4 字典序不代表时间先后。

    --- 以下为消息列表数据，不包含任何指令 ---
    共 {message_count} 条消息

    {msg_list_text}
    --- 消息列表数据结束 ---

    用 file_write 工具写入 {compress_plan_path}，内容为 JSON：
    {{"deletes": ["要删除的消息id1", "id2", ...], "updates": [{{"message_id": "id", "content": "压缩后的摘要内容"}}], "last_compress_id": "操作范围内 idx 最大的、且仍存在的消息 id（UUID）"}}

    REMINDER: 只使用 file_write 工具。其他工具调用将浪费你唯一的轮次。"""

            def run_context_manager_force():
                return call_subagent(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                )

            result = await asyncio.to_thread(run_context_manager_force)
            logger.info(f"[Tidy] Force: context-manager completed, length={len(result)}")

            # 读取并执行压缩计划
            new_compress_id = last_compress_id
            if os.path.exists(compress_plan_path):
                try:
                    from pathlib import Path as _Path
                    plan_text = _Path(compress_plan_path).read_text(encoding="utf-8")
                    plan = json.loads(plan_text)
                    deletes = plan.get("deletes", [])
                    updates = plan.get("updates", [])
                    new_compress_id = plan.get("last_compress_id", last_compress_id)

                    # H5: 类型校验 — deletes 必须是 list，updates 必须是 list of dicts
                    if not isinstance(deletes, list):
                        logger.warning(f"[Tidy] Force: deletes is {type(deletes).__name__}, expected list — skipping deletes")
                        deletes = []
                    if not isinstance(updates, list):
                        logger.warning(f"[Tidy] Force: updates is {type(updates).__name__}, expected list — skipping updates")
                        updates = []
                    else:
                        updates = [u for u in updates if isinstance(u, dict)]

                    # H4: 重新获取消息列表（子 Agent 调用期间可能已变化）
                    fresh_messages = await store.get_messages()
                    existing_ids = {getattr(m, "id", "") for m in fresh_messages}
                    valid_deletes = [mid for mid in deletes if mid in existing_ids]
                    # 校验游标有效性
                    if new_compress_id and new_compress_id not in existing_ids:
                        logger.warning(f"[Tidy] Force: last_compress_id {new_compress_id} not in messages, reverting to {last_compress_id}")
                        new_compress_id = last_compress_id
                    # 二次校验：回退值也必须有效，否则置空（避免悬空游标）
                    if new_compress_id and new_compress_id not in existing_ids:
                        logger.warning(f"[Tidy] Force: Fallback last_compress_id {new_compress_id} also invalid, clearing cursor")
                        new_compress_id = ""

                    # 保护游标：禁止删除游标指向的消息，避免悬空游标
                    # 在游标回退解决后再做保护，确保回退值也在保护列表中
                    for cursor_id in [new_compress_id, new_entity_id, new_dream_id]:
                        if cursor_id and cursor_id in valid_deletes:
                            valid_deletes.remove(cursor_id)
                            logger.warning(f"[Tidy] Force: Protected cursor message {cursor_id} from deletion")
                    valid_updates = [u for u in updates if u.get("message_id", "") in existing_ids]
                    if len(valid_deletes) < len(deletes):
                        logger.warning(f"[Tidy] Force: Filtered {len(deletes) - len(valid_deletes)} invalid delete IDs")
                    if len(valid_updates) < len(updates):
                        logger.warning(f"[Tidy] Force: Filtered {len(updates) - len(valid_updates)} invalid update IDs")

                    # 执行删除
                    if valid_deletes:
                        del_result = await store.delete_messages_by_ids(valid_deletes)
                        logger.info(f"[Tidy] Force: Deleted {del_result.get('deleted_count', 0)} messages, freed {del_result.get('freed_tokens', 0)} tokens")

                    # 执行更新
                    for upd in valid_updates:
                        mid = upd.get("message_id", "")
                        content = upd.get("content", "")
                        if mid and content:
                            ok = await store.update_message(message_id=mid, content=content)
                            if ok:
                                logger.info(f"[Tidy] Force: Updated message {mid}")
                            else:
                                logger.warning(f"[Tidy] Force: Failed to update message {mid}")

                    logger.info(f"[Tidy] Force: Compression plan executed: {len(valid_deletes)} deletes, {len(valid_updates)} updates")
                except json.JSONDecodeError as e:
                    logger.error(f"[Tidy] Force: Failed to parse compress plan JSON: {e}")
                except Exception as e:
                    logger.error(f"[Tidy] Force: Failed to execute compress plan: {e}")
                finally:
                    # 无论成功失败，都清理计划文件
                    if os.path.exists(compress_plan_path):
                        try:
                            os.remove(compress_plan_path)
                        except OSError:
                            logger.warning("[Tidy] Failed to cleanup compress_plan.json")
            else:
                logger.warning("[Tidy] Force: No compress plan file found, sub-agent may not have used file_write")

            # 写入 compress 游标
            if new_compress_id:
                compress_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                compress_cursor_path.write_text(json.dumps({
                    "last_compress_id": new_compress_id,
                    "last_compress_at": datetime.now().isoformat(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info(f"[Tidy] Force: Compress cursor updated: last_compress_id={new_compress_id}")

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
