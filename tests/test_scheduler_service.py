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


class TestTaskStoreMigration:
    def test_new_db_has_task_kind_and_script_file_columns(self, tmp_path):
        """新建库自动含 task_kind/script_file 列，task_kind 默认 reminder"""
        from niu_api.internal.scheduler.task_store import TaskStore

        store = TaskStore(str(tmp_path / "test.db"))
        store.create_task(content="t", scheduled_at="2026-01-01 00:00:00")
        tasks = store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["task_kind"] == "reminder"
        assert tasks[0]["script_file"] is None

    def test_old_db_migrates_adds_columns(self, tmp_path):
        """模拟老库（无 task_kind/script_file 列）迁移后可正常读写"""
        import sqlite3
        from niu_api.internal.scheduler.task_store import TaskStore

        db_path = str(tmp_path / "old.db")
        conn = sqlite3.connect(db_path)
        # 建一个不含新列的老表
        conn.execute("""
            CREATE TABLE scheduled_tasks (
                id TEXT PRIMARY KEY, content TEXT NOT NULL,
                scheduled_at DATETIME NOT NULL, is_recurring INTEGER DEFAULT 0,
                cron_expr TEXT, event_type TEXT, status TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("INSERT INTO scheduled_tasks (id, content, scheduled_at) VALUES ('old1', 'old', '2026-01-01 00:00:00')")
        conn.commit()
        conn.close()
        # 重新初始化触发迁移
        store = TaskStore(db_path)
        tasks = store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["task_kind"] == "reminder"  # 迁移后默认值
        assert tasks[0]["script_file"] is None

    def test_create_background_script_task(self, tmp_path):
        """创建 background_script 任务存入 task_kind/script_file"""
        from niu_api.internal.scheduler.task_store import TaskStore

        store = TaskStore(str(tmp_path / "test.db"))
        store.create_task(
            content="清理临时文件", scheduled_at="2026-01-01 00:00:00",
            task_kind="background_script", script_file="clean_tmp.py",
            is_recurring=True, cron_expr="0 3 * * *",
        )
        tasks = store.list_tasks()
        assert tasks[0]["task_kind"] == "background_script"
        assert tasks[0]["script_file"] == "clean_tmp.py"
