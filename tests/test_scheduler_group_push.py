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


class TestF4ServiceChatIdPass:
    """F4: trigger_callback 传递 chat_id 到 router.push（fire-and-forget：入队即推送）"""

    def test_trigger_callback_with_chat_id(self):
        """trigger_callback 应从 task 读取 chat_id 并传给 router.route_out；入队即完成"""
        from unittest.mock import AsyncMock, MagicMock, patch

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

        mock_route_out = AsyncMock()
        mock_router = MagicMock()
        mock_router.has_channel.return_value = True
        mock_router.route_out = mock_route_out

        with patch("niu_api.chat._main_loop", mock_loop), \
             patch("niu_api.chat_queue.get_chat_queue", return_value=mock_queue), \
             patch("niu_api.chat.get_or_create_runner", return_value=None), \
             patch("niu_api.channel.get_channel_router", return_value=mock_router), \
             patch("niu_api.alerts.add_pending_alert"), \
             patch("asyncio.run_coroutine_threadsafe") as mock_rc:

            push_future = MagicMock()
            push_future.result.return_value = None
            mock_rc.side_effect = [push_future]

            result = trigger_callback(task)

        assert result == "ok"
        # 入队内容 = [定时任务] + 任务内容，同步入队；channel 必须保持 "scheduler"
        # （enqueue_sync 默认 channel="im"——若漏传，ChatQueue worker 会把 Agent 回复
        #  自动 push 到 IM（channel/gateway.py 空 channel_id 回退广播），叠加手动 route_out = 双 IM 消息）
        mock_queue.enqueue_sync.assert_called_once_with(
            content="[定时任务] 群聊提醒", channel="scheduler", source="scheduler", session_id="default"
        )
        # IM 推送内容 = prompt（任务内容），不再等 Agent 回复
        mock_route_out.assert_called_once_with("[定时任务] 群聊提醒", "im", "oc_group123")

    def test_trigger_callback_without_chat_id(self):
        """私聊任务 chat_id 为空时，route_out 传空串"""
        from unittest.mock import AsyncMock, MagicMock, patch

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

        mock_route_out = AsyncMock()
        mock_router = MagicMock()
        mock_router.has_channel.return_value = True
        mock_router.route_out = mock_route_out

        with patch("niu_api.chat._main_loop", mock_loop), \
             patch("niu_api.chat_queue.get_chat_queue", return_value=mock_queue), \
             patch("niu_api.chat.get_or_create_runner", return_value=None), \
             patch("niu_api.channel.get_channel_router", return_value=mock_router), \
             patch("niu_api.alerts.add_pending_alert"), \
             patch("asyncio.run_coroutine_threadsafe") as mock_rc:

            push_future = MagicMock()
            push_future.result.return_value = None
            mock_rc.side_effect = [push_future]

            result = trigger_callback(task)

        assert result == "ok"
        mock_queue.enqueue_sync.assert_called_once_with(
            content="[定时任务] 私聊提醒", channel="scheduler", source="scheduler", session_id="default"
        )
        # chat_id 为 None 时，task.get("chat_id") or "" 应返回 ""
        mock_route_out.assert_called_once_with("[定时任务] 私聊提醒", "im", "")

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
