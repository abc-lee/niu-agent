"""Chat lock handoff: hold _chat_lock until the executor worker is really done."""

import asyncio
import logging
import threading
from concurrent.futures import Future as _CFuture

logger = logging.getLogger(__name__)


def start_executor(loop, fn):
    """Run fn in the default executor; return (asyncio_future, completion).

    `asyncio_future` may be cancelled when the awaiting task is cancelled.
    `completion` is only set when the worker function truly returns/raises;
    if the queued work item is cancelled before starting, completion is
    terminated by the sf done-callback (worker will never run).
    """
    completion = _CFuture()
    started = threading.Event()

    def _runner():
        started.set()
        try:
            result = fn()
        except BaseException as exc:
            completion.set_exception(exc)
            raise
        completion.set_result(result)
        return result

    sf = loop.run_in_executor(None, _runner)

    def _on_sf_done(fut):
        if started.is_set():
            return
        if fut.cancelled():
            exc = RuntimeError("executor cancelled before start")
        else:
            try:
                exc = fut.exception()
            except Exception:
                exc = None
            if exc is None:
                exc = RuntimeError("executor finished before start")
        try:
            completion.set_exception(exc)
        except Exception:
            pass

    sf.add_done_callback(_on_sf_done)
    return sf, completion


def release_chat_lock_when_done(completion, lock, watchdog_seconds: float = 300.0) -> None:
    """Release `lock` only once `completion` is finished; keep it held otherwise.

    - completion 已完成（或为 None）：立即 clear_stop → release。
    - completion 仍 pending 且有 running loop：request_stop + done-callback 延迟释放，
      并挂 watchdog（只 request_stop，绝不 release）。
    - completion 仍 pending 且无 running loop：只 clear_stop，锁留到进程恢复/重启。
    """
    from agent.runner import clear_stop, request_stop

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if completion is not None and getattr(completion, "_niu_chat_lock_released", False):
        logger.warning("[ChatLock] release already scheduled; ignored")
        return
    if completion is not None:
        try:
            completion._niu_chat_lock_released = True
        except Exception:
            pass

    def _release():
        try:
            clear_stop()
        except Exception:
            logger.exception("[ChatLock] clear_stop failed")
        try:
            lock.release()
        except RuntimeError:
            logger.warning("[ChatLock] lock already released")
        if completion is not None:
            try:
                completion.exception()
            except Exception:
                pass

    if completion is None or completion.done():
        _release()
        return

    if loop is None:
        # 不得提前释放；只清停止标志，锁留到进程恢复/重启。
        # 该分支不可达于四条 async 路径（finally 内必有 running loop）；不 request_stop。
        try:
            clear_stop()
        except Exception:
            logger.exception("[ChatLock] clear_stop failed")
        logger.error("[ChatLock] no running loop for pending completion; lock left held")
        return

    if not completion.done():
        try:
            request_stop()
        except Exception:
            logger.exception("[ChatLock] request_stop failed")

    watchdog = None

    def _on_done(_fut):
        if watchdog is not None:
            watchdog.cancel()
        loop.call_soon_threadsafe(_release)

    completion.add_done_callback(_on_done)

    def _watchdog():
        if not completion.done():
            logger.error(
                "[ChatLock] executor still running after %.0fs; requesting stop",
                watchdog_seconds,
            )
            try:
                request_stop()
            except Exception:
                logger.exception("[ChatLock] watchdog request_stop failed")

    watchdog = loop.call_later(watchdog_seconds, _watchdog)
