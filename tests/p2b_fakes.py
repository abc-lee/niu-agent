"""P2b 测试替身：`FakeRuntime` + 测试专用主循环线程（Task 2b 测试迁移专用）。

P2b 之后 ChatQueue 只剩入队 / 提交 / 路由（I-1）：历史读取、`_chat_lock`、
`runner.chat`、`RunOutcome` 构造与 `on_complete` 桥接都在 runtime 线程内。
`FakeRuntime` 复刻这些契约——独立线程串行执行命令、history 可注入、
`on_complete` 先于 handle 收口——但不引入真实 DB、真实 `_chat_lock`
或 `MainAgentRuntime` 单例。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import threading

from agent.main_runtime import RunCommand, RunOutcome

logger = logging.getLogger(__name__)


_STOP = object()
# 队列排空后 worker 线程自行退出，用例之间不残留线程
_IDLE_EXIT_SECONDS = 0.2


class FakeHandle:
    """`RunHandle` 替身：`wait()` 阻塞到命令收口（`on_complete` 已跑完）。"""

    def __init__(self, command: RunCommand, runtime: FakeRuntime):
        self.command = command
        self.id = command.id
        self.result: str | None = None
        self.error: str | None = None
        self._runtime = runtime
        self._done = threading.Event()
        self._stop_latched = False
        self._finished = False
        self._lock = threading.Lock()
        # D-3：与 `RunHandle._events` 同语义——记录已投递的 run 事件，供用例断言
        self.events: list[dict] = []

    def wait(self, timeout: float | None = None) -> str | None:
        if not self._done.wait(timeout):
            raise TimeoutError("run did not finish")
        return self.result

    def cancel(self) -> None:
        self._stop_latched = True
        if self._runtime is not None:
            self._runtime.cancel_active()

    def _emit(self, event: dict) -> None:
        """`RunHandle._emit` 等价：先记录事件，**出锁后**再调 `command.on_event`（D-3）。

        sink 抛错只记录，不影响 run（R1）——与生产 `_emit` 的捕获一致。
        """
        self.events.append(event)
        sink = self.command.on_event
        if sink is None:
            return
        try:
            sink(event)
        except Exception:
            logger.exception("[FakeRuntime] on_event sink failed")

    def _finish(self, result: str | None, error: str | None) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        self.result = result
        self.error = error
        self._done.set()


class FakeRuntime:
    """`MainAgentRuntime` 替身：`submit()` 把命令交给独立线程执行（D-2 / D-5 契约）。

    - 命令 FIFO、一次一条（与生产 runtime 线程一致）；
    - `history` 固定 / 可注入（I-2：ChatQueue 不再取历史）；
    - `lock_error` 非 None → 复刻 `_chat_lock` 获取失败：不调 `runner.chat`，
      直接产出 `RunOutcome(error=lock_error)`；
    - `on_complete` 在 runtime 线程内、handle 收口前调用（D-5）；
    - `commands` / `handles` / `outcomes` 记录提交与产出，供用例断言命令字段；
    - run 事件（chunk / done / error / cancelled）经 `handle._emit` 投递，次序与
      生产 `_finish_handle` 一致：先 chunk，再恰好一个终止事件（D-3）。
    """

    def __init__(self, runner=None, history: list | None = None,
                 lock_error: BaseException | None = None):
        self._runner = runner
        self.history = [] if history is None else list(history)
        self.lock_error = lock_error
        self.commands: list[RunCommand] = []
        self.outcomes: list[RunOutcome] = []
        self.handles: list[FakeHandle] = []
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._stopped = False
        self._active: FakeHandle | None = None
        self._pending: set[FakeHandle] = set()
        self._state_lock = threading.RLock()

    # ---------- 与生产同名的接口（ChatQueue 只用 set_runner / submit） ----------

    def set_runner(self, runner) -> None:
        with self._state_lock:
            self._runner = runner

    def start(self) -> None:
        with self._state_lock:
            self._stopped = False
            self._ensure_thread()

    def stop(self, timeout: float = 5.0) -> bool:
        with self._state_lock:
            self._stopped = True
            self._running = False
            active = self._active
            thread = self._thread
        if active is not None:
            active._stop_latched = True
        if thread is not None and thread.is_alive():
            self._queue.put(_STOP)
            thread.join(timeout)
        with self._state_lock:
            self._thread = None
        return True

    def is_busy(self) -> bool:
        with self._state_lock:
            return self._active is not None or bool(self._pending)

    def is_round_active(self) -> bool:
        """`MainAgentRuntime.is_round_active` 等价（I-6）：active 命令，或 `_chat_lock`
        被持有（compat `/compact`、`/clear`）；pending 不算。`_chat_lock` 惰性 import，
        取不到时只回 active——测试把 `niu_api.compat._chat_lock` 换成假锁即可覆盖持锁分支。
        """
        with self._state_lock:
            active = self._active is not None
        try:
            from niu_api.compat import _chat_lock
        except Exception:
            return active
        try:
            return active or _chat_lock.locked()
        except Exception:
            return active


    def cancel_active(self) -> None:
        with self._state_lock:
            active = self._active
            pending = list(self._pending)
        for handle in pending:
            handle._stop_latched = True
        if active is not None:
            active._stop_latched = True

    def submit(self, command: RunCommand) -> FakeHandle:
        handle = FakeHandle(command, self)
        with self._state_lock:
            if self._stopped:
                raise RuntimeError("fake runtime is stopped")
            self.commands.append(command)
            self.handles.append(handle)
            self._pending.add(handle)
            self._queue.put((command, handle))
            self._ensure_thread()
        return handle

    # ---------- 内部 ----------

    def _ensure_thread(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._running = True
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="fake-main-agent-runtime", daemon=True,
        )
        self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=_IDLE_EXIT_SECONDS)
            except queue.Empty:
                with self._state_lock:
                    self._thread = None
                    self._running = False
                return
            if item is _STOP:
                with self._state_lock:
                    self._thread = None
                    self._running = False
                return
            command, handle = item
            with self._state_lock:
                self._pending.discard(handle)
                self._active = handle
                runner = self._runner
            try:
                try:
                    outcome = self._execute(command, handle, runner)
                except BaseException as exc:  # 复刻 _loop_body：命令自身崩溃也收口
                    logger.exception("[FakeRuntime] command failed")
                    outcome = RunOutcome(error=exc)
                self.outcomes.append(outcome)
                self._call_on_complete(handle, outcome)
            finally:
                with self._state_lock:
                    if self._active is handle:
                        self._active = None
            self._finish_handle(handle, outcome)

    def _execute(self, command: RunCommand, handle: FakeHandle, runner) -> RunOutcome:
        """独立线程内跑一条命令，产出 `RunOutcome`（与 `_execute` 的取值规则一致）。"""
        chunks: list[str] = []
        gen = None
        outcome: RunOutcome | None = None
        advanced = False  # 出现过 reply/str chunk
        pumped = False    # 生成器至少被推进过一次

        if self.lock_error is not None:
            # D-4 等价：锁获取失败保留错误分类，runner.chat 永不调用
            outcome = RunOutcome(error=self.lock_error)
        else:
            try:
                gen = runner.chat(
                    command.session_id, command.content, stream=command.stream,
                    history=list(self.history), resources=command.resources,
                    channel_id=command.channel_id,
                )
                while True:
                    try:
                        chunk = next(gen)
                    except StopIteration:
                        pumped = True
                        break
                    pumped = True
                    if chunk:
                        chunks.append(chunk)
                        handle._emit({"type": "chunk", "id": handle.id, "content": chunk})
                        advanced = True
            except BaseException as exc:
                outcome = RunOutcome(error=exc)

        if outcome is None:
            rv = getattr(runner, "last_return_value", None)
            if not advanced and pumped and getattr(runner, "_persisted_msgs", None):
                advanced = True
            cancelled = (
                handle._stop_latched
                or (advanced and rv is None)
                or (isinstance(rv, dict) and rv.get("result") == "STOPPED")
            )
            if cancelled and not advanced:
                # 生成器从未推进：本轮结果一律不可信
                outcome = RunOutcome(result="".join(chunks), cancelled=True)
            else:
                outcome = RunOutcome(
                    result="".join(chunks),
                    cancelled=cancelled,
                    return_value=rv,
                    persisted_msgs=getattr(runner, "_persisted_msgs", None),
                    extracted_at_msgs=getattr(runner, "_extracted_at_msgs", None),
                )

        if gen is not None:
            try:
                gen.close()
            except Exception:
                logger.exception("[FakeRuntime] close generator failed")
        outcome.history_len = len(self.history)
        return outcome

    @staticmethod
    def _call_on_complete(handle: FakeHandle, outcome: RunOutcome) -> None:
        """D-5：恰好一次，在 runtime 线程内、handle 收口前。"""
        callback = handle.command.on_complete
        if callback is None:
            return
        try:
            callback(outcome)
        except Exception:
            logger.exception("[FakeRuntime] on_complete failed for %s", handle.id)

    @staticmethod
    def _finish_handle(handle: FakeHandle, outcome: RunOutcome) -> None:
        """终止事件与收口：与生产 `_finish_handle` 同映射（D-3 / I-3）。"""
        if outcome.cancelled:
            handle._emit({"type": "cancelled", "id": handle.id, "error": "cancelled"})
            handle._finish(None, "cancelled")
        elif outcome.error is not None:
            handle._emit({"type": "error", "id": handle.id, "error": str(outcome.error)})
            handle._finish(None, str(outcome.error))
        else:
            handle._emit({"type": "done", "id": handle.id})
            handle._finish(outcome.result, None)


@contextlib.contextmanager
def main_loop_thread():
    """测试专用 asyncio loop 线程，`niu_api.chat._main_loop` 指向它。

    `on_complete` 用 `run_coroutine_threadsafe` 把持久化桥回主循环（D-5），生产里
    那是 uvicorn 的 loop；测试在此把它指向专用线程，退出时恢复原值。
    """
    from niu_api import chat as chat_mod

    previous = chat_mod._main_loop
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="test-main-loop", daemon=True)
    thread.start()
    chat_mod._main_loop = loop
    try:
        yield loop
    finally:
        chat_mod._main_loop = previous
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        if not thread.is_alive():
            loop.close()
