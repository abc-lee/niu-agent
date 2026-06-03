"""F4: 定时推送群目标 TDD 测试"""
import os
import tempfile
import pytest
from niu_api.internal.scheduler.task_store import TaskStore


@pytest.fixture
def store():
    """创建临时数据库的 TaskStore"""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = TaskStore(db_path)
    yield s
    try:
        os.unlink(db_path)
    except Exception:
        pass


class TestF4TaskStoreChatId:
    """F4: task_store 支持 chat_id"""

    def test_create_task_with_chat_id(self, store):
        """创建任务时可以指定 chat_id"""
        task_id = store.create_task(
            content="群聊提醒",
            scheduled_at="2026-06-03T10:00:00",
            chat_id="oc_group123",
        )
        assert task_id is not None
        task = store.get_task(task_id)
        assert task is not None
        assert task["chat_id"] == "oc_group123"

    def test_create_task_without_chat_id(self, store):
        """不指定 chat_id 时默认为 None"""
        task_id = store.create_task(
            content="私聊提醒",
            scheduled_at="2026-06-03T10:00:00",
        )
        task = store.get_task(task_id)
        assert task is not None
        assert task["chat_id"] is None

    def test_overdue_tasks_include_chat_id(self, store):
        """get_overdue_tasks 返回结果包含 chat_id"""
        from datetime import datetime
        past = datetime.now().isoformat()
        task_id = store.create_task(
            content="到期任务",
            scheduled_at=past,
            chat_id="oc_group456",
        )
        tasks = store.get_overdue_tasks()
        matching = [t for t in tasks if t["id"] == task_id]
        assert len(matching) == 1
        assert matching[0]["chat_id"] == "oc_group456"

    def test_list_tasks_include_chat_id(self, store):
        """list_tasks 返回结果包含 chat_id"""
        task_id = store.create_task(
            content="列表任务",
            scheduled_at="2026-06-03T10:00:00",
            chat_id="oc_group789",
        )
        tasks = store.list_tasks()
        matching = [t for t in tasks if t["id"] == task_id]
        assert len(matching) == 1
        assert matching[0]["chat_id"] == "oc_group789"

    def test_find_task_by_name_include_chat_id(self, store):
        """find_task_by_name 返回结果包含 chat_id"""
        store.create_task(
            content="命名任务",
            scheduled_at="2026-06-03T10:00:00",
            name="test_named",
            chat_id="oc_group_named",
        )
        task = store.find_task_by_name("test_named")
        assert task is not None
        assert task["chat_id"] == "oc_group_named"
