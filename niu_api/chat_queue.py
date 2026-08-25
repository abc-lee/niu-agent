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


def _classify_degraded_reason(chat_error) -> str:
    """E4-12：降级回复错误类别——"timeout" | "internal"（DB 可追溯标记）。

    仅运维可追溯用——用户侧保持中性占位符（E2 定案，不泄露内部错误细节）。
    分类规则：
    - 锁超时路径（chat_error 为字符串 "timeout"）→ "timeout"
    - 异常对象类型名含 timeout（litellm.Timeout / APITimeoutError / TimeoutError 等瞬态）→ "timeout"
    - 其余（含 LLM 非超时类如 RateLimitError）→ "internal"
    """
    if chat_error == "timeout":
        return "timeout"
    if isinstance(chat_error, BaseException):
        if "timeout" in type(chat_error).__name__.lower():
            return "timeout"
    return "internal"


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

    @property
    def is_processing(self) -> bool:
        """当前是否正在处理消息"""
        return self._processing

    def reload_runner(self):
        """配置热更新后刷新 runner 引用（worker 下次处理消息即用新 runner）。

        不清除队列、不重启 worker——_worker_loop 每次处理消息时读 self._runner，
        替换引用后待处理消息自动用新配置。正在处理中的消息持旧 runner 完成（
        与 Runner 单例语义一致：进行中的回合不受影响）。
        """
        from niu_api.chat import get_or_create_runner
        self._runner = get_or_create_runner()

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
                # 唤醒睡眠整理管道（Case 3 可打断，方案 §3.4）：仅用户来源动作唤醒。
                # 门控按 channel 判据：electron/im 才唤醒；scheduler 与 ha-watcher 的
                # 入队 channel 均非 electron/im（ha-watcher 走默认 "scheduler"），
                # 后台来源天然不唤醒——双保险排除。
                if req.channel in ("electron", "im"):
                    from niu_api.compat import set_spirit_state  # 函数内惰性 import
                    set_spirit_state("idle")
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
                        # 定时任务主 Agent 回复必达 IM（trigger 提醒程序消息不推 IM——只写 DB 由前端 SSE 刷新；仅主 Agent 的话经 should_push_im 闸门投递）
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
                chat_error = e  # E2：保留异常对象（str() 化后 type() 判定恒 'str'——is_litellm_error_type 失效）；str() 插值/None 判定/日志均兼容
                full_reply = f"处理消息时出错：{str(e)}"
            finally:
                if acquired:
                    _chat_lock.release()

            # 方案 A：异常时不进 DB（避免错误文本被下一轮 _inject_dynamic_resources 当 query 反复查 lightrag）
            rv = getattr(self._runner, "last_return_value", None)
            if chat_error is None:
                if rv and isinstance(rv, dict) and rv.get("result") == "LLM_ERROR":
                    # E2：LLM_ERROR 错误文本不落库（用户拍板"不写 DB"——刷新 Chat 从 DB 加载历史时自然消失）
                    # full_reply 已是源头友好文案（agent_loop yield 双参）——不重复 format（通道 2 双包风险）
                    # 该函数内 message_id 无读取点（return full_reply 不消费），无需赋值
                    error_msg = rv.get("error_msg", "") or ""
                    error_type = rv.get("error_type")
                    from niu_api.chat import notify_llm_error_sync
                    from agent.generic.litellm_adapter import extract_error_type, format_llm_error_for_user
                    notify_llm_error_sync(
                        error_type or extract_error_type(error_msg),
                        format_llm_error_for_user(error_msg, error_type),
                        "chat_queue",
                    )
                    # skip persist——错误文本不落库
                else:
                    # 持久化回复消息（使用共享函数）
                    # source 强制 "electron"——所有 source（包括 scheduler）的 assistant 回复
                    # 都走 electron SSE 通道推送给前端，避免被 notify_new_message 白名单过滤
                    from niu_api.chat import persist_agent_reply
                    persisted_msgs = getattr(self._runner, "_persisted_msgs", None)  # V4: 已逐条持久化的消息
                    extracted_at_msgs = getattr(self._runner, "_extracted_at_msgs", None)  # 修正版方案：轮中提取的 subagent_msg（去重用）
                    message_id, full_reply = await persist_agent_reply(store, rv, history_len, full_reply, source="electron", persisted_msgs=persisted_msgs, extracted_at_msgs=extracted_at_msgs)
            else:
                # 异常路径：中性占位符落库 + 友好文案投递 + notify 解耦（E2）
                # DB 存中性占位符（错误细节不进 DB，避免污染下轮向量检索）；投递文本为友好文案（用户可见）
                # type_name/is_llm 提前判定（与 persist 解耦）——persist 成败均 notify
                # persisted_msgs 强制 None——异常路径下 _persisted_msgs 可能是上次的列表（语义陷阱）
                # source 强制 "electron"——与正常路径一致，避免被 notify_new_message 白名单过滤
                degraded_reply = "[系统繁忙，请重试]"  # 中性占位符落库（既有行为保持——错误细节不进 DB）
                from niu_api.chat import notify_llm_error_sync
                from agent.generic.litellm_adapter import format_llm_error_for_user, is_litellm_error_type
                type_name = type(chat_error).__name__ if isinstance(chat_error, BaseException) else None
                is_llm = bool(type_name) and is_litellm_error_type(type_name)
                if is_llm:
                    push_text = format_llm_error_for_user(str(chat_error), type_name)  # 友好文案（独立于 persist 成败）
                # E4-12：降级是监控可见事件——非 LLM 异常（内部 bug/锁超时）error 级；
                # LLM 异常保持 warning（notify_llm_error_sync 已负责用户可见）。degraded_reason 落库（DB 可追溯）。
                degraded_reason = _classify_degraded_reason(chat_error)
                _degraded_log = logger.error if not is_llm else logger.warning
                _degraded_log(
                    f"[ChatQueue] Chat error, writing degraded reply. original_error={chat_error}, degraded_reason={degraded_reason}"
                )
                from niu_api.chat import persist_agent_reply
                try:
                    message_id, _ = await persist_agent_reply(
                        store, None, history_len, degraded_reply,
                        source="electron", persisted_msgs=None,
                        degraded_reason=degraded_reason,
                    )
                except Exception as persist_e:
                    logger.error(f"[ChatQueue] Degraded reply persist failed: {persist_e}")
                # 统一赋值：persist 失败 DB 降级但投递仍友好（try/except 后单行）
                full_reply = push_text if is_llm else degraded_reply
                if is_llm:
                    notify_llm_error_sync(type_name, push_text, "chat_queue")  # notify 与 persist 解耦——persist 成败均推（Electron 并行可见性一致）；非 LLM 异常不 notify（不误标）

            # Task 3 溢出投递面收编：_check_overflow/_retry_force_compression 降级重试链整删。
            # 终态语义：压实后仍超限=放行服务端报错走既有降级回复（上方 chat_error 分支已落库）。

            return full_reply
        finally:
            # 防御性清除：确保停止标志不残留（与 chat_session 的 finally 对齐）
            if is_stop_requested():
                clear_stop()


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
