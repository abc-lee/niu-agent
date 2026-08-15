"""
ChatQueue — 消息队列 + 串行处理 + 上下文合并

替代 _chat_lock，所有消息来源（Electron、IM、Scheduler）统一入队，
ChatWorker 串行处理，补充消息在下一轮合并到上下文中。
"""
import asyncio
import itertools
from dataclasses import dataclass, field

from agent.runner import NiuRunner
from agent.session import get_message_store
from loguru import logger


@dataclass
class ChatRequest:
    """入队消息"""
    content: str
    source: str = "frontend"  # "frontend" | "im" | "scheduler"
    channel: str = "electron"  # 消息来源通道: "electron" | "im" | "scheduler" 等
    channel_id: str = ""
    sender_id: str = ""
    session_id: str = "default"
    reply_future: asyncio.Future | None = field(default=None, init=True, repr=False)


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
        self._worker_task: asyncio.Task | None = None
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
        result, _ = await self.enqueue_and_wait_with_future(
            content=content, source=source, channel=channel,
            session_id=session_id, timeout=timeout,
        )
        return result

    async def enqueue_and_wait_with_future(self, content: str, source: str = "scheduler",
                                           channel: str = "scheduler",
                                           session_id: str = "default",
                                           timeout: float = 120.0) -> tuple[str, asyncio.Future | None]:
        """入队并等待回复，返回 (result, reply_future) 元组——reply_future 供调用方读取
        确定性标志（如 _im_finalized：scheduler 回复已由 chat_queue 终结 IM 卡片，见
        _process_with_merge 回复路由 elif 分支；ha_watcher 据此决定是否自推防双投递）。
        全部返回点均为元组：/stop → ("已停止", None)；timeout → ("", future)；正常 → (reply, future)。"""
        # --- /stop directive: immediate stop, no queueing ---
        if content.strip() == "/stop":
            from agent.runner import request_stop
            request_stop()
            logger.info("[ChatQueue] /stop requested (immediate)")
            return ("已停止", None)

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
            return (await asyncio.wait_for(future, timeout=timeout), future)
        except TimeoutError:
            if not future.done():
                future.cancel()
            logger.warning(f"[ChatQueue] Wait timeout for: {content[:50]}...")
            return ("", future)

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
            except TimeoutError:
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
                reply = await self._process_single(merged_content, first_req.session_id, all_contents, channel=first_req.channel, channel_id=first_req.channel_id, source=first_req.source)
            except Exception as e:
                logger.error(f"[ChatQueue] Processing error: {e}")
                reply = f"[处理出错: {e}]"

            # 通道无关的回复路由（保留实际两分支结构——分支 1 是正常 IM 会话卡片终结唯一路径，不可并入 else；
            # 两分支均保留既有 try/except + logger.error 包装）
            from niu_api.channel import get_channel_router
            if first_req.channel != "electron" and first_req.channel_id:
                # 分支 1（原样保留）：有 channel_id → route_out → gateway.send → SEND → adapter _on_send 终结卡片
                try:
                    router = get_channel_router()
                    await router.route_out(reply, first_req.channel, first_req.channel_id)
                except Exception as e:
                    logger.error(f"[ChatQueue] Failed to route reply to {first_req.channel}: {e}")
            elif first_req.channel != "electron" and not first_req.channel_id:
                # 分支 2：无目标通道 ID（scheduler 主动推送等）——scheduler 特判（投递回复内容），其他通道原样 push
                try:
                    router = get_channel_router()
                    if first_req.channel == "scheduler":
                        # 单一判定入口（用户拍板：全局只有一个 IM 推送判定）——
                        # 定时任务主 Agent 回复必须走 IM（trigger 提醒 + 回复两条都应在 IM）
                        if self._runner.should_push_im():
                            from niu_api.channel.gateway import get_im_gateway
                            _gw = get_im_gateway()
                            if _gw and _gw.is_connected:
                                im_cid = self._runner.get_im_channel()
                                # 投递回复内容（替代 08-12"仅终结不投递"）——卡片生命周期由 adapter _on_send 保证：
                                # 有流式卡 → state 分支用 reply 终结（reply==accumulated 时直接终结不重复；
                                # strip 失配回退 accumulated best-effort）；无卡 → send_markdown 独立消息，
                                # receive_id 空时 adapter 回退 _push_chat_id 广播。
                                _gw.send_sync(im_cid, reply, pop_reply_to=False)
                                # 确定性标志（置位/读取同源 asyncio.Future；resolve 前写入无 TOCTOU；
                                # 遍历整个合并批次——supplement 未置位会让 watcher 自推 → 双投递）：
                                for r in (first_req, *supplements):
                                    if r.reply_future is not None:
                                        r.reply_future._im_finalized = True  # watcher 读 getattr(reply_future, "_im_finalized", False) 决定是否自推
                        # else：防御分支——scheduler 请求按规则 3 每轮重臂 force，双假生产不可达
                        # （仅 _chat_lock 超时等边缘路径）→ 回复只走 SSE
                    else:
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
                              channel_id: str = "", source: str = "user") -> str:
        """处理单条消息 — 加载历史，持久化 user 消息，调用 runner.chat()，持久化回复，SSE推送"""
        from agent.runner import clear_stop, is_stop_requested

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
            # 所有 source（包括 scheduler）写 DB 后都推 SSE，让前端实时显示 user 气泡
            # 之前 source="scheduler" 被白名单排除，导致 scheduler 触发的对话前端看不到 user 消息
            #
            # 注意：ChatQueue _process_single 处理的所有消息都没在别处推 SSE——
            # chat_session 路径不走 ChatQueue（直接调 _chat_lock + runner.chat + 自己推 SSE），
            # 所有走 ChatQueue 的路径都经 _process_single，这里推 SSE 不会双推。
            from niu_api.chat import notify_new_message
            if user_contents:
                for uc in user_contents:
                    user_msg_id = await store.add_message(role="user", content=uc)
                    await notify_new_message(user_msg_id, "user", uc, source="electron")
            else:
                user_msg_id = await store.add_message(role="user", content=content)
                await notify_new_message(user_msg_id, "user", content, source="electron")

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
                if channel == "electron":
                    self._runner.set_im_channel("")
                    self._runner.set_im_force(False)
                elif channel == "im":
                    self._runner.set_im_channel(channel_id)
                else:
                    # scheduler / ha-watcher 等后台触发：直接置 IM 强制标志（定时任务天生发 IM）
                    self._runner.set_im_force(True)
                # 标记当前请求来源（归一化）：scheduler/ha-watcher → "scheduler"（子 Agent 停止隔离）；
                # frontend/im → "user"（IM 对话也是用户对话，可被停止按钮停）
                _norm_source = "scheduler" if source in ("scheduler", "ha-watcher") else "user"
                prev_source = getattr(self._runner, "_request_source", "user")
                self._runner._request_source = _norm_source
                try:
                    full_reply = await asyncio.get_running_loop().run_in_executor(None, sync_chat)
                finally:
                    self._runner._request_source = prev_source
            except TimeoutError:
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
                # source 强制 "electron"——所有 source（包括 scheduler）的 assistant 回复
                # 都走 electron SSE 通道推送给前端，避免被 notify_new_message 白名单过滤
                from niu_api.chat import persist_agent_reply
                persisted_msgs = getattr(self._runner, "_persisted_msgs", None)  # V4: 已逐条持久化的消息
                extracted_at_msgs = getattr(self._runner, "_extracted_at_msgs", None)  # 修正版方案：轮中提取的 subagent_msg（去重用）
                message_id, full_reply = await persist_agent_reply(store, rv, history_len, full_reply, source="electron", persisted_msgs=persisted_msgs, extracted_at_msgs=extracted_at_msgs)
            else:
                # 异常路径：写降级回复 [系统繁忙，请重试] 到 DB + SSE 推送
                # 让前端看到 user 消息后立即跟一条 assistant 降级回复，不会卡 typing 状态
                # 保留原 full_reply（含具体错误信息）记日志，DB 存降级回复避免污染下轮向量检索
                # persisted_msgs 强制 None——异常路径下 _persisted_msgs 可能是上次的列表（语义陷阱）
                # source 强制 "electron"——与正常路径一致，避免被 notify_new_message 白名单过滤
                logger.warning(f"[ChatQueue] Chat error, writing degraded reply. original_error={chat_error}")
                degraded_reply = "[系统繁忙，请重试]"
                from niu_api.chat import persist_agent_reply
                try:
                    message_id, _ = await persist_agent_reply(
                        store, None, history_len, degraded_reply,
                        source="electron", persisted_msgs=None,
                    )
                    full_reply = degraded_reply
                except Exception as persist_e:
                    logger.error(f"[ChatQueue] Degraded reply persist failed: {persist_e}")
                    full_reply = degraded_reply

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
            except TimeoutError:
                logger.warning(f"[ChatQueue] Force compression retry {attempt+1}/{max_retries}: tidy lock still busy")
                continue

            try:
                request = {"session_id": session_id, "mode": "force"}
                if attempt < len(degrade_schedule) and degrade_schedule[attempt] is not None:
                    request["force_protect_recent"] = degrade_schedule[attempt]
                    logger.info(f"[ChatQueue] Force compression retry {attempt+1} with degraded protect_recent={degrade_schedule[attempt]}")

                # 广播压缩状态 started（前端圆环动画启动）
                try:
                    from niu_api.chat import notify_compact_status_sync
                    notify_compact_status_sync("started", mode="force")
                except Exception:
                    pass

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
                # 广播压缩状态 done（前端圆环动画结束），必须在 release 之前
                try:
                    from niu_api.chat import notify_compact_status_sync
                    notify_compact_status_sync("done", mode="force")
                except Exception:
                    pass
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
