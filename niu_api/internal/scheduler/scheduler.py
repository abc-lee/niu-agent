"""
Task Scheduler - Single-loop architecture

Periodically scans for due tasks and executes them via trigger_callback.
Overdue tasks are handled by the same loop: each waits for the backend idle
signal (_chat_lock.locked() polled via run_coroutine_threadsafe) with
double-confirm debounce, bounded by a max-wait timeout to prevent indefinite
blocking when the backend stays busy.
"""

import asyncio
import threading

from niu_api.chat import frontend_ready_event
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from typing import Callable, Optional, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from niu_api.internal.scheduler.task_store import TaskStore


_CALLBACK_TIMEOUT = 300  # 覆盖 service 最坏 2×120s+10s=250s + 余量；原 120s 小于 service 最坏耗时会导致外层先超时但 _chat_lock 仍被持有


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
        # 改造：错峰等待改为轮询后端非忙 + 二次确认防抖（持锁）
        self._busy_poll_interval = 2  # 轮询后端忙碌状态的间隔（秒）
        self._double_confirm_delay = 3  # 二次确认间隔：查到非忙→等3s→再查，仍非忙才执行
        self._stagger_max_wait = 600  # 错峰等待总超时上限（秒），防止后端一直忙导致永远等不到
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
        self._ready_event = threading.Event()
        # task 失败计数器：连续失败 N 次后标记 status='failed' 不再自动重试
        # 不持久化（重启清零，意味着重新尝试）
        self._task_fail_count: dict[str, int] = {}
        self._TASK_FAIL_THRESHOLD = 3
        # in_progress 任务超时阈值（小时）：超过则重置为 pending
        # 防止任务执行中崩溃或跨进程竞态导致状态卡死
        self._stale_timeout_hours = 8

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

    def signal_ready(self):
        """外部通知系统就绪（_main_loop + ChatQueue 已启动）"""
        self._ready_event.set()

    def start_delayed(self):
        """Start the scheduler after system is ready.

        Waits for signal_ready() to be called (indicating _main_loop and ChatQueue
        are operational), plus a minimum safety delay. Falls back to forced start
        after timeout to prevent indefinite blocking.
        """
        self._delayed_start_cancelled = False

        def _delayed_start():
            # Phase 1: Wait for system ready signal (180s 超时不再强行 start)
            #   原行为：超时后 warning + 继续 self.start()，会撞未就绪 runner
            #   新行为：超时后 return，scheduler 不启动，等下次程序启动重试
            timeout_seconds = 180
            signaled = self._ready_event.wait(timeout=timeout_seconds)
            if not signaled:
                logger.warning("[SCHEDULER] Ready signal not received within 180s, aborting start (will retry on next launch)")
                return

            if self._delayed_start_cancelled:
                logger.info("[SCHEDULER] Delayed start cancelled")
                return

            # Phase 2: Wait for frontend SSE subscription established
            #   前端 launch 后调 POST /api/frontend-ready 通知后端
            #   scheduler 等此事件才扫描过期任务，确保 reply 推 SSE 时前端已订阅
            #   60s 超时未收到强制继续（前端可能崩溃或未启动，不能让 scheduler 永远不工作）
            if not frontend_ready_event.wait(timeout=60):
                logger.warning("[SCHEDULER] Frontend not ready within 60s, proceeding anyway")

            # Phase 3: Minimum safety delay
            time.sleep(2)

            with self._lock:
                if self.running or self._delayed_start_cancelled:
                    return
            self.start()

        threading.Thread(target=_delayed_start, daemon=True).start()
        logger.info("[SCHEDULER] Delayed start: waiting for system_ready signal (180s timeout)")

    def cancel_delayed_start(self):
        """取消 delayed start（不 shutdown 整体 scheduler）。

        场景：启动期检测到 LightRAG 损坏（need_repair=True），
        lifespan 不调 signal_scheduler_ready，但 scheduler.start_delayed
        里的 _ready_event.wait(180) 180s 超时后会强行 start（L103-106）。
        此方法设 _delayed_start_cancelled=True，让 _delayed_start 线程
        在 180s 超时后检查到这个 flag 直接 return，不强行 start。

        与 stop() 的区别：
        - stop() 会 join 线程 + shutdown executor（重操作，整体关闭）
        - cancel_delayed_start 只设 flag，不 join 不 shutdown（轻量）

        时序约束：
        - 必须在 start_delayed() 之后调用（_delayed_start_cancelled 在
          start_delayed 开头被重置为 False，cancel 之前调会被重置覆盖）。
        - lifespan 顺序：L67 start_scheduler()（内部调 start_delayed）
          → Phase 1 检测 → 调 cancel_delayed_start，时序正确。
        """
        with self._lock:
            self._delayed_start_cancelled = True
        logger.info("[SCHEDULER] Delayed start cancelled (start_delayed will no-op on timeout)")

    def stop(self):
        """Stop the scheduler"""
        with self._lock:
            self.running = False
            self._delayed_start_cancelled = True
            self._ready_event.clear()

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
        The lock is held during stagger waits; concurrent invocations are skipped.
        """
        if not self._check_lock.acquire(blocking=False):
            logger.debug("[SCHEDULER] check_and_trigger already running, skipping")
            return

        try:
            self._check_and_trigger_impl()
        finally:
            self._check_lock.release()

    def _is_backend_busy(self) -> bool:
        """通过 run_coroutine_threadsafe 桥接读取后端 _chat_lock.locked()。

        复用项目既有桥接模式（service.py:100 等），不绕道 HTTP 自请求。
        - True：后端正在处理 chat 请求或 scheduler 任务，应等待
        - False：后端空闲，可执行下一条错过的任务
        - 主 loop 不可用或查询超时：返回 False（不阻塞调度，记 warning）

        从 scheduler 工作线程调用，桥接到主事件循环读取 asyncio.Lock 状态。
        """
        from niu_api.chat import _main_loop
        from niu_api.compat import _chat_lock

        loop = _main_loop
        if loop is None or loop.is_closed():
            logger.warning("[SCHEDULER] Main loop not available, _is_backend_busy assuming idle")
            return False

        async def _check():
            return _chat_lock.locked()

        try:
            future = asyncio.run_coroutine_threadsafe(_check(), loop)
            return future.result(timeout=3)
        except FuturesTimeoutError:
            logger.warning("[SCHEDULER] _is_backend_busy query timed out, assuming idle")
            return False
        except RuntimeError as e:
            # loop 已关闭等运行时异常，降级为不忙不阻塞调度
            logger.warning(f"[SCHEDULER] _is_backend_busy runtime error: {e}, assuming idle")
            return False

    def _interruptible_sleep(self, seconds: float) -> bool:
        """分块 sleep，每秒检查 running 标志。返回 True 表示被 stop 中断。"""
        remaining = seconds
        while remaining > 0:
            with self._lock:
                if not self.running:
                    return True
            chunk = min(remaining, 1)
            time.sleep(chunk)
            remaining -= chunk
        return False

    def _check_and_trigger_impl(self):
        """Implementation of check_and_trigger, called under _check_lock

        Holds _check_lock during stagger waits; concurrent check_and_trigger
        calls are skipped via acquire(blocking=False).
        """
        # Reset failed tasks older than 5 minutes to pending for retry
        self.store.retry_failed_tasks(retry_interval_seconds=300)
        # Reset in_progress tasks stuck longer than stale_timeout_hours (crash/cross-process safety)
        reset_count = self.store.reset_stale_in_progress(timeout_hours=self._stale_timeout_hours)
        if reset_count > 0:
            logger.warning(
                f"[SCHEDULER] Reset {reset_count} stale in_progress tasks "
                f"(exceeded {self._stale_timeout_hours}h timeout) to pending"
            )

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
            # 持锁（不 release _check_lock）：新逻辑轮询非忙快速推进，
            # 持锁避免多批次并发轮询破坏串行性（原 release 设计是为让
            # 准时任务不被固定 600s sleep 阻塞，新逻辑无需此妥协）
            if i > 0:
                stopped = False
                logger.info(
                    f"[SCHEDULER] Waiting for backend idle before next due task "
                    f"({i+1}/{len(due_tasks)})"
                )
                wait_start = time.time()
                while True:
                    with self._lock:
                        if not self.running:
                            logger.info("[SCHEDULER] Stopped during stagger wait")
                            stopped = True
                            break

                    # 总超时兜底：后端一直忙或 loop 异常时，强制执行下一条
                    if time.time() - wait_start >= self._stagger_max_wait:
                        logger.warning(
                            f"[SCHEDULER] Stagger wait exceeded {self._stagger_max_wait}s "
                            f"timeout, forcing next task"
                        )
                        break

                    # 二次确认防抖：查非忙→分块等3s（可中断）→再查，仍非忙才执行
                    # 原因：异步子 Agent 也会查这个状态抢着执行，
                    # 二次确认让两者动作错开（谁先拿到非忙谁先动，
                    # 另一个的二次确认会失败、退回等待）
                    if not self._is_backend_busy():
                        # 分块等待二次确认间隔，每秒检查 running
                        if self._interruptible_sleep(self._double_confirm_delay):
                            logger.info("[SCHEDULER] Stopped during double-confirm")
                            stopped = True
                            break
                        # 再次查后端状态
                        if not self._is_backend_busy():
                            break  # 二次确认成功，执行下一条
                        logger.debug("[SCHEDULER] Backend became busy during double-confirm, rewaiting")
                        # 分块等待，每秒检查 running（与二次确认一致的可中断性）
                        if self._interruptible_sleep(self._busy_poll_interval):
                            logger.info("[SCHEDULER] Stopped during rewait")
                            stopped = True
                            break
                        continue

                    # 后端忙，轮询等待
                    if self._interruptible_sleep(self._busy_poll_interval):
                        logger.info("[SCHEDULER] Stopped during busy poll")
                        stopped = True
                        break
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

            # CAS: pending -> in_progress, 同时记录触发时间
            now_iso = datetime.now().isoformat()
            if not self.store.update_task(task_id, status="in_progress", triggered_at=now_iso, expected_status="pending"):
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
                next_time = self._calc_next_trigger(datetime.now().isoformat(), cron_expr)

                if result is None:
                    # 失败计数器累加，达阈值才标 failed 不再自动重试
                    # 注意：pop 必须在 else（成功）分支，不能在 if 之前——
                    # 否则失败时先 pop 再 +=1，计数器永远 = 1，永远达不到阈值 3
                    self._task_fail_count[task_id] = self._task_fail_count.get(task_id, 0) + 1
                    fail_n = self._task_fail_count[task_id]

                    if fail_n >= self._TASK_FAIL_THRESHOLD:
                        logger.error(f"[SCHEDULER] Recurring task {task_id} failed {fail_n} times, marking as failed (DLQ)")
                        self.store.update_task(task_id, status="failed", expected_status="in_progress")
                        self._task_fail_count.pop(task_id, None)
                        continue

                    # 未达阈值：reschedule 到下次 cron 时间继续重试
                    if next_time:
                        logger.warning(f"[SCHEDULER] Recurring task {task_id} failed (attempt {fail_n}/{self._TASK_FAIL_THRESHOLD}), rescheduling to {next_time}")
                        self.store.update_task(task_id, scheduled_at=next_time.isoformat(), status="pending", expected_status="in_progress")
                    else:
                        # cron_expr 解析失败，标 failed 并清零计数器（避免内存泄漏）
                        self.store.update_task(task_id, status="failed", expected_status="in_progress")
                        self._task_fail_count.pop(task_id, None)
                    continue

                # 成功：清零失败计数器（在 if result is None 之后，确保失败时不会先 pop 再 +=1）
                self._task_fail_count.pop(task_id, None)
                self.store.update_last_executed_date(task_id, today)
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
                    # 一次性任务失败直接标 failed，由 retry_failed_tasks 5 分钟后重置（原行为）
                    # 不用失败计数器——retry_failed_tasks 会绕过计数器导致死循环
                    # trigger_callback 内部已重试 1 次（Task 7），所以这里失败 = 2 次真实尝试都失败
                    logger.warning(f"[SCHEDULER] One-time task {task_id} failed (trigger_callback retried already), marking as failed")
                    self.store.update_task(task_id, status="failed", expected_status="in_progress")
                    continue

                # 成功：删除任务（原行为）
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