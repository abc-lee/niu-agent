"""
ChatQueue — 消息队列 + 串行处理 + 上下文合并

替代 _chat_lock，所有消息来源（前端、飞书、Scheduler）统一入队，
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
    source: str = "frontend"  # "frontend" | "feishu" | "scheduler"
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
                      channel_id: str = "", sender_id: str = "",
                      session_id: str = "default") -> EnqueueResult:
        """消息入队 — 立即返回"""
        request_id = str(next(self._request_counter))
        req = ChatRequest(
            content=content,
            source=source,
            channel_id=channel_id,
            sender_id=sender_id,
            session_id=session_id,
        )
        await self._queue.put(req)
        logger.info(f"[ChatQueue] Enqueued: source={source}, content={content[:50]}...")
        return EnqueueResult(queued=True, request_id=request_id)

    def enqueue_sync(self, content: str, source: str = "frontend",
                     channel_id: str = "", sender_id: str = "",
                     session_id: str = "default") -> EnqueueResult:
        """同步入队 — 供飞书线程调用（通过 call_soon_threadsafe）"""
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
            channel_id=channel_id,
            sender_id=sender_id,
            session_id=session_id,
        )
        loop.call_soon_threadsafe(self._queue.put_nowait, req)
        logger.info(f"[ChatQueue] Enqueued (sync): source={source}, content={content[:50]}...")
        return EnqueueResult(queued=True, request_id=request_id)

    async def enqueue_and_wait(self, content: str, source: str = "scheduler",
                               session_id: str = "default",
                               timeout: float = 120.0) -> str:
        """入队并等待回复 — 供 Scheduler 等需要同步结果的场景"""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        req = ChatRequest(
            content=content,
            source=source,
            session_id=session_id,
            reply_future=future,
        )
        await self._queue.put(req)
        logger.info(f"[ChatQueue] Enqueued (wait): source={source}, content={content[:50]}...")

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
        source = first_req.source
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
                reply = await self._process_single(merged_content, first_req.session_id, all_contents)
            except Exception as e:
                logger.error(f"[ChatQueue] Processing error: {e}")
                reply = f"[处理出错: {e}]"

            # 推送回复到飞书（传空 channel_id，让 push() 按 open_id > chat_id 优先级选择）
            try:
                if source == "feishu":
                    await self._push_to_feishu(reply)
            except Exception as e:
                logger.error(f"[ChatQueue] Feishu push error: {e}")

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
                              user_contents: list[str] | None = None) -> str:
        """处理单条消息 — 加载历史，持久化 user 消息，调用 runner.chat()，持久化回复，SSE推送"""
        store = await get_message_store()

        # 先加载历史上下文（此时不包含当前 user 消息，避免重复）
        from agent.context_manager import get_context_manager
        context_manager = await get_context_manager(store)
        history_for_runner = await context_manager.get_context_for_chat()
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
            for chunk in self._runner.chat(session_id, content, stream=False, history=history_for_runner):
                chunks.append(chunk)
            return "".join(chunks)

        try:
            full_reply = await asyncio.get_running_loop().run_in_executor(None, sync_chat)
        except Exception as e:
            logger.error(f"[ChatQueue] Chat error: {e}")
            full_reply = f"处理消息时出错：{str(e)}"

        # 持久化回复消息（使用共享函数）
        from niu_api.chat import persist_agent_reply
        rv = getattr(self._runner, "last_return_value", None)
        message_id, full_reply = await persist_agent_reply(store, rv, history_len, full_reply)

        # 上下文溢出检测
        await self._check_overflow(session_id, store, full_reply)

        return full_reply

    async def _check_overflow(self, session_id: str, store, full_reply: str):
        """检测上下文溢出，触发压缩"""
        rv = getattr(self._runner, "last_return_value", None)
        if rv and isinstance(rv, dict) and rv.get("result") == "CONTEXT_OVERFLOW":
            overflow_data = rv.get("data", {})
            logger.warning(
                f"[ChatQueue] CONTEXT_OVERFLOW at {overflow_data.get('tokens_used', 0)} tokens"
            )
            from niu_api.compat import _tidy_context_impl, _tidy_lock
            async with _tidy_lock:
                await _tidy_context_impl(request={"session_id": session_id, "mode": "force"})
        elif full_reply.strip():
            from niu_api.compat import _check_and_trigger_auto_tidy
            await _check_and_trigger_auto_tidy(store)

    async def _push_to_feishu(self, reply: str):
        """推送回复到飞书 — 传空 channel_id，让 push() 按 open_id > chat_id 优先级选择"""
        try:
            from niu_api.channel import get_channel_router
            router = get_channel_router()
            if router.has_channel("feishu"):
                adapter = router.channels["feishu"]
                # 传空 channel_id，让 push() 内部按 open_id > chat_id 优先级选择
                await adapter.push("", reply)
        except Exception as e:
            logger.warning(f"[ChatQueue] Feishu push failed: {e}")


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
        await _queue.stop()
        _queue = None
