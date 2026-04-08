"""任务存储"""
import sqlite3
import uuid
from typing import List, Dict, Any, Optional


class TaskStore:
    """任务存储"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
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

    def create_task(
        self,
        content: str,
        scheduled_at: str,
        event_type: str = "reminder",
        is_recurring: bool = False,
        cron_expr: Optional[str] = None
    ) -> str:
        """创建任务"""
        task_id = str(uuid.uuid4())

        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            INSERT INTO scheduled_tasks
            (id, content, scheduled_at, is_recurring, cron_expr, event_type, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """, (task_id, content, scheduled_at, int(is_recurring), cron_expr, event_type))
        conn.commit()
        conn.close()

        return task_id

    def list_tasks(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """查询任务列表"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()

        if status:
            cursor.execute("""
                SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type, status, created_at
                FROM scheduled_tasks
                WHERE status = ?
                ORDER BY scheduled_at
            """, (status,))
        else:
            cursor.execute("""
                SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type, status, created_at
                FROM scheduled_tasks
                ORDER BY scheduled_at
            """)

        rows = cursor.fetchall()
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
                "created_at": row[7]
            }
            for row in rows
        ]

    def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE scheduled_tasks
            SET status = 'cancelled'
            WHERE id = ? AND status = 'pending'
        """, (task_id,))
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    def update_task(
        self,
        task_id: str,
        content: Optional[str] = None,
        scheduled_at: Optional[str] = None,
        cron_expr: Optional[str] = None
    ) -> bool:
        """更新任务"""
        updates = []
        params = []

        if content:
            updates.append("content = ?")
            params.append(content)

        if scheduled_at:
            updates.append("scheduled_at = ?")
            params.append(scheduled_at)

        if cron_expr is not None:
            updates.append("cron_expr = ?")
            params.append(cron_expr)

        if not updates:
            return False

        params.append(task_id)

        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE scheduled_tasks
            SET {', '.join(updates)}
            WHERE id = ? AND status = 'pending'
        """, params)
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取单个任务"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, content, scheduled_at, is_recurring, cron_expr, event_type, status, created_at
            FROM scheduled_tasks
            WHERE id = ?
        """, (task_id,))
        row = cursor.fetchone()
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
            "created_at": row[7]
        }
