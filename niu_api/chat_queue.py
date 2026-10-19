"""
ChatQueue — 消息队列 + 串行处理 + 上下文合并

替代 _chat_lock，所有消息来源（Electron、IM、Scheduler）统一入队，
ChatWorker 串行处理，补充消息在下一轮合并到上下文中。
P2b：ChatWorker 只提交 `RunCommand` 给 `MainAgentRuntime`（唯一主 Agent 执行体）——
历史、`_chat_lock`、`runner.chat`、channel/source 状态都在 runtime 线程内完成。
P3 D-1：回复持久化实现抽到 `niu_api.main_agent_persist.persist_run_outcome`，
ChatQueue 与三个前端端点共用同一份（分支次序不可重排）。
"""
import asyncio
import itertools
from dataclasses import dataclass, field

from agent.main_runtime import MainAgentRuntime, RunCommand, RunOutcome
from agent.runner import NiuRunner
from agent.session import get_message_store
from loguru import logger

from niu_api.main_agent_persist import persist_run_outcome


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

    def __init__(self, runner: NiuRunner, runtime: MainAgentRuntime):
        self._queue: asyncio.Queue[ChatRequest] = asyncio.Queue()
        self._runner = runner
        # D-12：runtime 必填——生产由 get_chat_queue() 注入 MainAgentRuntime.instance(runner)，
        # 测试注入 FakeRuntime（独立线程执行命令）
        self._runtime = runtime
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
        # D-7：runtime 与 runner 同步——待处理消息自动用新配置
        self._runtime.set_runner(self._runner)

    async def start(self):
        """启动 ChatWorker 后台协程（先把 runner 引用同步给 runtime）"""
        if self._running:
            return
        self._runtime.set_runner(self._runner)
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
            # §4.5：注入依赖优先——self._runtime.cancel_active()（latch active + pending，
            # 内部已 request_stop()）；无 cancel_active 的替身回落 request_stop()
            cancel = getattr(self._runtime, "cancel_active", None)
            if callable(cancel):
                cancel()
            else:
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

            # 合并补充消息：merged_content 进 RunCommand（runtime 内传给 runner.chat），
            # all_contents 逐条写 user 行并作为 exclude_message_ids
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
        """处理单条消息 — 写 user 行 + 推 SSE，提交 RunCommand，返回 holder 文案。

        D-6/I-1：历史读取、`_chat_lock`、`runner.chat`、channel/source 状态、回复持久化
        全部归 MainAgentRuntime（专用线程）；ChatQueue 只剩入队/提交/路由。
        """
        from niu_api.chat import notify_new_message

        store = await get_message_store()

        # I-5：user 行先落库并推 SSE，再提交命令；返回的全部 id 作为 exclude_message_ids，
        # runtime 读历史时必须排除它们（合并路径的 user_contents 同样覆盖）。
        # 所有 source（包括 scheduler）写 DB 后都推 SSE，让前端实时显示 user 气泡——
        # 走 ChatQueue 的路径都经这里推，chat_session 路径不走 ChatQueue，不会双推。
        exclude_message_ids: list[str] = []
        if user_contents:
            for uc in user_contents:
                user_msg_id = await store.add_message(role="user", content=uc)
                await notify_new_message(user_msg_id, "user", uc, source="electron")
                exclude_message_ids.append(str(user_msg_id))
        else:
            user_msg_id = await store.add_message(role="user", content=content)
            await notify_new_message(user_msg_id, "user", content, source="electron")
            exclude_message_ids.append(str(user_msg_id))

        # holder 是命令结果唯一出口：初值空串，各终态由 _persist_outcome 无条件写入
        holder: dict = {"reply": "", "message_id": None, "error": None}

        def on_complete(outcome: RunOutcome) -> None:
            """D-5：runtime 线程内被调用（持锁、handle 收口前）——桥回主循环做持久化。

            不设短超时（保 I-4：持久化完成前 handle 不 done，下一条命令不会先跑）；
            等待期间每 60s 记一条 warning。主循环不可用 / 持久化异常只记录，
            不改变命令结果。
            """
            from niu_api.chat import _main_loop

            loop = _main_loop
            if loop is None or loop.is_closed():
                # 桥不回去也必须给 user 行一个回复文案：holder 是命令结果唯一出口
                logger.error("[ChatQueue] Main loop unavailable, outcome not persisted")
                holder["reply"] = "[系统繁忙，请重试]"
                return
            fut = asyncio.run_coroutine_threadsafe(
                self._persist_outcome(store, outcome, holder), loop,
            )
            waited = 0.0
            while True:
                try:
                    fut.result(timeout=60.0)
                    return
                except TimeoutError:
                    if fut.done():
                        # 持久化协程自身抛 TimeoutError（不是这里的等待超时）
                        logger.error("[ChatQueue] Persist coroutine raised TimeoutError")
                        return
                    waited += 60.0
                    logger.warning(
                        f"[ChatQueue] on_complete persist still running ({waited:.0f}s)"
                    )
                except Exception as e:
                    logger.error(f"[ChatQueue] on_complete persist failed: {e}")
                    return

        cmd = RunCommand(
            content=content,
            session_id=session_id,
            source=source,
            channel=channel,
            channel_id=channel_id,
            stream=False,
            exclude_message_ids=exclude_message_ids,
            on_complete=on_complete,
        )
        # §4.3：reply_future 不塞进 RunCommand——它仍只由 _process_with_merge finally 收口
        try:
            handle = self._runtime.submit(cmd)
        except Exception as exc:
            # submit 失败（runtime 已停）→ 命令永不执行：在事件循环线程内直接落中性降级
            # 回复（不走 on_complete——同线程 .result() 会自锁），保证 user 行有回复
            logger.error(f"[ChatQueue] Submit failed: {exc}")
            await self._persist_outcome(
                store, RunOutcome(error=exc, neutral_degrade=True), holder,
            )
            return holder["reply"] or "[系统繁忙，请重试]"

        # 与现状 `await sf` 一致：不设外层超时（先返回会让 on_complete 稍后落真回复 → 双回复）
        await asyncio.to_thread(handle.wait)
        if not holder["reply"]:
            holder["reply"] = "[系统繁忙，请重试]"
        return holder["reply"]

    async def _persist_outcome(self, store, outcome: RunOutcome, holder: dict) -> None:
        """RunOutcome → DB / SSE / holder（P3 D-1：实现已抽到 niu_api.main_agent_persist，三端点共用一份）。

        只在主循环线程（on_complete 经 run_coroutine_threadsafe 桥回）或 submit 失败路径调用。
        不在这里 drain supplement——由 runtime 在释放锁之后处理（§4.1 步骤 6）。
        """
        await persist_run_outcome(store, outcome, holder)


# ============== 全局单例 ==============

_queue: ChatQueue | None = None
_queue_stopped: bool = False


def get_chat_queue() -> ChatQueue:
    """获取全局 ChatQueue 实例（D-12：生产在此注入 MainAgentRuntime 单例）"""
    global _queue, _queue_stopped
    if _queue_stopped:
        raise RuntimeError("ChatQueue has been stopped")
    if _queue is None:
        from niu_api.chat import get_or_create_runner
        runner = get_or_create_runner()
        _queue = ChatQueue(runner, MainAgentRuntime.instance(runner))
    return _queue


async def start_chat_queue():
    """启动 ChatQueue（FastAPI startup 调用）——I-7：runner → runtime → worker"""
    q = get_chat_queue()
    q._runtime.set_runner(q._runner)
    q._runtime.start()
    await q.start()


async def stop_chat_queue():
    """停止 ChatQueue（FastAPI shutdown 调用）——I-7：反向关闭

    runtime 的 join 不在主循环线程（D-7）：先停 worker，再 `to_thread` 停 runtime
    （先 cancel active/pending，再等 active 自己收口——on_complete 才能落库）。
    `_queue_stopped` 保持 True：关服期间新 enqueue 必须显式失败，不静默造未启动队列。
    """
    global _queue, _queue_stopped
    _queue_stopped = True
    if _queue:
        await _queue.stop()
        await asyncio.to_thread(MainAgentRuntime.stop_instance)
        _queue = None
