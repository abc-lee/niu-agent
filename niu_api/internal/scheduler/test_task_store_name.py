"""测试 TaskStore name 字段"""
import os
import sqlite3
import tempfile

from niu_api.internal.scheduler.task_store import TaskStore


def test_create_task_with_name():
    """带 name 创建任务，能通过 name 查询"""
    db_path = tempfile.mktemp(suffix=".db")
    try:
        store = TaskStore(db_path)
        task_id = store.create_task(
            content="测试任务",
            scheduled_at="2026-05-18T08:00:00",
            event_type="recurring",
            is_recurring=True,
            cron_expr="0 8 * * *",
            name="daily-entity-extractor",
        )
        tasks = store.list_tasks()
        found = [t for t in tasks if t.get("name") == "daily-entity-extractor"]
        assert len(found) == 1
        assert found[0]["id"] == task_id
        assert found[0]["name"] == "daily-entity-extractor"
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_create_task_without_name():
    """不带 name 创建任务（用户手动创建），name 为 None"""
    db_path = tempfile.mktemp(suffix=".db")
    try:
        store = TaskStore(db_path)
        task_id = store.create_task(
            content="用户提醒",
            scheduled_at="2026-05-18T15:00:00",
            event_type="reminder",
        )
        task = store.get_task(task_id)
        assert task is not None
        assert task.get("name") is None
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_find_task_by_name():
    """按 name 查找任务（核心功能：替代 cron_expr 匹配）"""
    db_path = tempfile.mktemp(suffix=".db")
    try:
        store = TaskStore(db_path)
        store.create_task(
            content="提取实体",
            scheduled_at="2026-05-18T08:00:00",
            event_type="recurring",
            is_recurring=True,
            cron_expr="0 9 * * *",
            name="daily-entity-extractor",
        )
        found = store.find_task_by_name("daily-entity-extractor")
        assert found is not None
        assert found["cron_expr"] == "0 9 * * *"
        assert found["name"] == "daily-entity-extractor"
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_find_task_by_name_not_found():
    """name 不存在时返回 None"""
    db_path = tempfile.mktemp(suffix=".db")
    try:
        store = TaskStore(db_path)
        found = store.find_task_by_name("nonexistent")
        assert found is None
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_find_task_by_name_ignores_cancelled():
    """find_task_by_name 忽略已取消的任务"""
    db_path = tempfile.mktemp(suffix=".db")
    try:
        store = TaskStore(db_path)
        task_id = store.create_task(
            content="已取消的任务",
            scheduled_at="2026-05-18T08:00:00",
            event_type="recurring",
            is_recurring=True,
            cron_expr="0 8 * * *",
            name="cancelled-task",
        )
        store.cancel_task(task_id)
        found = store.find_task_by_name("cancelled-task")
        assert found is None
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_name_migration_from_old_db():
    """旧数据库（无 name 列）迁移后 name 列存在且为 None"""
    db_path = tempfile.mktemp(suffix=".db")
    try:
        # 先创建旧格式数据库（无 name 列）
        conn = sqlite3.connect(db_path)
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
        conn.execute("INSERT INTO scheduled_tasks (id, content, scheduled_at, status) VALUES (?, ?, ?, 'pending')",
                     ("old-task-id", "旧任务", "2026-05-18T08:00:00"))
        conn.commit()
        conn.close()

        # TaskStore 初始化会自动迁移
        store = TaskStore(db_path)
        task = store.get_task("old-task-id")
        assert task is not None
        assert task.get("name") is None
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
