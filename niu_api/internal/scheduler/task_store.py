"""任务存储"""
import sqlite3
import uuid
from typing import List, Dict, Any, Optional


class TaskStore:
    """任务存储"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def __repr__(self) -> str:
        return f"TaskStore(db_path={self.db_path!r})"

    def _init_db(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            # 启用WAL模式，提高并发性能
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
                conn.execute("""
                    ALTER TABLE scheduled_tasks ADD COLUMN last_executed_date TEXT
                """)
            except sqlite3.OperationalError:
                pass  # 列已存在
            # 迁移：老数据库可能没有 name 列
            try:
                conn.execute("""
                    ALTER TABLE scheduled_tasks ADD COLUMN name TEXT
                """)
            except sqlite3.OperationalError:
                pass  # 列已存在
            # 迁移：老数据库可能没有 chat_id 列
            try:
                conn.execute("""
                    ALTER TABLE scheduled_tasks ADD COLUMN chat_id TEXT
                """)
            except sqlite3.OperationalError:
                pass  # 列已存在
            conn.commit()
        finally:
            conn.close()

    def create_task(
        self,
        content: str,
        scheduled_at: str,
        event_type: str = "reminder",
        is_recurring: bool = False,
        cron_expr: Optional[str] = None,
        name: Optional[str] = None,
        chat_id: Optional[str] = None
    ) -> str:
        """创建任务"""
        task_id = str(uuid.uuid4())

        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                INSERT INTO scheduled_tasks
                (id, content, scheduled_at, is_recurring, cron_expr, event_type, status, name, chat_id)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (task_id, content, scheduled_at, int(is_recurring), cron_expr, event_type, name, chat_id))
            conn.commit()
        finally:
            conn.close()

        return task_id

    def list_tasks(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """查询任务列表"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()

            if status:
                cursor.execute("""
                    SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type, status, created_at, last_executed_date, name, chat_id
                    FROM scheduled_tasks
                    WHERE status = ?
                    ORDER BY scheduled_at
                """, (status,))
            else:
                cursor.execute("""
                    SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type, status, created_at, last_executed_date, name, chat_id
                    FROM scheduled_tasks
                    ORDER BY scheduled_at
                """)

            rows = cursor.fetchall()
        finally:
            conn.close()

        return [
            {
                "id": row[0],
                "content": row[1],
                "scheduled_at": row[2],
                "is_recurring": bool(row[3]),
                "cron_expr": row[4],
                "event_type": row[5],
                "status": row[6],
                "created_at": row[7],
                "last_executed_date": row[8],
                "name": row[9],
                "chat_id": row[10]
            }
            for row in rows
        ]

    def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE scheduled_tasks
                SET status = 'cancelled'
                WHERE id = ? AND status = 'pending'
            """, (task_id,))
            affected = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return affected > 0

    def find_task_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """按 name 查找非取消状态的任务"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type, status, created_at, last_executed_date, name, chat_id
                FROM scheduled_tasks
                WHERE name = ? AND status != 'cancelled'
                LIMIT 1
            """, (name,))
            row = cursor.fetchone()
        finally:
            conn.close()

        if not row:
            return None

        return {
            "id": row[0],
            "content": row[1],
            "scheduled_at": row[2],
            "is_recurring": bool(row[3]),
            "cron_expr": row[4],
            "event_type": row[5],
            "status": row[6],
            "created_at": row[7],
            "last_executed_date": row[8],
            "name": row[9],
            "chat_id": row[10]
        }

    def update_task(
        self,
        task_id: str,
        content: Optional[str] = None,
        scheduled_at: Optional[str] = None,
        cron_expr: Optional[str] = None,
        status: Optional[str] = None,
        expected_status: Optional[str] = None,
        name: Optional[str] = None
    ) -> bool:
        """更新任务

        Args:
            expected_status: CAS 条件，仅当当前状态匹配时才更新（防止竞态）
        """
        updates = []
        params = []

        if content is not None:
            updates.append("content = ?")
            params.append(content)

        if scheduled_at is not None:
            updates.append("scheduled_at = ?")
            params.append(scheduled_at)

        if cron_expr is not None:
            updates.append("cron_expr = ?")
            params.append(cron_expr)

        if status is not None:
            updates.append("status = ?")
            params.append(status)

        if name is not None:
            updates.append("name = ?")
            params.append(name)

        if not updates:
            return False

        params.append(task_id)
        where_clause = "WHERE id = ?"
        if expected_status is not None:
            where_clause += " AND status = ?"
            params.append(expected_status)

        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()
            cursor.execute(f"""
                UPDATE scheduled_tasks
                SET {', '.join(updates)}
                {where_clause}
            """, params)
            affected = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return affected > 0

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取单个任务"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type, status, created_at, last_executed_date, name, chat_id
                FROM scheduled_tasks
                WHERE id = ?
            """, (task_id,))
            row = cursor.fetchone()
        finally:
            conn.close()

        if not row:
            return None

        return {
            "id": row[0],
            "content": row[1],
            "scheduled_at": row[2],
            "is_recurring": bool(row[3]),
            "cron_expr": row[4],
            "event_type": row[5],
            "status": row[6],
            "created_at": row[7],
            "last_executed_date": row[8],
            "name": row[9],
            "chat_id": row[10]
        }

    def delete_task_permanent(self, task_id: str) -> bool:
        """硬删除任务（用于一次性任务执行后清理）"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()
            cursor.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
            affected = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return affected > 0

    def update_last_executed_date(self, task_id: str, date_str: str) -> bool:
        """更新上次执行日期"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE scheduled_tasks
                SET last_executed_date = ?
                WHERE id = ?
            """, (date_str, task_id))
            affected = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return affected > 0

    def get_overdue_tasks(self) -> List[Dict[str, Any]]:
        """获取所有到期和过期的待执行任务（scheduled_at <= now）"""
        from datetime import datetime

        now = datetime.now().isoformat()
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type, status, created_at, last_executed_date, name, chat_id
                FROM scheduled_tasks
                WHERE status = 'pending' AND datetime(scheduled_at) <= datetime(?)
                ORDER BY scheduled_at
                LIMIT 50
            """, (now,))
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [
            {
                "id": row[0],
                "content": row[1],
                "scheduled_at": row[2],
                "is_recurring": bool(row[3]),
                "cron_expr": row[4],
                "event_type": row[5],
                "status": row[6],
                "created_at": row[7],
                "last_executed_date": row[8],
                "name": row[9],
                "chat_id": row[10]
            }
            for row in rows
        ]

    def recover_orphaned_tasks(self) -> int:
        """恢复崩溃遗留的 in_progress 任务（重置为 pending）"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        recovered = 0
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE scheduled_tasks SET status = 'pending'
                WHERE status = 'in_progress'
            """)
            recovered = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return recovered

    def cleanup_old_tasks(self, days: int = 100) -> int:
        """删除超过指定天数的已完成/取消/失败任务"""
        from datetime import datetime, timedelta

        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        deleted = 0
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM scheduled_tasks
                WHERE status IN ('completed', 'cancelled', 'failed')
                AND datetime(created_at) < datetime(?)
            """, (cutoff,))
            deleted = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return deleted
