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


class TestF4ServiceNoImPush:
    """F4: trigger_callback 不再推 IM——程序消息只写 DB 唤醒主 Agent（用户需求：
    定时提醒写 Message.DB，主 Agent 的话才由 chat_queue 特判投递 IM）"""

    def test_trigger_callback_with_chat_id_no_im_push(self):
        """带 chat_id 的任务：enqueue 写 DB 唤醒主 Agent，不再 route_out 推 IM"""
        from unittest.mock import MagicMock, patch

        from niu_api.chat_queue import EnqueueResult
        from niu_api.internal.scheduler.service import trigger_callback

        task = {
            "id": "task_123",
            "content": "群聊提醒",
            "scheduled_at": "2026-06-03T10:00:00",
            "chat_id": "oc_group123",
        }

        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = False

        mock_queue = MagicMock()
        mock_queue.enqueue_sync.return_value = EnqueueResult(queued=True, request_id="1")

        with patch("niu_api.chat._main_loop", mock_loop), \
             patch("niu_api.chat_queue.get_chat_queue", return_value=mock_queue), \
             patch("niu_api.alerts.add_pending_alert"):

            result = trigger_callback(task)

        assert result == "ok"
        # 入队内容 = [定时任务] + 任务内容，同步入队；channel 保持 "scheduler"
        # （主 Agent 回复由 chat_queue scheduler 特判经 should_push_im 闸门投递 IM——
        #  程序消息本身不再推 IM，chat_id 不参与推送）
        mock_queue.enqueue_sync.assert_called_once_with(
            content="[定时任务] 群聊提醒", channel="scheduler", source="scheduler", session_id="default"
        )

    def test_trigger_callback_without_chat_id_no_im_push(self):
        """私聊任务（无 chat_id）：同样只 enqueue 写 DB，不再推 IM"""
        from unittest.mock import MagicMock, patch

        from niu_api.chat_queue import EnqueueResult
        from niu_api.internal.scheduler.service import trigger_callback

        task = {
            "id": "task_456",
            "content": "私聊提醒",
            "scheduled_at": "2026-06-03T10:00:00",
        }

        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = False

        mock_queue = MagicMock()
        mock_queue.enqueue_sync.return_value = EnqueueResult(queued=True, request_id="1")

        with patch("niu_api.chat._main_loop", mock_loop), \
             patch("niu_api.chat_queue.get_chat_queue", return_value=mock_queue), \
             patch("niu_api.alerts.add_pending_alert"):

            result = trigger_callback(task)

        assert result == "ok"
        mock_queue.enqueue_sync.assert_called_once_with(
            content="[定时任务] 私聊提醒", channel="scheduler", source="scheduler", session_id="default"
        )

    def test_trigger_callback_enqueue_failure_returns_none(self):
        """enqueue_sync 返回 queued=False（loop 不可用）→ 返回 None，走 scheduler 失败链"""
        from unittest.mock import MagicMock, patch

        from niu_api.chat_queue import EnqueueResult
        from niu_api.internal.scheduler.service import trigger_callback

        task = {
            "id": "task_789",
            "content": "入队失败测试",
            "scheduled_at": "2026-06-03T10:00:00",
        }

        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = False

        mock_queue = MagicMock()
        mock_queue.enqueue_sync.return_value = EnqueueResult(queued=False, message="No event loop available")

        with patch("niu_api.chat._main_loop", mock_loop), \
             patch("niu_api.chat_queue.get_chat_queue", return_value=mock_queue), \
             patch("niu_api.alerts.add_pending_alert") as mock_alert:

            result = trigger_callback(task)

        assert result is None
        mock_alert.assert_not_called()  # 入队失败不推送
