"""定时任务调度器"""
import threading
import time
import sqlite3
from datetime import datetime
from typing import Optional, Callable
import logging

logger = logging.getLogger(__name__)


class Scheduler:
    """定时任务调度器"""

    def __init__(self, db_path: str, trigger_callback: Callable[[dict], str]):
        """
        Args:
            db_path: 数据库路径
            trigger_callback: 触发回调函数，接收 task 字典，返回 Agent 回复
        """
        self.db_path = db_path
        self.trigger_callback = trigger_callback
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path)
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
                last_triggered_at DATETIME
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_pending
            ON scheduled_tasks(scheduled_at)
            WHERE status = 'pending'
        """)
        conn.commit()
        conn.close()

    def start(self):
        """启动调度器"""
        if self.running:
            logger.info("[SCHEDULER] Already running")
            return

        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logger.info("[SCHEDULER] Background thread started successfully")

    def start_delayed(self, delay_seconds: int = 10):
        """延迟启动调度器（等待主服务就绪）"""
        import time

        def delayed_start():
            time.sleep(delay_seconds)
            if not self.running:
                self.start()

        threading.Thread(target=delayed_start, daemon=True).start()
        logger.info(f"[SCHEDULER] Scheduled to start in {delay_seconds}s")

    def stop(self):
        """停止调度器"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("Scheduler stopped")

    def _run_loop(self):
        """主循环：每分钟检查一次"""
        while self.running:
            try:
                self.check_and_trigger()
            except Exception as e:
                logger.error(f"Scheduler error: {e}", exc_info=True)

            time.sleep(60)  # 每分钟检查一次

    def check_and_trigger(self):
        """检查并触发到期任务（public方法）"""
        now = datetime.now()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 查询到期任务
        cursor.execute("""
            SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type
            FROM scheduled_tasks
            WHERE status = 'pending' AND scheduled_at <= ?
        """, (now.isoformat(),))

        tasks = cursor.fetchall()

        for task in tasks:
            task_id, content, scheduled_at, is_recurring, cron_expr, event_type = task

            try:
                # 调用主Agent处理任务
                agent_reply = self.trigger_callback({
                    "id": task_id,
                    "content": content,
                    "event_type": event_type,
                    "scheduled_at": scheduled_at
                })

                logger.info(f"Task triggered: {task_id} - {content}, Agent replied: {agent_reply[:100]}")

            except Exception as e:
                logger.error(f"Failed to trigger task {task_id}: {e}", exc_info=True)

            # 更新任务状态
            if is_recurring:
                # 循环任务：计算下次触发时间
                next_time = self._calc_next_trigger(scheduled_at, cron_expr)
                if next_time:
                    cursor.execute("""
                        UPDATE scheduled_tasks
                        SET scheduled_at = ?, last_triggered_at = ?, triggered_at = ?
                        WHERE id = ?
                    """, (next_time.isoformat(), now.isoformat(), now.isoformat(), task_id))
                else:
                    # 无法计算下次时间，标记为已完成
                    cursor.execute("""
                        UPDATE scheduled_tasks
                        SET status = 'triggered', triggered_at = ?
                        WHERE id = ?
                    """, (now.isoformat(), task_id))
            else:
                # 单次任务：标记为已触发
                cursor.execute("""
                    UPDATE scheduled_tasks
                    SET status = 'triggered', triggered_at = ?
                    WHERE id = ?
                """, (now.isoformat(), task_id))

        conn.commit()
        conn.close()

    def _calc_next_trigger(self, scheduled_at: str, cron_expr: str) -> Optional[datetime]:
        """
        计算下次触发时间

        Args:
            scheduled_at: 当前触发时间
            cron_expr: cron 表达式

        Returns:
            下次触发时间，如果无法计算则返回 None
        """
        from .cron_parser import CronParser

        try:
            parser = CronParser(cron_expr)
            current = datetime.fromisoformat(scheduled_at)
            next_time = parser.get_next(current)
            return next_time
        except Exception as e:
            logger.error(f"Failed to calculate next trigger: {e}")
            return None
