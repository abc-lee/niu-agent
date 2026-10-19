"""Single-threaded main Agent runtime.

Only this runtime's dedicated thread may execute the main Agent
(`NiuRunner.chat`). All entry points submit RunCommand; commands are
processed FIFO, one at a time.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_TERMINAL = object()
_STOP = object()


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
        self._pending = 0
        self._loop_exited = threading.Event()
        self._state_lock = threading.RLock()
        self._start_lock = threading.Lock()

    @classmethod
    def instance(cls, runner=None) -> MainAgentRuntime:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(runner=runner)
            return cls._instance

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
                self._drain_after_stop()
                self._running = True
                self._loop_exited.clear()
                self._thread = threading.Thread(
                    target=self._loop, name="main-agent-runtime", daemon=True,
                )
                self._thread.start()

    def cancel_active(self) -> None:
        """Latch and stop the currently running command (P2 routes /stop here)."""
        with self._state_lock:
            active = self._active
        if active is not None:
            active._stop_latched = True
            try:
                from agent.runner import request_stop
                request_stop()
            except Exception:
                pass

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
                active._emit({"type": "error", "id": active.id, "error": "runtime stopped"})
                active._finish(None, "runtime stopped")
            self._drain_after_stop()
            return True

    def _drain_after_stop(self, requeue_stop: bool = False) -> None:
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
                self._pending = max(0, self._pending - 1)
            handle._emit({"type": "error", "id": handle.id, "error": "runtime stopped"})
            handle._finish(None, "runtime stopped")
        if requeue_stop and saw_stop:
            self._queue.put(_STOP)

    def is_busy(self) -> bool:
        with self._state_lock:
            thread_alive = self._thread is not None and self._thread.is_alive()
            return self._active is not None or self._pending > 0 or (
                thread_alive and not self._running
            )

    def submit(self, command: RunCommand) -> RunHandle:
        handle = RunHandle(command)
        handle._runtime = self
        with self._state_lock:
            if not self._running:
                raise RuntimeError("main runtime is stopped")
            self._pending += 1
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
                        crashed._emit({
                            "type": "error",
                            "id": crashed.id,
                            "error": "runtime error",
                        })
                        crashed._finish(None, "runtime error")
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
                active._emit({"type": "error", "id": active.id, "error": "runtime exited"})
                active._finish(None, "runtime exited")

    def _loop_body(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._drain_after_stop()
                return
            command, handle = item
            with self._state_lock:
                self._pending = max(0, self._pending - 1)
                running = self._running
                if running:
                    self._active = handle
            if not running:
                handle._emit({"type": "error", "id": handle.id, "error": "runtime stopped"})
                handle._finish(None, "runtime stopped")
                continue
            try:
                self._execute(command, handle)
            except BaseException as exc:
                logger.exception("[MainRuntime] command failed")
                handle._set_result(None)
                if handle._stop_latched:
                    handle._set_error("cancelled")
                    try:
                        handle._emit({"type": "cancelled", "id": handle.id})
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

    def _execute(self, command: RunCommand, handle: RunHandle) -> None:
        from agent.runner import clear_stop, is_stop_requested
        clear_stop()
        if handle._stop_latched:
            handle._set_error("cancelled")
            handle._emit({"type": "cancelled", "id": handle.id})
            return
        runner = self._runner
        if runner is None:
            from niu_api.chat import get_or_create_runner
            runner = get_or_create_runner()
            self._runner = runner
        gen = None
        chunks: list[str] = []
        ok = False
        stop_requested = False
        try:
            gen = runner.chat(
                command.session_id, command.content, stream=command.stream,
                channel_id=command.channel_id, resources=command.resources,
            )
            for chunk in gen:
                if handle._stop_latched or is_stop_requested():
                    stop_requested = True
                    break
                if chunk:
                    chunks.append(chunk)
                    handle._emit({"type": "chunk", "id": handle.id, "content": chunk})
            ok = True
        finally:
            stop_requested = stop_requested or handle._stop_latched
            if gen is not None:
                try:
                    gen.close()
                except Exception:
                    logger.exception("[MainRuntime] close generator failed")
            try:
                clear_stop()
            except Exception:
                pass
            handle._set_result("".join(chunks) if ok and not stop_requested else None)
        if stop_requested:
            handle._set_error("cancelled")
            handle._emit({"type": "cancelled", "id": handle.id})
        else:
            handle._emit({"type": "done", "id": handle.id})
