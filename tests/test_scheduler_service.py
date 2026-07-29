"""Tests for service.py trigger_callback — ChatQueue-based implementation"""
from unittest.mock import MagicMock, patch


class TestTriggerCallback:
    def test_main_loop_not_available_returns_none(self):
        """_main_loop 未就绪时返回 None"""
        from niu_api.internal.scheduler.service import trigger_callback

        task = {"content": "test task"}

        with patch("niu_api.chat._main_loop", None):
            result = trigger_callback(task)
            assert result is None

    def test_main_loop_closed_returns_none(self):
        """_main_loop 已关闭时返回 None"""
        from niu_api.internal.scheduler.service import trigger_callback

        task = {"content": "test task"}
        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = True

        with patch("niu_api.chat._main_loop", mock_loop):
            result = trigger_callback(task)
            assert result is None

    def test_agent_reply_success(self):
        """ChatQueue 正常返回时返回回复内容"""
        from niu_api.internal.scheduler.service import trigger_callback

        task = {"content": "test task"}
        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = False

        mock_queue = MagicMock()
        mock_future = MagicMock()
        mock_future.result.return_value = "Agent replied"
        mock_future.timeout = 300

        with patch("niu_api.chat._main_loop", mock_loop), \
             patch("niu_api.chat_queue.get_chat_queue", return_value=mock_queue), \
             patch("niu_api.internal.scheduler.service.asyncio") as mock_asyncio, \
             patch("niu_api.alerts.add_pending_alert"):

            mock_asyncio.run_coroutine_threadsafe.return_value = mock_future
            result = trigger_callback(task)
            assert result == "Agent replied"

    def test_agent_empty_reply_returns_none(self):
        """ChatQueue 返回空回复时返回 None"""
        from niu_api.internal.scheduler.service import trigger_callback

        task = {"content": "test task"}
        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = False

        mock_queue = MagicMock()
        mock_future = MagicMock()
        mock_future.result.return_value = ""

        with patch("niu_api.chat._main_loop", mock_loop), \
             patch("niu_api.chat_queue.get_chat_queue", return_value=mock_queue), \
             patch("niu_api.internal.scheduler.service.asyncio") as mock_asyncio, \
             patch("niu_api.alerts.add_pending_alert"):

            mock_asyncio.run_coroutine_threadsafe.return_value = mock_future
            result = trigger_callback(task)
            assert result is None

    def test_chatqueue_exception_returns_none(self):
        """ChatQueue 调用异常时返回 None"""
        from niu_api.internal.scheduler.service import trigger_callback

        task = {"content": "test task"}
        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = False

        with patch("niu_api.chat._main_loop", mock_loop), \
             patch("niu_api.chat_queue.get_chat_queue", side_effect=Exception("queue error")):

            result = trigger_callback(task)
            assert result is None
