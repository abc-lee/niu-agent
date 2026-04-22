"""定时任务调度器"""
import logging
import threading
import time
import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Callable

if TYPE_CHECKING:
    from .task_store import TaskStore

logger = logging.getLogger(__name__)


class Scheduler:
    """定时任务调度器"""

    def __init__(self, db_path: str, trigger_callback: Callable[[dict], str], store: "TaskStore"):
        """
        Args:
            db_path: 数据库路径
            trigger_callback: 触发回调函数，接收 task 字典，返回 Agent 回复
            store: TaskStore 实例（用于过期任务处理）
        """
        self.db_path = db_path
        self.trigger_callback = trigger_callback
        self.store = store
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self._delayed_thread: Optional[threading.Thread] = None
        self._overdue_thread: Optional[threading.Thread] = None
        # 保护 running 标志和任务查询/标记操作
        self._lock = threading.RLock()
        # trigger_callback HTTP 调用可能很慢，用单独的线程池加超时保护
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="scheduler_cb_")
        self._init_db()

    def __repr__(self) -> str:
        return f"Scheduler(db_path={self.db_path!r}, running={self.running})"

    def _init_db(self):
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        recovered = 0
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    scheduled_at DATETIME NOT NULL,
                    is_recurring INTEGER DEFAULT 0,
                    cron_expr TEXT,
                    event_type TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    triggered_at DATETIME,
                    last_triggered_at DATETIME,
                    last_executed_date TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_pending
                ON scheduled_tasks(scheduled_at)
                WHERE status = 'pending'
            """)
            # 迁移：老数据库可能没有 last_executed_date 列
            try:
                conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN last_executed_date TEXT")
            except sqlite3.OperationalError:
                pass  # 列已存在
            # 恢复崩溃遗留的 in_progress 任务为 pending
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE scheduled_tasks SET status = 'pending'
                WHERE status = 'in_progress'
            """)
            recovered = cursor.rowcount
            conn.commit()
        finally:
            conn.close()

        if recovered > 0:
            logger.info(f"[SCHEDULER] Recovered {recovered} orphaned in_progress tasks")

    def _cleanup_old_tasks(self):
        """清理老旧任务：删除100天前的已完成/已取消/已失败任务"""
        from datetime import timedelta

        cleanup_threshold = timedelta(days=100)
        cutoff_date = datetime.now() - cleanup_threshold

        conn = sqlite3.connect(self.db_path, timeout=10.0)
        deleted_count = 0
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM scheduled_tasks
                WHERE status IN ('completed', 'cancelled', 'failed')
                AND datetime(created_at) < datetime(?)
            """, (cutoff_date.isoformat(),))
            deleted_count = cursor.rowcount
            conn.commit()
        finally:
            conn.close()

        if deleted_count > 0:
            logger.info(f"[SCHEDULER] Cleaned up {deleted_count} old tasks (older than 100 days)")

    def start(self):
        """启动调度器"""
        with self._lock:
            if self.running:
                logger.info("[SCHEDULER] Already running")
                return
            self.running = True
            self._cleanup_old_tasks()
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
        logger.info("[SCHEDULER] Background thread started successfully")

    def start_delayed(self, delay_seconds: int = 10):
        """延迟启动调度器（等待主服务就绪）"""
        with self._lock:
            if self.running:
                logger.info("[SCHEDULER] Already running, skip start_delayed")
                return
            self._delayed_thread = threading.Thread(
                target=self._delayed_start_inner, args=(delay_seconds,), daemon=True
            )
            self._delayed_thread.start()
        logger.info(f"[SCHEDULER] Scheduled to start in {delay_seconds}s, overdue scan in 3m")

    def _delayed_start_inner(self, delay_seconds: int):
        """start_delayed 的内部逻辑"""
        time.sleep(delay_seconds)
        with self._lock:
            if self.running:
                logger.info("[SCHEDULER] Already started via start(), skipping delayed start")
                return
            self.running = True
            self._cleanup_old_tasks()
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
            logger.info("[SCHEDULER] Background thread started (delayed)")
        self._handle_overdue_tasks_delayed(delay_minutes=3)

    def stop(self):
        """停止调度器"""
        with self._lock:
            self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        if self._delayed_thread:
            self._delayed_thread.join(timeout=5)
        if self._overdue_thread:
            self._overdue_thread.join(timeout=5)
        self._executor.shutdown(wait=False)
        logger.info("Scheduler stopped")

    def _run_loop(self):
        """主循环：每10秒检查一次（快速响应 stop）"""
        last_overdue_scan = 0  # 上次过期扫描的时间戳
        overdue_interval = 30 * 60  # 30分钟扫描一次过期任务

        while True:
            with self._lock:
                if not self.running:
                    break
            try:
                self.check_and_trigger()

                # 周期性扫描过期任务
                now_ts = time.time()
                if now_ts - last_overdue_scan >= overdue_interval:
                    last_overdue_scan = now_ts
                    try:
                        self._handle_overdue_tasks()
                    except Exception as e:
                        logger.error(f"[SCHEDULER] Periodic overdue scan error: {e}", exc_info=True)

            except Exception as e:
                logger.error(f"Scheduler error: {e}", exc_info=True)
            # 每10秒检查一次 running 标志，确保 stop() 后最多等 10 秒
            for _ in range(6):
                with self._lock:
                    if not self.running:
                        return
                time.sleep(10)

    def check_and_trigger(self):
        """检查并触发到期任务（5分钟窗口内）"""
        from datetime import timedelta, date

        now = datetime.now()
        max_delay = timedelta(minutes=5)
        earliest_time = now - max_delay
        today = date.today().isoformat()

        # 1. 锁内：查询 + CAS 标记为 in_progress
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type
                    FROM scheduled_tasks
                    WHERE status = 'pending' AND scheduled_at <= ? AND scheduled_at >= ?
                    LIMIT 100
                """, (now.isoformat(), earliest_time.isoformat()))

                tasks_to_fire = []
                for row in cursor:
                    task_id = row[0]
                    # CAS: pending -> in_progress，防止其他线程重复触发
                    if self.store.update_task(task_id, status="in_progress", expected_status="pending"):
                        tasks_to_fire.append(row)
            finally:
                conn.close()

        # 2. 锁外：执行回调（不持锁，不阻塞 stop/overdue）
        for task_row in tasks_to_fire:
            task_id, content, scheduled_at, is_recurring, cron_expr, event_type = task_row

            # CAS 后重新读取最新状态（防止与 _handle_overdue_tasks 竞态）
            fresh_task = self.store.get_task(task_id)
            if not fresh_task or fresh_task["status"] != "in_progress":
                continue  # 已被其他路径处理
            scheduled_at = fresh_task["scheduled_at"]

            task_dict = {
                "id": task_id,
                "content": content,
                "event_type": event_type,
                "scheduled_at": scheduled_at,
                "is_recurring": bool(is_recurring)
            }

            agent_reply = self._call_trigger_callback(task_dict)
            if agent_reply is None:
                logger.error(f"Failed to trigger task {task_id}: timeout or error")
                self.store.update_task(task_id, status="failed", expected_status="in_progress")
                continue
            logger.info(f"Task triggered: {task_id} - {content}, Agent replied: {agent_reply[:100]}")

            if is_recurring:
                # 检查当天是否已执行（防止崩溃恢复后重复触发）
                last_executed = fresh_task.get("last_executed_date")
                if last_executed == today:
                    next_time = self._calc_next_trigger(scheduled_at, cron_expr)
                    if next_time:
                        self.store.update_task(task_id, scheduled_at=next_time.isoformat(), status="pending", expected_status="in_progress")
                    else:
                        self.store.update_task(task_id, status="failed", expected_status="in_progress")
                    continue

                self.store.update_last_executed_date(task_id, today)
                next_time = self._calc_next_trigger(scheduled_at, cron_expr)
                if next_time:
                    self.store.update_task(task_id, scheduled_at=next_time.isoformat(), status="pending", expected_status="in_progress")
                else:
                    logger.warning(f"[SCHEDULER] Cannot calculate next trigger for {task_id}, marking failed")
                    self.store.update_task(task_id, status="failed", expected_status="in_progress")
            else:
                self.store.delete_task_permanent(task_id)

    def _call_trigger_callback(self, task: dict) -> Optional[str]:
        """带超时的 trigger_callback 包装（60秒超时）"""
        try:
            future = self._executor.submit(self.trigger_callback, task)
            return future.result(timeout=60)
        except FuturesTimeoutError:
            logger.error(f"trigger_callback timed out after 60s for task {task.get('id')}")
            return None
        except Exception as e:
            logger.error(f"trigger_callback error for task {task.get('id')}: {e}")
            return None

    def _calc_next_trigger(self, scheduled_at: str, cron_expr: str) -> Optional[datetime]:
        """计算下次触发时间"""
        from .cron_parser import CronParser

        try:
            parser = CronParser(cron_expr)
            current = datetime.fromisoformat(scheduled_at)
            next_time = parser.get_next(current)
            return next_time
        except Exception as e:
            logger.error(f"Failed to calculate next trigger: {e}")
            return None

    def _handle_overdue_tasks_delayed(self, delay_minutes: int = 3):
        """延时处理过期任务（避免启动时系统太忙）"""
        def delayed_handler():
            time.sleep(delay_minutes * 60)
            with self._lock:
                if not self.running:
                    return
            logger.info("[SCHEDULER] Scanning for overdue tasks...")
            try:
                self._handle_overdue_tasks()
            except Exception as e:
                logger.error(f"[SCHEDULER] Failed to handle overdue tasks: {e}", exc_info=True)

        self._overdue_thread = threading.Thread(target=delayed_handler, daemon=True)
        self._overdue_thread.start()
        logger.info(f"[SCHEDULER] Overdue task scan scheduled in {delay_minutes} minutes")

    def _handle_overdue_tasks(self):
        """处理所有过期的待执行任务"""
        from datetime import date

        today = date.today().isoformat()

        # 1. 锁内：查询 + CAS 标记为 in_progress
        with self._lock:
            overdue_tasks = self.store.get_overdue_tasks()
            if not overdue_tasks:
                logger.info("[SCHEDULER] No overdue tasks found")
                return

            logger.info(f"[SCHEDULER] Found {len(overdue_tasks)} overdue tasks")

            tasks_to_fire = []
            for task in overdue_tasks:
                task_id = task["id"]
                # CAS: pending -> in_progress
                if self.store.update_task(task_id, status="in_progress", expected_status="pending"):
                    tasks_to_fire.append(task)

        # 2. 锁外：执行回调
        for task in tasks_to_fire:
            task_id = task["id"]
            is_recurring = task["is_recurring"]

            # CAS 后重新读取最新 scheduled_at（防止与 check_and_trigger 竞态导致双重触发）
            fresh_task = self.store.get_task(task_id)
            if not fresh_task or fresh_task["status"] != "in_progress":
                continue  # 已被其他路径处理
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
                        # 重新标记为 pending
                        self.store.update_task(task_id, status="pending", expected_status="in_progress")
                        continue
                except (ValueError, TypeError):
                    pass

                # 执行任务
                logger.info(f"[SCHEDULER] Executing overdue recurring task: {task['content'][:50]}")
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
                logger.info(f"[SCHEDULER] Executing overdue one-time task: {task['content'][:50]}")
                result = self._call_trigger_callback(task)
                if result is None:
                    self.store.update_task(task_id, status="failed", expected_status="in_progress")
                    continue
                self.store.delete_task_permanent(task_id)
