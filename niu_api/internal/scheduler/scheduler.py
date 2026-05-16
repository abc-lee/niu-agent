"""
Task Scheduler - Single-loop architecture

Periodically scans for due tasks and executes them via trigger_callback.
Overdue tasks are handled by the same loop with stagger intervals to prevent
simultaneous execution on startup.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from typing import Callable, Optional, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from niu_api.internal.scheduler.task_store import TaskStore


_CALLBACK_TIMEOUT = 120  # 2 minutes


class Scheduler:
    def __init__(
        self,
        db_path: str,
        trigger_callback: Callable,
        store: Optional["TaskStore"] = None,
        store_factory: Optional[Callable[[], "TaskStore"]] = None,
    ):
        self.db_path = db_path
        self.trigger_callback = trigger_callback
        self.running = False
        self.thread: Optional[threading.Thread] = None
        # 过期任务顺序执行间隔（秒），防止启动时多个过期任务同时触发
        self._overdue_stagger_interval = 600  # 10 分钟
        # 保护 running 标志和任务查询/标记操作
        self._lock = threading.RLock()
        # 防止 check_and_trigger 并发执行
        self._check_lock = threading.Lock()

        # Store: 优先使用 factory（动态获取），其次使用传入的实例，最后自己创建
        if store_factory is not None:
            self._store_factory = store_factory
            self.store = store_factory()
        elif store is not None:
            self._store_factory = None
            self.store = store
        else:
            from niu_api.internal.scheduler.task_store import TaskStore
            self._store_factory = None
            self.store = TaskStore(db_path)

        # Thread pool for executing trigger callbacks (non-blocking)
        self._executor = ThreadPoolExecutor(max_workers=3)

        # Track whether delayed start has been cancelled
        self._delayed_start_cancelled = False

        # Recover orphaned in_progress tasks from crashes
        self._recover_orphaned_tasks()

    def _recover_orphaned_tasks(self):
        """Recover orphaned in_progress tasks from crashes"""
        recovered = self.store.recover_orphaned_tasks()
        if recovered > 0:
            logger.info(f"[SCHEDULER] Recovered {recovered} orphaned in_progress tasks")

    def _cleanup_old_tasks(self):
        """Delete completed/cancelled/failed tasks older than 100 days"""
        deleted = self.store.cleanup_old_tasks(days=100)
        if deleted > 0:
            logger.info(f"[SCHEDULER] Cleaned up {deleted} old tasks (older than 100 days)")

    def start(self):
        """Start the scheduler loop in a background thread"""
        with self._lock:
            if self.running:
                return
            self.running = True
            self._cleanup_old_tasks()
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
        logger.info("[SCHEDULER] Started (single-loop, 10s interval)")

    def start_delayed(self, delay_seconds: int = 10):
        """Start the scheduler after a delay (wait for main service readiness)"""
        self._delayed_start_cancelled = False

        def _delayed_start():
            remaining = delay_seconds
            while remaining > 0:
                if self._delayed_start_cancelled:
                    logger.info("[SCHEDULER] Delayed start cancelled")
                    return
                chunk = min(remaining, 1)
                time.sleep(chunk)
                remaining -= chunk
            with self._lock:
                if self.running or self._delayed_start_cancelled:
                    return
            self.start()

        threading.Thread(target=_delayed_start, daemon=True).start()
        logger.info(f"[SCHEDULER] Delayed start scheduled ({delay_seconds}s)")

    def stop(self):
        """Stop the scheduler"""
        with self._lock:
            self.running = False
            self._delayed_start_cancelled = True

        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
        self._executor.shutdown(wait=False)
        logger.info("[SCHEDULER] Stopped")

    def _run_loop(self):
        """Main scheduler loop - scans for due tasks every 10 seconds"""
        while True:
            with self._lock:
                if not self.running:
                    return

            try:
                self.check_and_trigger()
            except Exception as e:
                logger.error(f"[SCHEDULER] Error in check_and_trigger: {e}")

            # Sleep in small chunks so stop() can interrupt quickly
            for _ in range(10):
                with self._lock:
                    if not self.running:
                        return
                time.sleep(1)

    def check_and_trigger(self):
        """Scan for due tasks and execute them sequentially with stagger intervals

        Queries all tasks where scheduled_at <= now (no time window limit),
        so both on-time and overdue tasks are handled by this single path.
        Multiple overdue tasks are executed with stagger intervals to prevent
        simultaneous execution on startup.

        Protected by _check_lock to prevent concurrent invocations from _run_loop.
        The lock is released during stagger waits so new on-time tasks can be
        triggered without waiting for the entire stagger queue to complete.
        """
        if not self._check_lock.acquire(blocking=False):
            logger.debug("[SCHEDULER] check_and_trigger already running, skipping")
            return

        try:
            self._check_and_trigger_impl()
        finally:
            self._check_lock.release()

    def _check_and_trigger_impl(self):
        """Implementation of check_and_trigger, called under _check_lock

        Releases _check_lock during stagger waits so new check_and_trigger
        calls can process newly-arrived on-time tasks.
        """
        # 动态刷新 store（如果使用 factory，确保 db_path 与 workspace 一致）
        if self._store_factory is not None:
            self.store = self._store_factory()

        from datetime import date

        today = date.today().isoformat()

        # 1. 查询所有到期任务（scheduled_at <= now）
        due_tasks = self.store.get_overdue_tasks()
        if not due_tasks:
            return

        logger.debug(f"[SCHEDULER] Found {len(due_tasks)} due tasks")

        # 2. 顺序执行，每个任务之间间隔 stagger_interval
        for i, task in enumerate(due_tasks):
            task_id = task["id"]
            is_recurring = task["is_recurring"]

            # 间隔等待（第一个任务不等待）
            # 释放 _check_lock 让新任务可以被触发
            if i > 0:
                self._check_lock.release()
                stopped = False
                try:
                    logger.info(
                        f"[SCHEDULER] Waiting {self._overdue_stagger_interval}s "
                        f"before next due task ({i+1}/{len(due_tasks)})"
                    )
                    remaining = self._overdue_stagger_interval
                    while remaining > 0:
                        with self._lock:
                            if not self.running:
                                logger.info("[SCHEDULER] Stopped during stagger wait")
                                stopped = True
                                break
                        chunk = min(remaining, 10)
                        time.sleep(chunk)
                        remaining -= chunk
                finally:
                    # 重新获取 _check_lock（阻塞等待，因为可能有其他调用正在执行）
                    self._check_lock.acquire()
                if stopped:
                    return

            # Stagger 后重新检查任务是否仍然到期（可能已被并发调用 reschedule 到未来）
            fresh = self.store.get_task(task_id)
            if not fresh:
                continue
            try:
                if datetime.fromisoformat(fresh["scheduled_at"]) > datetime.now():
                    continue
            except (ValueError, TypeError):
                pass

            # CAS: pending -> in_progress
            if not self.store.update_task(task_id, status="in_progress", expected_status="pending"):
                continue

            # CAS 后重新读取最新状态（防止竞态导致双重触发）
            fresh_task = self.store.get_task(task_id)
            if not fresh_task or fresh_task["status"] != "in_progress":
                continue
            scheduled_at = fresh_task["scheduled_at"]

            if is_recurring:
                cron_expr = task.get("cron_expr")
                if not cron_expr:
                    logger.warning(f"[SCHEDULER] Recurring task {task_id} has no cron_expr, marking failed")
                    self.store.update_task(task_id, status="failed", expected_status="in_progress")
                    continue

                # 检查当天是否已执行
                last_executed = fresh_task.get("last_executed_date")
                if last_executed == today:
                    next_time = self._calc_next_trigger(datetime.now().isoformat(), cron_expr)
                    if next_time:
                        self.store.update_task(task_id, scheduled_at=next_time.isoformat(), status="pending", expected_status="in_progress")
                    else:
                        self.store.update_task(task_id, status="failed", expected_status="in_progress")
                    continue

                # 检查是否已被 reschedule 到未来
                try:
                    if datetime.fromisoformat(scheduled_at) > datetime.now():
                        self.store.update_task(task_id, status="pending", expected_status="in_progress")
                        continue
                except (ValueError, TypeError):
                    pass

                # 执行任务
                logger.info(f"[SCHEDULER] Executing recurring task ({i+1}/{len(due_tasks)}): {task['content'][:50]}")
                result = self._call_trigger_callback(task)
                if result is None:
                    self.store.update_task(task_id, status="failed", expected_status="in_progress")
                    continue

                self.store.update_last_executed_date(task_id, today)
                next_time = self._calc_next_trigger(datetime.now().isoformat(), cron_expr)
                if next_time:
                    self.store.update_task(task_id, scheduled_at=next_time.isoformat(), status="pending", expected_status="in_progress")
                else:
                    logger.warning(f"[SCHEDULER] Cannot calculate next trigger for {task_id}, marking failed")
                    self.store.update_task(task_id, status="failed", expected_status="in_progress")
            else:
                # 一次性任务：执行后删除
                logger.info(f"[SCHEDULER] Executing one-time task ({i+1}/{len(due_tasks)}): {task['content'][:50]}")
                result = self._call_trigger_callback(task)
                if result is None:
                    self.store.update_task(task_id, status="failed", expected_status="in_progress")
                    continue
                if not self.store.delete_task_permanent(task_id):
                    # 删除失败时标记为 completed，防止恢复后重复执行
                    self.store.update_task(task_id, status="completed", expected_status="in_progress")

    def _call_trigger_callback(self, task: dict) -> Optional[str]:
        """Call the trigger callback in thread pool, return result or None on failure"""
        try:
            future = self._executor.submit(self.trigger_callback, task)
            result = future.result(timeout=_CALLBACK_TIMEOUT)
            return result
        except FuturesTimeoutError:
            logger.error(f"[SCHEDULER] Trigger callback timed out for task {task['id']} ({_CALLBACK_TIMEOUT}s)")
            return None
        except Exception as e:
            logger.error(f"[SCHEDULER] Trigger callback failed for task {task['id']}: {e}")
            return None

    @staticmethod
    def _calc_next_trigger(scheduled_at: str, cron_expr: str) -> Optional[datetime]:
        """Calculate the next trigger time based on cron expression"""
        from .cron_parser import CronParser

        try:
            parser = CronParser(cron_expr)
            base = datetime.fromisoformat(scheduled_at)
            return parser.get_next(base)
        except Exception as e:
            logger.error(f"[SCHEDULER] Failed to calculate next trigger: {e}")
            return None

    # --- Convenience methods ---

    def create_task(self, **kwargs) -> str:
        return self.store.create_task(**kwargs)

    def list_tasks(self) -> list:
        return self.store.list_tasks()

    def get_task(self, task_id: str) -> Optional[dict]:
        return self.store.get_task(task_id)

    def update_task(self, task_id: str, **kwargs) -> bool:
        return self.store.update_task(task_id, **kwargs)

    def cancel_task(self, task_id: str) -> bool:
        return self.store.update_task(task_id, status="cancelled", expected_status="pending")

    def delete_task(self, task_id: str) -> bool:
        return self.store.delete_task_permanent(task_id)