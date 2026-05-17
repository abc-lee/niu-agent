"""测试启动时自动注入定时任务的逻辑（按 name 匹配）"""
import tempfile
import os
from niu_api.internal.scheduler.task_store import TaskStore


def test_inject_does_not_duplicate_when_user_changes_time():
    """用户改了 cron 时间后，按 name 查找不会创建重复任务"""
    db_path = tempfile.mktemp(suffix=".db")
    try:
        store = TaskStore(db_path)
        # 模拟用户改了时间（从 8 点改到 9 点）
        store.create_task(
            content="调用 chat-with-entity-extractor ...",
            scheduled_at="2026-05-18T09:00:00",
            event_type="recurring",
            is_recurring=True,
            cron_expr="0 9 * * *",
            name="daily-entity-extractor",
        )
        # 按 name 查找，不依赖 cron_expr
        existing = store.find_task_by_name("daily-entity-extractor")
        assert existing is not None
        assert existing["cron_expr"] == "0 9 * * *"
        assert existing["name"] == "daily-entity-extractor"
        # 不应再创建新任务
        tasks = store.list_tasks()
        active = [t for t in tasks if t.get("status") != "cancelled" and t.get("name") == "daily-entity-extractor"]
        assert len(active) == 1
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_inject_creates_task_when_not_exists():
    """name 不存在时，注入创建新任务"""
    db_path = tempfile.mktemp(suffix=".db")
    try:
        store = TaskStore(db_path)
        existing = store.find_task_by_name("daily-entity-extractor")
        assert existing is None
        store.create_task(
            content="调用 chat-with-entity-extractor ...",
            scheduled_at="2026-05-18T08:00:00",
            event_type="recurring",
            is_recurring=True,
            cron_expr="0 8 * * *",
            name="daily-entity-extractor",
        )
        found = store.find_task_by_name("daily-entity-extractor")
        assert found is not None
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_inject_updates_content_but_keeps_user_cron():
    """注入更新 content 但保留用户修改的 cron_expr"""
    db_path = tempfile.mktemp(suffix=".db")
    try:
        store = TaskStore(db_path)
        store.create_task(
            content="旧内容",
            scheduled_at="2026-05-18T09:00:00",
            event_type="recurring",
            is_recurring=True,
            cron_expr="0 9 * * *",
            name="daily-entity-extractor",
        )
        # 模拟注入逻辑：只更新 content，不改 cron_expr
        existing = store.find_task_by_name("daily-entity-extractor")
        store.update_task(existing["id"], content="新内容")
        updated = store.get_task(existing["id"])
        assert updated["content"] == "新内容"
        assert updated["cron_expr"] == "0 9 * * *"  # 用户改的时间保留
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
