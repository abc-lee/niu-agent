"""
ChatQueue — 消息队列 + 串行处理 + 上下文合并

替代 _chat_lock，所有消息来源（Electron、IM、Scheduler）统一入队，
ChatWorker 串行处理，补充消息在下一轮合并到上下文中。
"""
import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from agent.runner import NiuRunner
from agent.session import get_message_store


@dataclass
class ChatRequest:
    """入队消息"""
    content: str
    source: str = "frontend"  # "frontend" | "im" | "scheduler"
    channel: str = "electron"  # 消息来源通道: "electron" | "im" | "scheduler" 等
    channel_id: str = ""
    sender_id: str = ""
    session_id: str = "default"
    reply_future: Optional[asyncio.Future] = field(default=None, init=True, repr=False)


@dataclass
class EnqueueResult:
    """入队结果"""
    queued: bool = True
    request_id: str = ""
    message: str = "已入队"


class ChatQueue:
    """
    消息队列 — 替代 _chat_lock

    所有消息来源统一入队，ChatWorker 串行处理。
    处理期间到达的补充消息在下一轮合并到上下文中。
    """

    def __init__(self, runner: NiuRunner):
        self._queue: asyncio.Queue[ChatRequest] = asyncio.Queue()
        self._runner = runner
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False
        self._paused = False
        self._processing = False
        self._processing_done = asyncio.Event()
        self._processing_done.set()  # 初始状态：未在处理
        self._request_counter = itertools.count(1)
        self._bg_tasks: set = set()  # 后台任务引用集合，防止 GC 回收

    @property
    def is_processing(self) -> bool:
        """当前是否正在处理消息"""
        return self._processing

    async def start(self):
        """启动 ChatWorker 后台协程"""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("[ChatQueue] Worker started")

    async def stop(self):
        """停止 ChatWorker"""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("[ChatQueue] Worker stopped")

    async def enqueue(self, content: str, source: str = "frontend",
                      channel: str = "electron", channel_id: str = "",
                      sender_id: str = "", session_id: str = "default") -> EnqueueResult:
        """消息入队 — 立即返回"""
        request_id = str(next(self._request_counter))
        req = ChatRequest(
            content=content,
            source=source,
            channel=channel,
            channel_id=channel_id,
            sender_id=sender_id,
            session_id=session_id,
        )
        await self._queue.put(req)
        logger.info(f"[ChatQueue] Enqueued: source={source}, channel={channel}, content={content[:50]}...")
        return EnqueueResult(queued=True, request_id=request_id)

    def enqueue_sync(self, content: str, source: str = "frontend",
                     channel: str = "im", channel_id: str = "",
                     sender_id: str = "", session_id: str = "default") -> EnqueueResult:
        """同步入队 — 供外部线程调用（通过 call_soon_threadsafe）"""
        from niu_api.chat import _main_loop
        loop = _main_loop
        if loop is None or loop.is_closed():
            # Fallback: try to get a running event loop
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.error("[ChatQueue] No event loop available, cannot enqueue")
                return EnqueueResult(queued=False, message="No event loop available")

        request_id = str(next(self._request_counter))
        req = ChatRequest(
            content=content,
            source=source,
            channel=channel,
            channel_id=channel_id,
            sender_id=sender_id,
            session_id=session_id,
        )
        loop.call_soon_threadsafe(self._queue.put_nowait, req)
        logger.info(f"[ChatQueue] Enqueued (sync): source={source}, channel={channel}, content={content[:50]}...")
        return EnqueueResult(queued=True, request_id=request_id)

    async def enqueue_and_wait(self, content: str, source: str = "scheduler",
                               channel: str = "scheduler",
                               session_id: str = "default",
                               timeout: float = 120.0) -> str:
        """入队并等待回复 — 供 Scheduler 等需要同步结果的场景"""
        # --- /stop directive: immediate stop, no queueing ---
        if content.strip() == "/stop":
            from agent.runner import request_stop
            request_stop()
            logger.info("[ChatQueue] /stop requested (immediate)")
            return "已停止"

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        req = ChatRequest(
            content=content,
            source=source,
            channel=channel,
            session_id=session_id,
            reply_future=future,
        )
        await self._queue.put(req)
        logger.info(f"[ChatQueue] Enqueued (wait): source={source}, channel={channel}, content={content[:50]}...")

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            if not future.done():
                future.cancel()
            logger.warning(f"[ChatQueue] Wait timeout for: {content[:50]}...")
            return ""

    async def drain(self, timeout: float = 30.0) -> bool:
        """等待当前处理完成并清空队列"""
        # 清空队列中的待处理消息（非原子，但 drain 仅在 clear 时调用，低并发场景可接受）
        while not self._queue.empty():
            try:
                req = self._queue.get_nowait()
                if req.reply_future and not req.reply_future.done():
                    req.reply_future.set_result("[会话已清空]")
            except asyncio.QueueEmpty:
                break

        # 等待当前处理完成
        if self._processing:
            try:
                await asyncio.wait_for(self._processing_done.wait(), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                logger.warning("[ChatQueue] Drain timeout")
                return False
        return True

    def pause(self):
        """暂停 worker 处理（用于 clear_chat 防止 drain→clear 间隙中新消息被处理）"""
        self._paused = True

    def resume(self):
        """恢复 worker 处理"""
        self._paused = False

    async def _worker_loop(self):
        """ChatWorker 主循环 — 串行处理队列中的消息"""
        while self._running:
            try:
                req = await self._queue.get()
                if self._paused:
                    # 暂停期间跳过处理，消息留在队列中
                    await self._queue.put(req)
                    await asyncio.sleep(0.1)
                    continue
                await self._process_with_merge(req)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ChatQueue] Worker error: {e}")
                self._processing = False
                self._processing_done.set()

    async def _process_with_merge(self, first_req: ChatRequest):
        """处理消息，合并队列中的补充消息"""
        self._processing = True
        self._processing_done.clear()

        # 初始化变量，确保 finally 块中始终可用
        reply_future = first_req.reply_future
        supplements = []
        reply = ""

        try:
            remaining = []
            while not self._queue.empty():
                try:
                    extra = self._queue.get_nowait()
                    if extra.session_id == first_req.session_id:
                        supplements.append(extra)
                    else:
                        remaining.append(extra)
                except asyncio.QueueEmpty:
                    break
            # Put back messages from other sessions
            for r in remaining:
                self._queue.put_nowait(r)

            # 合并补充消息（仅用于传给 runner.chat() 的参数）
            all_contents = [first_req.content] + [s.content for s in supplements]
            if supplements:
                supplement_parts = []
                for i, s in enumerate(supplements, 1):
                    supplement_parts.append(f"[补充{i}] {s.content}")
                merged_content = f"{first_req.content}\n\n" + "\n".join(supplement_parts)
                logger.info(
                    f"[ChatQueue] Merged {len(supplements)} supplement(s): "
                    f"{merged_content[:80]}..."
                )
            else:
                merged_content = first_req.content

            # 处理合并后的消息
            try:
                reply = await self._process_single(merged_content, first_req.session_id, all_contents, channel=first_req.channel, channel_id=first_req.channel_id)
            except Exception as e:
                logger.error(f"[ChatQueue] Processing error: {e}")
                reply = f"[处理出错: {e}]"

            # 通道无关的回复路由
            if first_req.channel != "electron" and first_req.channel_id:
                try:
                    from niu_api.channel import get_channel_router
                    router = get_channel_router()
                    await router.route_out(reply, first_req.channel, first_req.channel_id)
                except Exception as e:
                    logger.error(f"[ChatQueue] Failed to route reply to {first_req.channel}: {e}")
            elif first_req.channel != "electron" and not first_req.channel_id:
                # 无目标通道ID（如scheduler主动推送），用push广播
                try:
                    from niu_api.channel import get_channel_router
                    router = get_channel_router()
                    await router.push(reply, first_req.channel, "")
                except Exception as e:
                    logger.error(f"[ChatQueue] Failed to push reply to {first_req.channel}: {e}")

        finally:
            # Always resolve futures, regardless of push success
            if reply_future and not reply_future.done():
                reply_future.set_result(reply)
            for s in supplements:
                if s.reply_future and not s.reply_future.done():
                    s.reply_future.set_result(reply)
            self._processing = False
            self._processing_done.set()

    async def _process_single(self, content: str, session_id: str = "default",
                              user_contents: list[str] | None = None, channel: str = "electron",
                              channel_id: str = "") -> str:
        """处理单条消息 — 加载历史，持久化 user 消息，调用 runner.chat()，持久化回复，SSE推送"""
        from agent.runner import is_stop_requested, clear_stop

        # 如果停止标志仍被设置，说明前一个 Agent 还未退出或标志残留
        # 残留标志：清除并继续；用户新设置的：不应该处理新消息
        if is_stop_requested():
            # 标志可能残留（Agent 退出后未被清除）
            # 清除残留标志，继续处理
            logger.info("[ChatQueue] Clearing residual stop flag before processing")
            clear_stop()

        try:
            from niu_api.compat import _chat_lock

            store = await get_message_store()

            # 先加载历史上下文（此时不包含当前 user 消息，避免重复）
            from agent.context_manager import get_context_manager
            context_manager = await get_context_manager(store)
            history_for_runner = await context_manager.get_context_for_chat(exclude_last=False)
            history_len = len(history_for_runner)

            # 持久化 user 消息（每条独立持久化，在历史加载之后）
            if user_contents:
                for uc in user_contents:
                    await store.add_message(role="user", content=uc)
            else:
                await store.add_message(role="user", content=content)

            # 调用 runner.chat()（在 executor 中运行，不阻塞事件循环）
            # NiuRunner.chat(session_id, user_input, stream=False, history=...)
            def sync_chat():
                chunks = []
                for chunk in self._runner.chat(session_id, content, stream=False, history=history_for_runner, channel_id=channel_id):
                    chunks.append(chunk)
                return "".join(chunks)

            acquired = False
            chat_error = None
            try:
                acquired = await asyncio.wait_for(_chat_lock.acquire(), timeout=600.0)
                if not acquired:
                    raise TimeoutError("Timeout waiting for chat lock")
                full_reply = await asyncio.get_running_loop().run_in_executor(None, sync_chat)
            except asyncio.TimeoutError:
                logger.error("[ChatQueue] Timeout waiting for chat lock")
                chat_error = "timeout"
                full_reply = "处理消息超时，请稍后重试"
            except Exception as e:
                logger.error(f"[ChatQueue] Chat error: {e}")
                chat_error = str(e)
                full_reply = f"处理消息时出错：{str(e)}"
            finally:
                if acquired:
                    _chat_lock.release()

            # 方案 A：异常时不进 DB（避免错误文本被下一轮 _inject_dynamic_resources 当 query 反复查 lightrag）
            rv = getattr(self._runner, "last_return_value", None)
            if chat_error is None:
                # 持久化回复消息（使用共享函数）
                from niu_api.chat import persist_agent_reply
                persisted_msgs = getattr(self._runner, "_persisted_msgs", None)  # V4: 已逐条持久化的消息
                message_id, full_reply = await persist_agent_reply(store, rv, history_len, full_reply, source=channel, persisted_msgs=persisted_msgs)
            else:
                message_id = None
                logger.warning(f"[ChatQueue] Skipped persist due to chat error: {chat_error}")

            # 上下文溢出检测
            await self._check_overflow(session_id, store, full_reply)

            return full_reply
        finally:
            # 防御性清除：确保停止标志不残留（与 chat_session 的 finally 对齐）
            if is_stop_requested():
                clear_stop()

    async def _check_overflow(self, session_id: str, store, full_reply: str):
        """检测上下文溢出，触发压缩

        不在 ChatQueue worker 内部直接调用 _tidy_context_impl(force)，
        因为 force 模式会 pause ChatQueue + 等待 _processing_done，
        而当前协程就是 ChatQueue worker，会导致死锁。
        改为调度延迟任务，让 worker 先完成当前消息处理。
        """
        rv = getattr(self._runner, "last_return_value", None)
        if rv and isinstance(rv, dict) and rv.get("result") == "CONTEXT_OVERFLOW":
            overflow_data = rv.get("data", {})
            logger.warning(
                f"[ChatQueue] CONTEXT_OVERFLOW at {overflow_data.get('tokens_used', 0)} tokens, "
                f"scheduling delayed force compression"
            )
            _task = asyncio.create_task(self._retry_force_compression(session_id, delay=1.0))
            self._bg_tasks.add(_task)
            _task.add_done_callback(lambda t: self._bg_tasks.discard(t))

    async def _retry_force_compression(self, session_id: str, delay: float = 5.0, max_retries: int = 3):
        """重试 force 压缩，逐步放宽保护"""
        from niu_api.compat import _tidy_context_impl, _tidy_lock
        # 降级策略：每次重试减少保护消息数量
        # 第 1 次：默认 protect_recent_count（10）
        # 第 2 次：protect_recent_count = 5
        # 第 3 次：protect_recent_count = 2
        degrade_schedule = [None, 5, 2]  # None = 使用默认值

        for attempt in range(max_retries):
            await asyncio.sleep(delay)

            try:
                await asyncio.wait_for(_tidy_lock.acquire(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning(f"[ChatQueue] Force compression retry {attempt+1}/{max_retries}: tidy lock still busy")
                continue

            try:
                request = {"session_id": session_id, "mode": "force"}
                if attempt < len(degrade_schedule) and degrade_schedule[attempt] is not None:
                    request["force_protect_recent"] = degrade_schedule[attempt]
                    logger.info(f"[ChatQueue] Force compression retry {attempt+1} with degraded protect_recent={degrade_schedule[attempt]}")

                result = await _tidy_context_impl(request=request)

                # 检查压缩后 token 是否降到安全水平
                tokens_after = result.get("tokens_after", 0) if isinstance(result, dict) else 0
                if tokens_after > 0:
                    from agent.subagent import _read_context_window_tokens, _read_warning_threshold
                    _cw = _read_context_window_tokens()
                    _wt = _read_warning_threshold()
                    _safe_level = int(_cw * _wt)
                    if tokens_after <= _safe_level:
                        logger.info(f"[ChatQueue] Force compression retry {attempt+1} succeeded: tokens_after={tokens_after} <= warning_threshold={_safe_level}")
                        return
                    else:
                        logger.warning(f"[ChatQueue] Force compression retry {attempt+1}: tokens_after={tokens_after} still above warning_threshold={_safe_level}")
                        # 继续降级重试，不 return
                else:
                    logger.warning(f"[ChatQueue] Force compression retry {attempt+1}: no tokens_after in result, continuing degradation")
                    # 继续降级重试，不 return
            except Exception as e:
                logger.error(f"[ChatQueue] Force compression retry {attempt+1} failed: {e}")
                # 继续降级重试，不 return——降级策略需要多轮才能生效
            finally:
                _tidy_lock.release()

        logger.error(f"[ChatQueue] All {max_retries} force compression retries exhausted")


# ============== 全局单例 ==============

_queue: ChatQueue | None = None
_queue_stopped: bool = False


def get_chat_queue() -> ChatQueue:
    """获取全局 ChatQueue 实例"""
    global _queue, _queue_stopped
    if _queue_stopped:
        raise RuntimeError("ChatQueue has been stopped")
    if _queue is None:
        from niu_api.chat import get_or_create_runner
        runner = get_or_create_runner()
        _queue = ChatQueue(runner)
    return _queue


async def start_chat_queue():
    """启动 ChatQueue（在 FastAPI startup 中调用）"""
    q = get_chat_queue()
    await q.start()


async def stop_chat_queue():
    """停止 ChatQueue（在 FastAPI shutdown 中调用）"""
    global _queue, _queue_stopped
    _queue_stopped = True
    if _queue:
        # Cancel background tasks (e.g., _retry_force_compression)
        for task in list(_queue._bg_tasks):
            task.cancel()
        if _queue._bg_tasks:
            await asyncio.gather(*_queue._bg_tasks, return_exceptions=True)
        await _queue.stop()
        _queue = None
