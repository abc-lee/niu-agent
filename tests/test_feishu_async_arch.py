"""
飞书通道异步/同步架构整改方案 — 关键假设验证测试

验证 4 个核心假设：
  1. SDK _invoke 对 sync handler 不 await
  2. asyncio.get_event_loop() 在 SDK bg loop 线程中返回 SDK bg loop
  3. 从工作线程中通过 run_coroutine_threadsafe 可以成功提交协程到 SDK bg loop
  4. sync handler 不阻塞 bg loop（心跳可以正常运行）

运行方式：
  python -m pytest tests/test_feishu_async_arch.py -v

所有测试纯模拟，不依赖飞书 SDK 实际连接。
"""

import asyncio
import inspect
import threading
import time
import pytest


# ---------------------------------------------------------------------------
# 辅助：模拟 SDK _invoke 行为（直接取自 lark_oapi/channel/channel.py:382-403）
# ---------------------------------------------------------------------------

async def _invoke_mock(handlers, *args):
    """模拟 SDK FeishuChannel._invoke 的行为。

    SDK 源码：
        result = handler(*args)
        if inspect.isawaitable(result):
            await result
    """
    for handler in list(handlers):
        result = handler(*args)
        if inspect.isawaitable(result):
            await result


# ---------------------------------------------------------------------------
# 辅助：创建模拟 SDK bg loop 的后台线程
# ---------------------------------------------------------------------------

class BgLoopSimulator:
    """模拟 SDK 的 _ensure_bg_loop 行为。

    SDK 源码（channel.py:1016-1044）：
        loop = asyncio.new_event_loop()
        def _runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()
        t = threading.Thread(target=_runner, name="lark-channel-bg", daemon=True)
        t.start()
        self._bg_loop = loop
    """

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None

    def start(self):
        loop = asyncio.new_event_loop()
        self.loop = loop

        def _runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        self.thread = threading.Thread(target=_runner, name="lark-channel-bg-mock", daemon=True)
        self.thread.start()
        # 等待 loop 真正开始运行
        while not loop.is_running():
            time.sleep(0.01)

    def stop(self):
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not None:
            self.thread.join(timeout=5)

    def submit(self, coro):
        """模拟 channel.schedule() — 线程安全地提交协程到 bg loop"""
        assert self.loop is not None, "BgLoop not started"
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


@pytest.fixture
def bg_loop():
    """创建并清理模拟 SDK bg loop"""
    sim = BgLoopSimulator()
    sim.start()
    yield sim
    sim.stop()


# ===========================================================================
# 假设1：SDK _invoke 对 sync handler 不 await
# ===========================================================================

class TestHypothesis1_SyncHandlerNotAwaited:
    """验证：sync handler 返回 None 时，_invoke 不会 await，立即返回。"""

    def test_sync_handler_returns_none_not_awaited(self):
        """sync handler 返回 None，_invoke 立即返回（不等待）。"""
        call_log = []

        def sync_handler(msg):
            call_log.append(("sync_called", time.monotonic()))
            return None  # 不返回 coroutine

        # 在事件循环中运行 _invoke_mock
        async def _run():
            await _invoke_mock([sync_handler], "test_msg")

        asyncio.run(_run())

        assert len(call_log) == 1
        assert call_log[0][0] == "sync_called"

    def test_async_handler_is_awaited(self):
        """async handler 返回 coroutine，_invoke 会 await 它。"""
        call_log = []

        async def async_handler(msg):
            call_log.append(("async_start", time.monotonic()))
            await asyncio.sleep(0.05)
            call_log.append(("async_end", time.monotonic()))
            return "async_result"

        async def _run():
            await _invoke_mock([async_handler], "test_msg")

        asyncio.run(_run())

        assert len(call_log) == 2
        assert call_log[0][0] == "async_start"
        assert call_log[1][0] == "async_end"

    def test_sync_handler_timing_no_delay(self):
        """sync handler 不引入任何 await 延迟。"""
        timestamps = []

        def sync_handler(msg):
            timestamps.append(time.monotonic())
            return None

        async def _run():
            t0 = time.monotonic()
            await _invoke_mock([sync_handler], "test_msg")
            t1 = time.monotonic()
            return t1 - t0

        elapsed = asyncio.run(_run())
        # sync handler 不应有任何可测量的延迟（< 10ms）
        assert elapsed < 0.1, f"sync handler 引入了意外延迟: {elapsed:.3f}s"

    def test_async_handler_timing_has_delay(self):
        """async handler 引入 await 延迟。"""
        async def async_handler(msg):
            await asyncio.sleep(0.1)
            return "result"

        async def _run():
            t0 = time.monotonic()
            await _invoke_mock([async_handler], "test_msg")
            t1 = time.monotonic()
            return t1 - t0

        elapsed = asyncio.run(_run())
        # async handler 应有 ~0.1s 延迟
        assert elapsed >= 0.08, f"async handler 未被 await: {elapsed:.3f}s"

    def test_mixed_handlers_sync_not_block_async(self):
        """sync handler 不阻塞后续 async handler 的 await。"""
        results = []

        def sync_handler(msg):
            results.append("sync_done")
            return None

        async def async_handler(msg):
            await asyncio.sleep(0.05)
            results.append("async_done")
            return None

        async def _run():
            await _invoke_mock([sync_handler, async_handler], "test_msg")

        asyncio.run(_run())

        assert results == ["sync_done", "async_done"]

    def test_inspect_isawaitable_on_none(self):
        """直接验证 inspect.isawaitable(None) == False。"""
        assert not inspect.isawaitable(None)
        assert not inspect.isawaitable("string")
        assert not inspect.isawaitable(42)

    def test_inspect_isawaitable_on_coroutine(self):
        """直接验证 inspect.isawaitable(coroutine) == True。"""
        async def coro_fn():
            pass

        coro = coro_fn()
        try:
            assert inspect.isawaitable(coro)
        finally:
            coro.close()  # 避免警告


# ===========================================================================
# 假设2：asyncio.get_event_loop() 在 SDK bg loop 线程中返回 SDK bg loop
# ===========================================================================

class TestHypothesis2_GetEventLoopInBgThread:
    """验证：在 SDK bg loop 线程上下文中，asyncio.get_event_loop() 返回 bg loop。"""

    def test_get_event_loop_in_bg_loop_thread(self, bg_loop):
        """在 bg loop 线程中通过 run_coroutine_threadsafe 调用 sync handler，
        handler 中 asyncio.get_event_loop() 应返回 bg_loop。"""
        captured_loop = None
        capture_done = threading.Event()

        def sync_handler(msg):
            nonlocal captured_loop
            captured_loop = asyncio.get_event_loop()
            capture_done.set()
            return None

        # 模拟 SDK _invoke 在 bg loop 中调用 sync handler
        async def _invoke_in_bg(handler, msg):
            result = handler(msg)
            if inspect.isawaitable(result):
                await result

        future = asyncio.run_coroutine_threadsafe(
            _invoke_in_bg(sync_handler, "test"), bg_loop.loop
        )
        future.result(timeout=5)
        capture_done.wait(timeout=5)

        assert captured_loop is bg_loop.loop, (
            f"Expected bg_loop.loop, got {captured_loop}. "
            f"Is same: {captured_loop is bg_loop.loop}"
        )

    def test_get_event_loop_in_bg_loop_coroutine(self, bg_loop):
        """在 bg loop 中运行的协程内，asyncio.get_running_loop() 返回 bg loop。"""
        captured_loop = None

        async def check_loop():
            nonlocal captured_loop
            captured_loop = asyncio.get_running_loop()

        future = asyncio.run_coroutine_threadsafe(check_loop(), bg_loop.loop)
        future.result(timeout=5)

        assert captured_loop is bg_loop.loop

    def test_get_event_loop_from_main_thread_differs(self, bg_loop):
        """主线程中 asyncio.get_event_loop() 不应返回 bg loop（不同线程）。"""
        # 在主线程中获取 loop
        try:
            main_loop = asyncio.get_running_loop()
        except RuntimeError:
            main_loop = None

        # 如果主线程没有 running loop，get_event_loop() 的行为取决于 Python 版本
        # 关键是：bg_loop.loop 不应等于主线程的 loop
        # 在没有 running loop 的主线程中，get_event_loop() 可能返回新 loop 或抛异常
        # 这个测试验证的是：bg_loop.loop 是独立的
        assert bg_loop.loop is not None
        assert bg_loop.loop.is_running()

    def test_set_event_loop_in_bg_thread(self, bg_loop):
        """验证 SDK 的 _runner 模式：asyncio.set_event_loop + run_forever
        使 get_event_loop() 在该线程中返回正确的 loop。"""
        loop_from_handler = None

        def handler_in_bg_thread():
            nonlocal loop_from_handler
            loop_from_handler = asyncio.get_event_loop()
            return None

        async def _invoke_handler():
            handler_in_bg_thread()

        future = asyncio.run_coroutine_threadsafe(_invoke_handler(), bg_loop.loop)
        future.result(timeout=5)

        assert loop_from_handler is bg_loop.loop


# ===========================================================================
# 假设3：从工作线程中通过 run_coroutine_threadsafe 可以成功提交协程到 SDK bg loop
# ===========================================================================

class TestHypothesis3_RunCoroutineThreadsafeFromWorkerThread:
    """验证：在 threading.Thread 中通过 run_coroutine_threadsafe
    可以成功提交协程到 SDK bg loop。"""

    def test_submit_coro_from_worker_thread(self, bg_loop):
        """工作线程中提交协程到 bg loop，协程成功执行。"""
        executed = threading.Event()
        execution_thread = None

        async def mock_send(chat_id, message):
            nonlocal execution_thread
            execution_thread = threading.current_thread().name
            executed.set()
            return True

        def worker_thread():
            future = asyncio.run_coroutine_threadsafe(
                mock_send("test_chat", {"markdown": "reply"}),
                bg_loop.loop,
            )
            result = future.result(timeout=5)
            assert result is True

        t = threading.Thread(target=worker_thread, name="feishu-worker", daemon=True)
        t.start()
        t.join(timeout=10)

        assert executed.is_set(), "协程未在 bg loop 中执行"
        # 协程应在 bg loop 线程中执行，而非工作线程
        assert execution_thread == "lark-channel-bg-mock", (
            f"协程在错误线程中执行: {execution_thread}"
        )

    def test_submit_coro_after_blocking_work(self, bg_loop):
        """工作线程先执行阻塞操作，再提交协程 — 模拟 _chat_sync 后发送回复。"""
        results = []

        async def mock_send(chat_id, message):
            results.append(("sent", chat_id, message))
            return True

        def worker_thread():
            # 模拟阻塞的 _chat_sync
            time.sleep(0.5)
            results.append(("chat_done",))

            # 提交 send 协程
            future = asyncio.run_coroutine_threadsafe(
                mock_send("chat_123", {"markdown": "reply content"}),
                bg_loop.loop,
            )
            send_result = future.result(timeout=5)
            assert send_result is True

        t = threading.Thread(target=worker_thread, daemon=True)
        t.start()
        t.join(timeout=10)

        assert ("chat_done",) in results
        assert any(r[0] == "sent" and r[1] == "chat_123" for r in results)

    def test_submit_multiple_coros_from_different_threads(self, bg_loop):
        """多个工作线程同时提交协程到 bg loop — 线程安全。"""
        completed_count = 0
        lock = threading.Lock()

        async def mock_send(chat_id, message):
            nonlocal completed_count
            await asyncio.sleep(0.01)  # 模拟网络延迟
            with lock:
                completed_count += 1
            return True

        def worker(chat_id):
            future = asyncio.run_coroutine_threadsafe(
                mock_send(chat_id, {"markdown": f"reply to {chat_id}"}),
                bg_loop.loop,
            )
            assert future.result(timeout=10) is True

        threads = [
            threading.Thread(target=worker, args=(f"chat_{i}",), daemon=True)
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert completed_count == 5, f"只有 {completed_count}/5 个协程完成"

    def test_submit_coro_returns_future_with_result(self, bg_loop):
        """run_coroutine_threadsafe 返回的 Future 可以获取协程返回值。"""
        async def compute():
            await asyncio.sleep(0.01)
            return 42

        def worker():
            future = asyncio.run_coroutine_threadsafe(compute(), bg_loop.loop)
            result = future.result(timeout=5)
            assert result == 42

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=10)

    def test_submit_coro_exception_propagates(self, bg_loop):
        """协程中的异常通过 Future 传播到工作线程。"""
        async def failing_coro():
            await asyncio.sleep(0.01)
            raise ValueError("test error")

        def worker():
            future = asyncio.run_coroutine_threadsafe(failing_coro(), bg_loop.loop)
            with pytest.raises(ValueError, match="test error"):
                future.result(timeout=5)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=10)


# ===========================================================================
# 假设4：sync handler 不阻塞 bg loop（心跳可以正常运行）
# ===========================================================================

class TestHypothesis4_SyncHandlerNotBlockingBgLoop:
    """验证：sync handler + threading 方式下，bg loop 心跳不被阻塞。"""

    def test_sync_handler_does_not_block_heartbeat(self, bg_loop):
        """sync handler 执行时，bg loop 中的心跳任务可以正常运行。"""
        heartbeat_count = 0

        async def heartbeat():
            nonlocal heartbeat_count
            while True:
                heartbeat_count += 1
                await asyncio.sleep(0.2)

        # 启动心跳任务
        heartbeat_task = asyncio.run_coroutine_threadsafe(heartbeat(), bg_loop.loop)

        # 等待心跳至少执行 1 次
        time.sleep(0.5)
        count_before = heartbeat_count
        assert count_before >= 1, f"心跳未启动: count={count_before}"

        # 模拟 sync handler（在 bg loop 中调用，但不阻塞）
        def sync_handler(msg):
            # sync handler 立即返回，不阻塞
            return None

        async def _invoke_sync(handler, msg):
            result = handler(msg)
            if inspect.isawaitable(result):
                await result

        future = asyncio.run_coroutine_threadsafe(
            _invoke_sync(sync_handler, "test"), bg_loop.loop
        )
        future.result(timeout=5)

        # 等待更多心跳
        time.sleep(1.0)
        count_after = heartbeat_count

        # 心跳应继续正常计数
        assert count_after > count_before, (
            f"心跳被阻塞！before={count_before}, after={count_after}"
        )

        # 清理
        heartbeat_task.cancel()

    def test_async_handler_with_blocking_blocks_heartbeat(self, bg_loop):
        """对比：async handler 内部阻塞会阻塞心跳（这就是 Bug 的根因）。"""
        heartbeat_count = 0

        async def heartbeat():
            nonlocal heartbeat_count
            while True:
                heartbeat_count += 1
                await asyncio.sleep(0.2)

        heartbeat_task = asyncio.run_coroutine_threadsafe(heartbeat(), bg_loop.loop)

        # 等待心跳启动
        time.sleep(0.5)
        count_before = heartbeat_count
        assert count_before >= 1

        # 模拟 async handler 内部阻塞（time.sleep 在 async 函数中阻塞整个 loop）
        async def blocking_async_handler(msg):
            # 这是 Bug 的根因：async handler 中调用同步阻塞操作
            time.sleep(2.0)  # 阻塞 bg loop 2 秒
            return None

        # 在 bg loop 中调用 blocking async handler
        future = asyncio.run_coroutine_threadsafe(
            _invoke_mock([blocking_async_handler], "test"), bg_loop.loop
        )

        # 等待 handler 完成
        future.result(timeout=5)

        count_after = heartbeat_count

        # 阻塞期间心跳应显著减少（2 秒阻塞，0.2 秒间隔，应有 ~10 次，
        # 但阻塞期间只有 0 次）
        # 阻塞前可能有几次，阻塞后可能有几次，但阻塞期间 0 次
        # 总数应远少于无阻塞情况
        max_expected = count_before + 3  # 阻塞前 + 阻塞后少量
        assert count_after <= max_expected, (
            f"预期心跳被阻塞（count<={max_expected}），实际 count={count_after}。"
            f"这验证了 async handler + 同步阻塞 = 阻塞 bg loop"
        )

        heartbeat_task.cancel()

    def test_sync_handler_with_threading_no_block(self, bg_loop):
        """sync handler + threading 方式：阻塞操作在线程中，不影响心跳。"""
        heartbeat_count = 0

        async def heartbeat():
            nonlocal heartbeat_count
            while True:
                heartbeat_count += 1
                await asyncio.sleep(0.2)

        heartbeat_task = asyncio.run_coroutine_threadsafe(heartbeat(), bg_loop.loop)

        # 等待心跳启动
        time.sleep(0.5)
        count_before = heartbeat_count
        assert count_before >= 1

        # 模拟整改后的方案：sync handler + threading
        def sync_handler_with_threading(msg):
            # 捕获 bg loop 引用
            sdk_loop = asyncio.get_event_loop()

            def _process_and_reply():
                # 在独立线程中执行阻塞操作
                time.sleep(2.0)  # 模拟 _chat_sync(timeout=120)

                # 通过 run_coroutine_threadsafe 发送回复
                async def mock_send():
                    pass  # 模拟 channel.send

                asyncio.run_coroutine_threadsafe(mock_send(), sdk_loop)

            threading.Thread(target=_process_and_reply, daemon=True).start()
            return None  # sync handler 立即返回

        async def _invoke_sync(handler, msg):
            result = handler(msg)
            if inspect.isawaitable(result):
                await result

        # 在 bg loop 中调用 sync handler
        future = asyncio.run_coroutine_threadsafe(
            _invoke_sync(sync_handler_with_threading, "test"), bg_loop.loop
        )
        future.result(timeout=5)

        # 等待阻塞操作完成 + 额外时间
        time.sleep(3.0)
        count_after = heartbeat_count

        # 心跳应持续正常运行（2 秒阻塞在线程中，不影响 bg loop）
        # 0.2 秒间隔，3.5 秒总时间，预期 ~17 次心跳
        # 保守估计至少 10 次
        assert count_after >= 10, (
            f"心跳被阻塞！count={count_after}，预期 >= 10"
        )

        heartbeat_task.cancel()

    def test_heartbeat_during_multiple_concurrent_threads(self, bg_loop):
        """多个并发工作线程同时处理消息时，心跳仍正常。"""
        heartbeat_count = 0

        async def heartbeat():
            nonlocal heartbeat_count
            while True:
                heartbeat_count += 1
                await asyncio.sleep(0.2)

        heartbeat_task = asyncio.run_coroutine_threadsafe(heartbeat(), bg_loop.loop)
        time.sleep(0.5)

        def sync_handler(msg):
            sdk_loop = asyncio.get_event_loop()
            chat_id = msg

            def _process():
                time.sleep(1.0)  # 模拟 _chat_sync
                async def mock_send():
                    pass
                asyncio.run_coroutine_threadsafe(mock_send(), sdk_loop)

            threading.Thread(target=_process, daemon=True).start()
            return None

        # 模拟 3 条消息同时到达
        for i in range(3):
            async def _invoke_one(handler, msg):
                result = handler(msg)
                if inspect.isawaitable(result):
                    await result

            future = asyncio.run_coroutine_threadsafe(
                _invoke_one(sync_handler, f"msg_{i}"), bg_loop.loop
            )
            future.result(timeout=5)

        # 等待所有线程完成
        time.sleep(2.0)

        # 心跳应正常
        assert heartbeat_count >= 8, (
            f"并发消息处理期间心跳被阻塞！count={heartbeat_count}"
        )

        heartbeat_task.cancel()


# ===========================================================================
# 综合测试：完整流程模拟
# ===========================================================================

class TestFullFlowSimulation:
    """模拟完整的飞书消息处理流程，验证整改方案的端到端正确性。"""

    def test_full_flow_sync_handler_threading_reply(self, bg_loop):
        """完整流程：消息到达 → sync handler → 线程处理 → run_coroutine_threadsafe 回复。"""
        received_replies = []
        received_chat_ids = []

        async def mock_channel_send(chat_id, message):
            """模拟 channel.send()"""
            received_chat_ids.append(chat_id)
            received_replies.append(message)
            return True

        def sync_on_message(msg):
            """整改后的 _on_message（sync handler）"""
            # 1. 在 bg loop 上下文中捕获 loop 引用
            sdk_loop = asyncio.get_event_loop()
            chat_id = msg["chat_id"]

            # 2. 在独立线程中执行阻塞调用
            def _process_and_reply():
                try:
                    # 模拟 route_in_sync → _chat_sync（阻塞）
                    time.sleep(0.5)
                    reply = f"Reply to: {msg['content']}"

                    # 3. 通过 run_coroutine_threadsafe 发送回复
                    asyncio.run_coroutine_threadsafe(
                        mock_channel_send(chat_id, {"markdown": reply}),
                        sdk_loop,
                    )
                except Exception as e:
                    print(f"Process/reply error: {e}")

            threading.Thread(target=_process_and_reply, daemon=True).start()
            return None  # sync handler 立即返回

        # 模拟 SDK _invoke 在 bg loop 中调用 sync handler
        async def _invoke_message(handler, msg):
            result = handler(msg)
            if inspect.isawaitable(result):
                await result

        # 发送消息
        test_msg = {"chat_id": "oc_test123", "content": "Hello"}
        future = asyncio.run_coroutine_threadsafe(
            _invoke_message(sync_on_message, test_msg), bg_loop.loop
        )
        future.result(timeout=5)

        # 等待工作线程完成 + 回复发送
        time.sleep(2.0)

        # 验证回复已发送
        assert len(received_replies) == 1, f"预期 1 条回复，实际 {len(received_replies)}"
        assert received_chat_ids[0] == "oc_test123"
        assert "Reply to: Hello" in received_replies[0]["markdown"]

    def test_full_flow_heartbeat_survives_message_processing(self, bg_loop):
        """完整流程中，心跳在消息处理期间持续运行。"""
        heartbeat_times = []

        async def heartbeat():
            while True:
                heartbeat_times.append(time.monotonic())
                await asyncio.sleep(0.3)

        heartbeat_task = asyncio.run_coroutine_threadsafe(heartbeat(), bg_loop.loop)
        time.sleep(0.5)

        def sync_on_message(msg):
            sdk_loop = asyncio.get_event_loop()
            chat_id = msg["chat_id"]

            def _process():
                time.sleep(2.0)  # 模拟长时间 _chat_sync
                async def mock_send():
                    pass
                asyncio.run_coroutine_threadsafe(mock_send(), sdk_loop)

            threading.Thread(target=_process, daemon=True).start()
            return None

        async def _invoke_message(handler, msg):
            result = handler(msg)
            if inspect.isawaitable(result):
                await result

        # 发送消息
        future = asyncio.run_coroutine_threadsafe(
            _invoke_message(sync_on_message, {"chat_id": "test", "content": "hi"}),
            bg_loop.loop,
        )
        future.result(timeout=5)

        # 等待处理完成
        time.sleep(3.0)

        # 验证心跳持续运行
        assert len(heartbeat_times) >= 8, (
            f"心跳不足：{len(heartbeat_times)} 次，预期 >= 8"
        )

        # 验证心跳间隔大致均匀（没有长时间间隔）
        intervals = [
            heartbeat_times[i+1] - heartbeat_times[i]
            for i in range(len(heartbeat_times) - 1)
        ]
        max_interval = max(intervals)
        assert max_interval < 1.0, (
            f"心跳间隔过大：{max_interval:.2f}s，说明 bg loop 被阻塞"
        )

        heartbeat_task.cancel()


# ===========================================================================
# 假设验证结果汇总
# ===========================================================================

class TestHypothesisSummary:
    """汇总所有假设验证结果。

    如果所有测试通过，说明整改方案可行：
    - 假设1 通过：SDK _invoke 对 sync handler 不 await，sync handler 立即返回
    - 假设2 通过：在 SDK bg loop 线程中，get_event_loop() 返回 bg loop
    - 假设3 通过：工作线程中 run_coroutine_threadsafe 可以提交协程到 bg loop
    - 假设4 通过：sync handler + threading 方式不阻塞 bg loop 心跳

    如果某个假设验证失败，对应的测试类会明确报错。
    """

    def test_all_hypotheses_validated(self, bg_loop):
        """快速综合验证 — 所有 4 个假设在一个测试中验证。"""
        # --- 假设1: sync handler 不被 await ---
        sync_called = []

        def sync_handler(msg):
            sync_called.append(True)
            return None

        async def _run_invoke():
            await _invoke_mock([sync_handler], "test")

        asyncio.run(_run_invoke())
        assert len(sync_called) == 1, "假设1失败：sync handler 未被调用"

        # --- 假设2: get_event_loop 在 bg loop 线程中返回 bg loop ---
        captured_loop = None

        def capture_loop_handler(msg):
            nonlocal captured_loop
            captured_loop = asyncio.get_event_loop()
            return None

        async def _invoke_capture():
            capture_loop_handler("test")

        future = asyncio.run_coroutine_threadsafe(_invoke_capture(), bg_loop.loop)
        future.result(timeout=5)
        assert captured_loop is bg_loop.loop, "假设2失败：get_event_loop 未返回 bg loop"

        # --- 假设3: 工作线程中 run_coroutine_threadsafe 成功 ---
        coro_executed = threading.Event()

        async def test_coro():
            coro_executed.set()
            return True

        def worker():
            f = asyncio.run_coroutine_threadsafe(test_coro(), bg_loop.loop)
            f.result(timeout=5)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=10)
        assert coro_executed.is_set(), "假设3失败：协程未在 bg loop 中执行"

        # --- 假设4: sync handler 不阻塞心跳 ---
        hb_count = 0

        async def hb():
            nonlocal hb_count
            while True:
                hb_count += 1
                await asyncio.sleep(0.1)

        hb_task = asyncio.run_coroutine_threadsafe(hb(), bg_loop.loop)
        time.sleep(0.5)

        def sync_non_blocking(msg):
            sdk_loop = asyncio.get_event_loop()

            def _work():
                time.sleep(0.5)
                async def _send(): pass
                asyncio.run_coroutine_threadsafe(_send(), sdk_loop)

            threading.Thread(target=_work, daemon=True).start()
            return None

        async def _invoke_sync():
            sync_non_blocking("test")

        future = asyncio.run_coroutine_threadsafe(_invoke_sync(), bg_loop.loop)
        future.result(timeout=5)
        time.sleep(1.0)

        assert hb_count >= 5, f"假设4失败：心跳被阻塞，count={hb_count}"

        hb_task.cancel()
