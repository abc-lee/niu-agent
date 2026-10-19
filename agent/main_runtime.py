"""Single-threaded main Agent runtime.

Only this runtime's dedicated thread may execute the main Agent
(`NiuRunner.chat`). All entry points submit RunCommand; commands are
processed FIFO, one at a time.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_TERMINAL = object()
_STOP = object()

# D-4/D-3 bridge budgets (tests may shrink them)
_LOCK_ACQUIRE_TIMEOUT = 600.0
_LOCK_FUTURE_TIMEOUT = 610.0
_HISTORY_LOAD_TIMEOUT = 30.0


@dataclass
class RunOutcome:
    """Outcome of one command (D-2). Every exit path builds one, failures included."""

    result: str = ""
    error: BaseException | None = None
    return_value: Any = None
    persisted_msgs: Any = None
    extracted_at_msgs: Any = None
    history_len: int = 0
    cancelled: bool = False
    neutral_degrade: bool = False


@dataclass
class RunCommand:
    content: str
    session_id: str = "default"
    source: str = "user"
    channel: str = "electron"
    channel_id: str = ""
    stream: bool = True
    resources: list | None = None
    reply_future: Any | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # D-1: ids the submitter already persisted (user rows) — excluded from history
    exclude_message_ids: list[str] = field(default_factory=list)
    # D-5: called on the runtime thread, inside _chat_lock, before the handle finishes
    on_complete: Callable[[RunOutcome], None] | None = None
    # D-3: run 事件 sink（chunk/done/error/cancelled）——runtime 线程调用，_emit 锁外
    on_event: Callable[[dict], None] | None = None


class RunHandle:
    def __init__(self, command: RunCommand):
        self.command = command
        self.id = command.id
        self.result: str | None = None
        self.error: str | None = None
        self._events: queue.Queue[Any] = queue.Queue()
        self._done = threading.Event()
        self._finish_lock = threading.Lock()
        self._finished = False
        self._stop_latched = False
        self._on_complete_called = False

    def events(self):
        while True:
            item = self._events.get()
            if item is _TERMINAL:
                return
            yield item

    def wait(self, timeout: float | None = None) -> str | None:
        if not self._done.wait(timeout):
            raise TimeoutError("run did not finish")
        return self.result

    def cancel(self) -> None:
        # Latch this command; only the active command issues the global stop.
        self._stop_latched = True
        runtime = getattr(self, "_runtime", None)
        if runtime is not None:
            with runtime._state_lock:
                is_active = runtime._active is self
            with self._finish_lock:
                finished = self._finished
            if is_active and not finished:
                from agent.runner import request_stop
                request_stop()

    def _emit(self, event: dict) -> None:
        with self._finish_lock:
            if self._finished:
                return
            self._events.put(event)
        # D-3: 出锁后才调 sink——sink 若回调 handle 的任何方法（cancel/events/wait）
        # 都不会自死锁；sink 抛错只记录，不影响 run（R1）
        sink = self.command.on_event
        if sink is not None:
            try:
                sink(event)
            except Exception:
                logger.exception("[MainRuntime] on_event sink failed")

    def _set_result(self, result: str | None) -> None:
        with self._finish_lock:
            if not self._finished:
                self.result = result

    def _set_error(self, error: str | None) -> None:
        with self._finish_lock:
            if not self._finished:
                self.error = error

    def _finish(self, result: str | None, error: str | None) -> None:
        with self._finish_lock:
            if self._finished:
                return
            self._finished = True
            self.result = result
            self.error = error
            self._events.put(_TERMINAL)
        fut = self.command.reply_future
        if fut is not None and not fut.done():
            try:
                loop = fut.get_loop()
            except AttributeError:
                try:
                    if error is not None:
                        fut.set_exception(RuntimeError(error))
                    else:
                        fut.set_result(result if result is not None else "")
                except Exception:
                    pass
            else:
                try:
                    if error is not None:
                        loop.call_soon_threadsafe(fut.set_exception, RuntimeError(error))
                    else:
                        loop.call_soon_threadsafe(fut.set_result, result if result is not None else "")
                except Exception as exc:
                    if loop.is_closed():
                        try:
                            if error is not None:
                                fut.set_exception(RuntimeError(error))
                            else:
                                fut.set_result(result if result is not None else "")
                        except Exception:
                            pass
                    else:
                        logger.warning("[MainRuntime] future scheduling failed: %s", exc)
        self._done.set()


class MainAgentRuntime:
    _instance: MainAgentRuntime | None = None
    _instance_lock = threading.Lock()

    def __init__(self, runner=None):
        self._runner = runner
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._active: RunHandle | None = None
        self._pending_handles: set[RunHandle] = set()
        self._loop_exited = threading.Event()
        self._state_lock = threading.RLock()
        self._start_lock = threading.Lock()

    @classmethod
    def instance(cls, runner=None) -> MainAgentRuntime:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(runner=runner)
            return cls._instance

    def set_runner(self, runner) -> None:
        """D-7: start/reload_runner keep the runtime in step with the runner."""
        with self._state_lock:
            self._runner = runner

    def start(self) -> None:
        with self._start_lock:
            with self._state_lock:
                thread = self._thread
                if thread is not None and thread.is_alive() and not self._loop_exited.is_set():
                    if not self._running:
                        raise RuntimeError("main runtime is stopping")
                    return
            if thread is not None and thread.is_alive():
                thread.join(0.1)
                if thread.is_alive():
                    raise RuntimeError("main runtime is stopping")
            with self._state_lock:
                current = self._thread
                if current is not None and current.is_alive():
                    if not self._running:
                        raise RuntimeError("main runtime is stopping")
                    return
                if self._thread is thread:
                    self._thread = None
                self._drain_after_stop(call_on_complete=False)
                self._running = True
                self._loop_exited.clear()
                self._thread = threading.Thread(
                    target=self._loop, name="main-agent-runtime", daemon=True,
                )
                self._thread.start()

    def cancel_active(self) -> None:
        """Latch the active command and every queued one (D-9: /stop, /clear)."""
        with self._state_lock:
            active = self._active
            pending = list(self._pending_handles)
        for handle in pending:
            handle._stop_latched = True
        if active is not None:
            active._stop_latched = True
        try:
            from agent.runner import request_stop
            request_stop()
        except Exception:
            pass

    @classmethod
    def stop_instance(cls) -> None:
        """Shut the singleton down (D-7: call from a worker thread, never the main loop).

        Cancel first, then wait for the active command to finish on its own — its
        on_complete persists the reply. Only a command that outlives the budget is
        force-finished, and force-finish never calls on_complete (D-11 exception:
        _execute step 6 calls it later by itself).
        """
        runtime = cls._instance
        if runtime is None:
            return
        with runtime._state_lock:
            thread = runtime._thread
            active = runtime._active
            pending = len(runtime._pending_handles)
        if thread is None and active is None and pending == 0:
            return
        runtime.cancel_active()
        if active is not None and not active._done.wait(300.0):
            logger.error("[MainRuntime] active command outlived the stop budget: %s", active.id)
        runtime.stop(timeout=5.0)
        if active is not None and not active._done.is_set():
            try:
                active._emit({"type": "cancelled", "id": active.id, "error": "cancelled"})
            except Exception:
                pass
            active._finish(None, "cancelled")

    def stop(self, timeout: float = 5.0) -> bool:
        with self._state_lock:
            self._running = False
            active = self._active
        if active is not None:
            active._stop_latched = True
            try:
                from agent.runner import request_stop
                request_stop()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._queue.put(_STOP)
            thread.join(timeout)
            joined = not thread.is_alive()
            if joined:
                with self._state_lock:
                    if self._thread is thread:
                        self._thread = None
            else:
                if active is not None:
                    try:
                        active._emit({"type": "cancelled", "id": active.id, "error": "cancelled"})
                    except Exception:
                        pass
                    active._finish(None, "cancelled")
                    with self._state_lock:
                        if self._active is active:
                            self._active = None
                self._drain_after_stop(requeue_stop=True)
            return joined
        else:
            with self._state_lock:
                if self._thread is thread:
                    self._thread = None
            if active is not None:
                self._finish_unexecuted(active)
            self._drain_after_stop()
            return True

    def _drain_after_stop(self, requeue_stop: bool = False, call_on_complete: bool = True) -> None:
        saw_stop = False
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _STOP:
                saw_stop = True
                continue
            _, handle = item
            with self._state_lock:
                self._pending_handles.discard(handle)
            self._finish_unexecuted(handle, call_on_complete=call_on_complete)
        if requeue_stop and saw_stop:
            self._queue.put(_STOP)

    def _finish_unexecuted(
        self, handle: RunHandle, error: str = "runtime stopped", call_on_complete: bool = True,
    ) -> None:
        """§4.1 step 7: a command that never ran still yields a neutral outcome so the
        submitter can degrade, then the error event and the finish.

        call_on_complete is False only for the start()-side drain: on_complete must
        not run on the caller's thread (D-5 — it bridges back to the main loop).
        """
        if call_on_complete:
            self._call_on_complete(handle, RunOutcome(error=RuntimeError(error), neutral_degrade=True))
        handle._emit({"type": "error", "id": handle.id, "error": error})
        handle._finish(None, error)

    @staticmethod
    def _call_on_complete(handle: RunHandle, outcome: RunOutcome) -> None:
        """D-5: exactly once, on the runtime thread, before the handle finishes."""
        callback = handle.command.on_complete
        if callback is None or handle._on_complete_called:
            return
        handle._on_complete_called = True
        try:
            callback(outcome)
        except Exception:
            logger.exception("[MainRuntime] on_complete failed for %s", handle.id)

    def is_busy(self) -> bool:
        with self._state_lock:
            thread_alive = self._thread is not None and self._thread.is_alive()
            return self._active is not None or len(self._pending_handles) > 0 or (
                thread_alive and not self._running
            )

    def is_round_active(self) -> bool:
        """I-6: 回合真在跑——active 命令，或 `_chat_lock` 被持有（compat /compact 等）。

        pending 不算：排队中的命令还没开始，`/chat/session` 必须走正常 FIFO 提交，
        而不是把 supplement 直接入队。`_chat_lock` 惰性 import；取不到时只回 active。
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

    def submit(self, command: RunCommand) -> RunHandle:
        handle = RunHandle(command)
        handle._runtime = self
        with self._state_lock:
            if not self._running:
                raise RuntimeError("main runtime is stopped")
            self._pending_handles.add(handle)
            self._queue.put((command, handle))
        return handle

    def _loop(self) -> None:
        restarts = 0
        try:
            while True:
                try:
                    self._loop_body()
                    return
                except BaseException:
                    restarts += 1
                    logger.exception("[MainRuntime] loop body crashed; restarting")
                    with self._state_lock:
                        crashed = self._active
                        self._active = None
                    if crashed is not None:
                        self._finish_unexecuted(crashed, error="runtime error")
                    if restarts > 3:
                        with self._state_lock:
                            self._running = False
                        self._drain_after_stop()
                        return
                    time.sleep(0.1)
        finally:
            self._loop_exited.set()
            with self._state_lock:
                self._running = False
            self._drain_after_stop()
            with self._state_lock:
                active = self._active
                self._active = None
                if self._thread is threading.current_thread():
                    self._thread = None
            if active is not None:
                self._finish_unexecuted(active, error="runtime exited")

    def _loop_body(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._drain_after_stop()
                return
            command, handle = item
            with self._state_lock:
                self._pending_handles.discard(handle)
                running = self._running
                if running:
                    self._active = handle
            if not running:
                self._finish_unexecuted(handle)
                continue
            try:
                outcome = self._execute(command, handle)
                if outcome is None:  # defensive: _execute always yields an outcome
                    outcome = RunOutcome(
                        error=RuntimeError("runtime command failed"), neutral_degrade=True,
                    )
                self._finish_handle(handle, outcome)
            except BaseException as exc:
                # only a crash of _execute itself can land here
                logger.exception("[MainRuntime] command failed")
                handle._set_result(None)
                if handle._stop_latched:
                    handle._set_error("cancelled")
                    try:
                        handle._emit({"type": "cancelled", "id": handle.id, "error": "cancelled"})
                    except Exception:
                        logger.exception("[MainRuntime] cancelled event emit failed")
                else:
                    handle._set_error(str(exc))
                    try:
                        handle._emit({"type": "error", "id": handle.id, "error": str(exc)})
                    except Exception:
                        logger.exception("[MainRuntime] error event emit failed")
            finally:
                with self._state_lock:
                    if self._active is handle:
                        self._active = None
                handle._finish(handle.result, handle.error)

    @staticmethod
    def _finish_handle(handle: RunHandle, outcome: RunOutcome) -> None:
        """Terminal handle state/event is written from the outcome (§4.1 step 7)."""
        if outcome.cancelled:
            handle._set_result(None)
            handle._set_error("cancelled")
            try:
                handle._emit({"type": "cancelled", "id": handle.id, "error": "cancelled"})
            except Exception:
                logger.exception("[MainRuntime] cancelled event emit failed")
        elif outcome.error is not None:
            handle._set_result(None)
            handle._set_error(str(outcome.error))
            try:
                handle._emit({"type": "error", "id": handle.id, "error": str(outcome.error)})
            except Exception:
                logger.exception("[MainRuntime] error event emit failed")
        else:
            handle._set_result(outcome.result)
            try:
                handle._emit({"type": "done", "id": handle.id})
            except Exception:
                logger.exception("[MainRuntime] done event emit failed")

    def _execute(self, command: RunCommand, handle: RunHandle) -> RunOutcome:
        """Run one command while holding _chat_lock; return its RunOutcome (D-11).

        §4.1 steps 1-7: lock -> latch check -> clear_stop/state/history/runner.chat
        -> outcome -> on_complete (inside the lock) -> release -> drain_supplements.
        """
        from agent.runner import clear_stop, is_stop_requested

        runner = self._runner
        loop: Any = None
        acquired = False
        outcome: RunOutcome | None = None
        prev_source: str | None = None
        gen: Any = None
        history: list | None = None
        chunks: list[str] = []
        advanced = False  # a reply/str chunk appeared
        pumped = False    # the generator was advanced at least once
        stop_consumed = False

        try:
            # 2. runner + chat lock (_acquire_chat_lock never raises)
            if runner is None:
                from niu_api.chat import get_or_create_runner
                runner = get_or_create_runner()
                self._runner = runner
            try:
                loop = self._get_main_loop()
            except BaseException as exc:
                # D-11: loop lookup failure must also land in outcome + on_complete
                loop = None
                acquired, lock_exc = False, exc
            else:
                acquired, lock_exc = self._acquire_chat_lock(loop)
            if not acquired:
                # lock failure keeps the timeout classification (neutral_degrade is
                # only for drain/submit/never-executed) and skips steps 3-5
                outcome = RunOutcome(error=lock_exc)
            elif handle._stop_latched:
                # 3. D-9: only the per-command latch early-exits; a residual global
                # stop flag is not a reason to skip (D-8 clears it after the lock)
                outcome = RunOutcome(cancelled=True)
            else:
                # 4. clear the residual flag, take state ownership, read history, run
                clear_stop()
                prev_source = self._set_command_state(command, runner)
                try:
                    history = self._load_history_sync(command.exclude_message_ids)
                    gen = runner.chat(
                        command.session_id, command.content, stream=command.stream,
                        history=history, resources=command.resources,
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
                        if handle._stop_latched or is_stop_requested():
                            # keep consuming until _chat_impl exits on its own so this
                            # round's fresh last_return_value/_persisted_msgs survive
                            stop_consumed = True
                            from agent.runner import request_stop
                            request_stop()
                except BaseException as exc:
                    outcome = RunOutcome(error=exc)

            # 5. build the outcome (skipped when step 4 already set one)
            if outcome is None:
                rv = getattr(runner, "last_return_value", None)
                if not advanced and pumped and getattr(runner, "_persisted_msgs", None):
                    advanced = True
                cancelled = (
                    handle._stop_latched
                    or stop_consumed
                    or (advanced and rv is None)
                    or (isinstance(rv, dict) and rv.get("result") == "STOPPED")
                )
                if cancelled and not advanced:
                    # generator never advanced: nothing of this round is trustworthy
                    outcome = RunOutcome(result="".join(chunks), cancelled=True)
                else:
                    outcome = RunOutcome(
                        result="".join(chunks),
                        cancelled=cancelled,
                        return_value=rv,
                        persisted_msgs=getattr(runner, "_persisted_msgs", None),
                        extracted_at_msgs=getattr(runner, "_extracted_at_msgs", None),
                    )
        finally:
            # 6. close, restore source, on_complete (inside the lock, before the
            # handle finishes), release, then drain the tail-window supplements
            if gen is not None:
                try:
                    gen.close()
                except Exception:
                    logger.exception("[MainRuntime] close generator failed")
            self._restore_command_state(runner, prev_source)
            if outcome is None:
                outcome = RunOutcome(
                    error=RuntimeError("runtime command failed"), neutral_degrade=True,
                )
            outcome.history_len = len(history or [])
            self._call_on_complete(handle, outcome)
            if acquired:
                self._release_chat_lock(loop)
            if pumped:
                try:
                    from agent.runner import drain_supplements
                    drain_supplements()
                except Exception:
                    logger.exception("[MainRuntime] drain_supplements failed")

        # 7. _loop_body writes the handle state and the terminal event
        return outcome

    @staticmethod
    def _get_main_loop():
        """D-4/D-3: always read niu_api.chat._main_loop, never a cached copy."""
        from niu_api.chat import _main_loop
        return _main_loop

    def _acquire_chat_lock(self, loop) -> tuple[bool, BaseException | None]:
        """D-4: take _chat_lock on the main loop from the runtime thread.

        Returns (acquired, error); never raises. The coroutine and this thread share
        one holder, so a coroutine that gets the lock after this thread gave up
        releases it itself: the coroutine's answer is the only verdict and no orphan
        waiter survives. Depends on the Python 3.11 asyncio.Lock.acquire cancel
        semantics (D-4 note).
        """
        from niu_api.compat import _chat_lock

        if loop is None or loop.is_closed():
            return False, RuntimeError("main event loop unavailable for _chat_lock")

        holder = {"acquired": False, "abandoned": False}

        async def _acquire() -> bool:
            try:
                await asyncio.wait_for(_chat_lock.acquire(), timeout=_LOCK_ACQUIRE_TIMEOUT)
            except TimeoutError:
                return False
            holder["acquired"] = True  # store-then-load: no TOCTOU against abandoned
            if holder["abandoned"]:
                holder["acquired"] = False
                try:
                    _chat_lock.release()
                except RuntimeError:
                    pass
                return False
            return True

        try:
            future = asyncio.run_coroutine_threadsafe(_acquire(), loop)
        except RuntimeError as exc:  # loop closed between the check and the submit
            return False, exc

        def _on_done(fut) -> None:
            # Only the abandoned path may be released here; a normal success stays
            # owned by this thread until _release_chat_lock.
            if not holder["abandoned"]:
                return
            try:
                if fut.cancelled() or fut.exception() is not None:
                    return
                if fut.result() is True:
                    loop.call_soon_threadsafe(_chat_lock.release)
            except Exception as exc:
                logger.warning("[MainRuntime] orphan chat-lock release failed: %s", exc)

        future.add_done_callback(_on_done)
        try:
            acquired = future.result(timeout=_LOCK_FUTURE_TIMEOUT)
        except TimeoutError:
            holder["abandoned"] = True
            future.cancel()
            return False, TimeoutError(f"_chat_lock acquire timeout ({_LOCK_FUTURE_TIMEOUT}s)")
        except asyncio.CancelledError:
            return False, TimeoutError("_chat_lock acquire cancelled")
        except BaseException as exc:
            return False, exc
        if acquired:
            return True, None
        # the coroutine itself reported failure (inner _LOCK_ACQUIRE_TIMEOUT fired)
        return False, TimeoutError(f"_chat_lock acquire timeout ({_LOCK_ACQUIRE_TIMEOUT}s)")

    def _release_chat_lock(self, loop) -> None:
        """D-4: clear_stop first, then release on the loop thread (P2a helper制式)."""
        from niu_api.compat import _chat_lock

        try:
            from agent.runner import clear_stop
            clear_stop()
        except Exception:
            logger.exception("[MainRuntime] clear_stop before chat-lock release failed")
        try:
            if _chat_lock.locked():
                loop.call_soon_threadsafe(_chat_lock.release)
        except RuntimeError as exc:
            logger.warning("[MainRuntime] chat-lock release dropped (loop closed): %s", exc)

    def _load_history_sync(self, exclude_ids: list[str]) -> list:
        """D-3: store -> context manager -> view, all in one bridged coroutine.

        Returns the un-transformed assemble_view_sync view (runner.chat/agent_loop
        transform exactly once — a second transform would truncate twice).
        Raises; _execute turns it into outcome.error and still calls on_complete.
        """
        loop = self._get_main_loop()
        if loop is None or loop.is_closed():
            raise RuntimeError("main event loop unavailable for history load")
        ids = set(exclude_ids or ())

        async def _do() -> list:
            from agent.context_manager import get_context_manager
            from agent.session import get_message_store

            store = await get_message_store()
            context_manager = await get_context_manager(store)
            return await context_manager.get_context_for_chat(
                exclude_last=False, exclude_ids=ids,
            )

        fut = asyncio.run_coroutine_threadsafe(_do(), loop)
        try:
            return fut.result(timeout=_HISTORY_LOAD_TIMEOUT)
        except TimeoutError:
            # 超时后取消桥接协程，避免主循环遗留孤儿协程；原样抛出交给 _execute
            fut.cancel()
            raise

    @staticmethod
    def _set_command_state(command: RunCommand, runner) -> str:
        """D-10: channel/source ownership, taken inside _chat_lock. Returns prev source."""
        prev_source = getattr(runner, "_request_source", "user")
        set_channel = getattr(runner, "set_im_channel", None)
        set_force = getattr(runner, "set_im_force", None)
        if command.channel == "electron":
            # Electron user message clears the IM flags (rule 2)
            if set_channel is not None:
                set_channel("")
            if set_force is not None:
                set_force(False)
        elif command.channel == "im":
            if set_channel is not None:
                set_channel(command.channel_id)
        elif command.channel == "inherit":
            # D-6: compat /chat/session 的程序化回填（source==""）——通道继承：
            # 既不清 channel/force（Electron 语义），也不置 force（后台触发语义）
            pass
        else:
            # scheduler / ha-watcher and other background triggers arm the IM force flag
            if set_force is not None:
                set_force(True)
        runner._request_source = (
            "scheduler" if command.source in ("scheduler", "ha-watcher") else "user"
        )
        return prev_source

    @staticmethod
    def _restore_command_state(runner, prev_source: str | None) -> None:
        """D-10: restore only when state was actually taken; set_im_* stays sticky."""
        if prev_source is None or runner is None:
            return
        runner._request_source = prev_source


def get_main_runtime() -> MainAgentRuntime | None:
    """D-8: 读现有单例，绝不创建（端点用它取 runtime；测试 monkeypatch 本函数注入 fake）。

    没有单例时返回 None——端点按现有 busy/中性文案处理，不得顺手 `instance()` 拉起线程。
    """
    return MainAgentRuntime._instance


def is_main_agent_busy() -> bool:
    """D-8: the single busy signal (runtime singleton exists and is busy).

    Callers import it lazily inside the function; scheduler/db_monitor/chat status
    combine it with `_chat_lock.locked()`.
    """
    runtime = get_main_runtime()
    if runtime is None:
        return False
    return runtime.is_busy()
