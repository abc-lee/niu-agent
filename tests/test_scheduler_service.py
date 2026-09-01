"""Tests for service.py trigger_callback — ChatQueue-based implementation"""
import pytest
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

    def test_enqueue_success_returns_ok(self):
        """入队成功（queued=True）→ 返回 ok（fire-and-forget：不等 Agent 回复）"""
        from unittest.mock import MagicMock, patch

        from niu_api.chat_queue import EnqueueResult
        from niu_api.internal.scheduler.service import trigger_callback

        task = {"content": "test task"}
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
            content="[定时任务] test task", channel="scheduler", source="scheduler", session_id="default"
        )


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



class TestTriggerCallbackBackgroundScript:
    """background_script 分支测试"""

    def _make_bg_task(self, script_file="clean.py"):
        return {
            "id": "bg1", "content": "清理", "task_kind": "background_script",
            "script_file": script_file, "is_recurring": True, "cron_expr": "0 3 * * *",
        }

    def test_silent_success_no_enqueue(self, tmp_path, monkeypatch):
        """脚本 stdout 空 + exit 0 → 静默，不调 enqueue_sync"""
        from niu_api.chat_queue import EnqueueResult
        from niu_api.internal.scheduler import service

        # workspace = tmp_path, scripts/clean.py 存在
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "clean.py").write_text("import os\nprint('', end='')\n")

        # get_db_path 返回 tmp_path 下，使 workspace=tmp_path
        monkeypatch.setattr(service, "get_db_path", lambda: str(tmp_path / "scheduled_tasks.db"))
        # code_run 返回静默成功
        monkeypatch.setattr(service, "code_run", lambda *a, **kw: {"status": "success", "stdout": "", "exit_code": 0})

        enqueue_called = []
        monkeypatch.setattr(service, "get_chat_queue", lambda: type("Q", (), {
            "enqueue_sync": lambda self, **kw: enqueue_called.append(kw) or EnqueueResult(queued=True, request_id="1")
        })())

        result = service.trigger_callback(self._make_bg_task())
        assert result is not None  # 静默成功返回 truthy（调度器据此走成功路径，非 None=成功）
        assert result == "(silent)"
        assert enqueue_called == []  # 未通知

    def test_has_output_enqueues(self, tmp_path, monkeypatch):
        """脚本 stdout 非空 → enqueue_sync 注入主 Agent，返回 ok"""
        from unittest.mock import MagicMock, patch

        from niu_api.chat_queue import EnqueueResult
        from niu_api.internal.scheduler import service

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "clean.py").write_text("print('有垃圾')\n")

        monkeypatch.setattr(service, "get_db_path", lambda: str(tmp_path / "scheduled_tasks.db"))
        monkeypatch.setattr(service, "code_run", lambda *a, **kw: {"status": "success", "stdout": "有垃圾", "exit_code": 0})

        captured = {}
        mock_q = type("Q", (), {
            "enqueue_sync": lambda self, **kw: captured.update(kw) or EnqueueResult(queued=True, request_id="1")
        })()
        monkeypatch.setattr(service, "get_chat_queue", lambda: mock_q)

        with patch("niu_api.chat._main_loop", MagicMock(is_closed=lambda: False)), \
             patch("niu_api.alerts.add_pending_alert"):

            result = service.trigger_callback(self._make_bg_task())

        assert result == "ok"
        assert captured["content"].startswith("[定时任务]")
        assert "有垃圾" in captured["content"]
        assert captured["source"] == "scheduler"
        assert captured["channel"] == "scheduler"  # channel 必须保持 scheduler（防回复走广播错位——router.push 广播回退 _push_target，非消息条数）

    def test_error_enqueues_with_stderr(self, tmp_path, monkeypatch):
        """脚本异常 → stdout(含traceback) 注入主 Agent；recurring 报错返回 None 走 DLQ"""
        from unittest.mock import MagicMock, patch

        from niu_api.chat_queue import EnqueueResult
        from niu_api.internal.scheduler import service

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "clean.py").write_text("raise Exception('boom')\n")

        monkeypatch.setattr(service, "get_db_path", lambda: str(tmp_path / "scheduled_tasks.db"))
        monkeypatch.setattr(service, "code_run", lambda *a, **kw: {"status": "error", "stdout": "Traceback...boom", "exit_code": 1})

        captured = {}
        mock_q = type("Q", (), {
            "enqueue_sync": lambda self, **kw: captured.update(kw) or EnqueueResult(queued=True, request_id="1")
        })()
        monkeypatch.setattr(service, "get_chat_queue", lambda: mock_q)

        with patch("niu_api.chat._main_loop", MagicMock(is_closed=lambda: False)), \
             patch("niu_api.alerts.add_pending_alert"):

            result = service.trigger_callback(self._make_bg_task())

        assert "Traceback" in captured["content"]
        # recurring 报错：保留 3-strike DLQ（scheduler 失败计数 3 次标 failed 终态）——
        # 返回 "ok" 会让永久失败脚本每周期无限注入报错
        assert result is None

    def test_one_time_error_deletes_task(self, tmp_path, monkeypatch):
        """one-time 报错 → 已注入主 Agent + 永久删除任务（防 retry_failed 无限重置），返回 ok"""
        from unittest.mock import MagicMock, patch

        from niu_api.chat_queue import EnqueueResult
        from niu_api.internal.scheduler import service

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "clean.py").write_text("raise Exception('boom')\n")

        monkeypatch.setattr(service, "get_db_path", lambda: str(tmp_path / "scheduled_tasks.db"))
        monkeypatch.setattr(service, "code_run", lambda *a, **kw: {"status": "error", "stdout": "Traceback", "exit_code": 1})

        mock_q = type("Q", (), {
            "enqueue_sync": lambda self, **kw: EnqueueResult(queued=True, request_id="1")
        })()
        monkeypatch.setattr(service, "get_chat_queue", lambda: mock_q)

        deleted = []
        monkeypatch.setattr(service, "get_store", lambda: type("S", (), {
            "delete_task_permanent": lambda self, tid: deleted.append(tid)
        })())

        # one-time 任务（is_recurring=false）
        task = self._make_bg_task()
        task["is_recurring"] = False

        with patch("niu_api.chat._main_loop", MagicMock(is_closed=lambda: False)), \
             patch("niu_api.alerts.add_pending_alert"):

            result = service.trigger_callback(task)

        assert result == "ok"
        assert deleted == ["bg1"]  # one-time 报错永久删除（scheduler 成功路径也会删，幂等）

    def test_missing_script_file_returns_none_no_enqueue(self, tmp_path, monkeypatch):
        """脚本文件不存在 → 永久删除任务 + 返回 None，不调 code_run/enqueue"""
        from niu_api.chat_queue import EnqueueResult
        from niu_api.internal.scheduler import service

        monkeypatch.setattr(service, "get_db_path", lambda: str(tmp_path / "scheduled_tasks.db"))
        # scripts 目录存在但文件不存在
        (tmp_path / "scripts").mkdir()

        code_run_called = []
        monkeypatch.setattr(service, "code_run", lambda *a, **kw: code_run_called.append(1) or {"status": "success", "stdout": "", "exit_code": 0})

        enqueue_called = []
        monkeypatch.setattr(service, "get_chat_queue", lambda: type("Q", (), {
            "enqueue_sync": lambda self, **kw: enqueue_called.append(kw) or EnqueueResult(queued=True, request_id="1")
        })())

        deleted = []
        monkeypatch.setattr(service, "get_store", lambda: type("S", (), {
            "delete_task_permanent": lambda self, tid: deleted.append(tid)
        })())

        result = service.trigger_callback(self._make_bg_task(script_file="nonexistent.py"))
        assert result is None
        assert code_run_called == []  # 文件不存在不调 code_run
        assert enqueue_called == []
        assert deleted == ["bg1"]  # 任务被永久删除

    def test_stdout_truncated_to_2000(self, tmp_path, monkeypatch):
        """stdout 超 2000 字符 → 截断"""
        from niu_api.internal.scheduler import service

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "clean.py").write_text("print('x'*5000)\n")

        monkeypatch.setattr(service, "get_db_path", lambda: str(tmp_path / "scheduled_tasks.db"))
        monkeypatch.setattr(service, "code_run", lambda *a, **kw: {"status": "success", "stdout": "x"*5000, "exit_code": 0})

        from niu_api.chat_queue import EnqueueResult

        captured = {}
        mock_q = type("Q", (), {
            "enqueue_sync": lambda self, **kw: captured.update(kw) or EnqueueResult(queued=True, request_id="1")
        })()
        monkeypatch.setattr(service, "get_chat_queue", lambda: mock_q)

        with patch("niu_api.chat._main_loop", MagicMock(is_closed=lambda: False)), \
             patch("niu_api.alerts.add_pending_alert"):

            service.trigger_callback(self._make_bg_task())

        # [定时任务] 前缀 + 截断提示 + ≤2000 字符正文
        assert len(captured["content"]) < 2200
        assert "…[截断]" in captured["content"]  # 截断标记必须存在（spec：超出加提示）
        assert captured["channel"] == "scheduler"

    def test_has_output_no_im_push(self, tmp_path, monkeypatch):
        """有输出 → 入队写 DB 唤醒主 Agent，不再 route_out 推 IM（程序消息不推 IM——用户需求）"""
        from unittest.mock import MagicMock, patch

        from niu_api.chat_queue import EnqueueResult
        from niu_api.internal.scheduler import service

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "clean.py").write_text("print('有垃圾')\n")

        monkeypatch.setattr(service, "get_db_path", lambda: str(tmp_path / "scheduled_tasks.db"))
        monkeypatch.setattr(service, "code_run", lambda *a, **kw: {"status": "success", "stdout": "有垃圾", "exit_code": 0})

        mock_q = type("Q", (), {
            "enqueue_sync": lambda self, **kw: EnqueueResult(queued=True, request_id="1")
        })()
        monkeypatch.setattr(service, "get_chat_queue", lambda: mock_q)

        with patch("niu_api.chat._main_loop", MagicMock(is_closed=lambda: False)), \
             patch("niu_api.alerts.add_pending_alert"), \
             patch("niu_api.channel.get_channel_router") as mock_cr:

            result = service.trigger_callback(self._make_bg_task())
            mock_cr.assert_not_called()  # 正向锁定 no-push 契约：程序消息不推 IM（防回归重新引入推送）

        assert result == "ok"

class TestTaskStoreAgentName:
    """T1: agent_name 列（subagent 任务类型）"""

    def test_new_db_agent_name_roundtrip(self, tmp_path):
        """新库 agent_name 可写可读（list_tasks 两分支 + get_task）"""
        from niu_api.internal.scheduler.task_store import TaskStore

        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="每日日志整理", scheduled_at="2026-01-01 00:00:00",
            name="journal-daily", task_kind="subagent", agent_name="journal-daily-agent",
            is_recurring=True, cron_expr="0 18 * * *",
        )
        assert store.list_tasks()[0]["agent_name"] == "journal-daily-agent"
        assert store.list_tasks(status="pending")[0]["agent_name"] == "journal-daily-agent"
        assert store.get_task(task_id)["agent_name"] == "journal-daily-agent"

    def test_find_and_overdue_include_agent_name(self, tmp_path):
        """find_task_by_name / get_overdue_tasks 也返回 agent_name"""
        from niu_api.internal.scheduler.task_store import TaskStore

        store = TaskStore(str(tmp_path / "test.db"))
        store.create_task(
            content="每日日志整理", scheduled_at="2020-01-01 00:00:00",  # 过去时间 → overdue
            name="journal-daily", task_kind="subagent", agent_name="journal-daily-agent",
        )
        assert store.find_task_by_name("journal-daily")["agent_name"] == "journal-daily-agent"
        overdue = store.get_overdue_tasks()
        assert len(overdue) == 1
        assert overdue[0]["agent_name"] == "journal-daily-agent"

    def test_old_db_migrates_agent_name(self, tmp_path):
        """老库（无 agent_name 列）迁移后可读可写；update_task 可设 agent_name（D4 迁移路径）"""
        import sqlite3

        from niu_api.internal.scheduler.task_store import TaskStore

        db_path = str(tmp_path / "old.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE scheduled_tasks (
                id TEXT PRIMARY KEY, content TEXT NOT NULL,
                scheduled_at DATETIME NOT NULL, is_recurring INTEGER DEFAULT 0,
                cron_expr TEXT, event_type TEXT, status TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP, triggered_at DATETIME,
                last_triggered_at DATETIME, name TEXT, chat_id TEXT,
                task_kind TEXT DEFAULT 'reminder', script_file TEXT
            )
        """)
        conn.execute("INSERT INTO scheduled_tasks (id, content, scheduled_at) VALUES ('old1', '老任务', '2026-01-01 00:00:00')")
        conn.commit()
        conn.close()

        store = TaskStore(db_path)
        tasks = store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["agent_name"] is None
        # D4 启动迁移路径：update_task 可设 task_kind + agent_name
        assert store.update_task("old1", task_kind="subagent", agent_name="journal-daily-agent")
        assert store.get_task("old1")["agent_name"] == "journal-daily-agent"


class TestScheduleTaskSubagentValidation:
    """T1: schedule_task subagent 类型校验（ToolRegistry 生产入口）"""

    @pytest.fixture
    def scheduler_module(self, tmp_path, monkeypatch):
        import sys

        from pathlib import Path

        src = Path(__file__).resolve().parent.parent / "mcp-servers" / "scheduler-server" / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        monkeypatch.setenv("SCHEDULER_DB_PATH", str(tmp_path / "tasks.db"))
        import niu_scheduler_server

        return niu_scheduler_server

    def test_subagent_requires_agent_name(self, scheduler_module):
        result = scheduler_module.schedule_task(
            content="每日日志整理", scheduled_at="2026-09-01T18:00:00", task_kind="subagent"
        )
        assert "error" in result
        assert "agent_name" in result["error"]

    def test_subagent_rejects_unknown_agent(self, scheduler_module, monkeypatch):
        import agent.subagent as subagent_mod

        monkeypatch.setattr(subagent_mod, "_resolve_agent_md_path", lambda name: None)
        result = scheduler_module.schedule_task(
            content="每日日志整理", scheduled_at="2026-09-01T18:00:00",
            task_kind="subagent", agent_name="no-such-agent",
        )
        assert "error" in result
        assert "不存在" in result["error"]

    def test_subagent_valid_agent_stores_agent_name(self, scheduler_module, monkeypatch):
        import agent.subagent as subagent_mod

        monkeypatch.setattr(subagent_mod, "_resolve_agent_md_path", lambda name: f"/fake/{name}.md")
        result = scheduler_module.schedule_task(
            content="每日日志整理", scheduled_at="2026-09-01T18:00:00",
            task_kind="subagent", agent_name="journal-daily-agent",
            is_recurring=True, cron_expr="0 18 * * *",
        )
        assert result["status"] == "success"
        from niu_api.internal.scheduler.task_store import TaskStore

        store = TaskStore(scheduler_module._get_db_path())
        task = store.list_tasks()[0]
        assert task["task_kind"] == "subagent"
        assert task["agent_name"] == "journal-daily-agent"

    def test_fallback_check_without_agent_package(self, scheduler_module, monkeypatch):
        """agent 包不可用（独立 stdio）→ 回退本地双目录检查，不抛异常"""
        import sys

        monkeypatch.setitem(sys.modules, "agent", None)
        monkeypatch.setitem(sys.modules, "agent.subagent", None)
        # kebab-case 非法 → 明确报错
        assert "非法" in scheduler_module._check_subagent_agent_name("Bad_Name")
        # 合法名但双目录均无 md → 不存在错误（而非异常）
        err = scheduler_module._check_subagent_agent_name("definitely-not-an-agent-xyz")
        assert err is not None and "不存在" in err


class TestRoutesSubagentValidation:
    """T1: HTTP POST /scheduler/tasks subagent 校验"""

    def _post(self, tmp_path, payload):
        from unittest.mock import patch

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from niu_api.internal.scheduler.routes import router
        from niu_api.internal.scheduler.task_store import TaskStore

        app = FastAPI()
        app.include_router(router)
        store = TaskStore(str(tmp_path / "test.db"))
        with patch("niu_api.internal.scheduler.routes.get_store", return_value=store):
            client = TestClient(app)
            response = client.post("/scheduler/tasks", json=payload)
        return store, response

    def test_create_subagent_without_agent_name_returns_400(self, tmp_path):
        _, response = self._post(tmp_path, {
            "content": "每日日志整理", "scheduled_at": "2026-09-01T18:00:00",
            "task_kind": "subagent",
        })
        assert response.status_code == 400
        assert "agent_name" in response.json()["detail"]

    def test_create_subagent_unknown_agent_returns_400(self, tmp_path, monkeypatch):
        import agent.subagent as subagent_mod

        monkeypatch.setattr(subagent_mod, "_resolve_agent_md_path", lambda name: None)
        _, response = self._post(tmp_path, {
            "content": "每日日志整理", "scheduled_at": "2026-09-01T18:00:00",
            "task_kind": "subagent", "agent_name": "no-such-agent",
        })
        assert response.status_code == 400
        assert "不存在" in response.json()["detail"]

    def test_create_subagent_valid_returns_200(self, tmp_path, monkeypatch):
        import agent.subagent as subagent_mod

        monkeypatch.setattr(subagent_mod, "_resolve_agent_md_path", lambda name: f"/fake/{name}.md")
        store, response = self._post(tmp_path, {
            "content": "每日日志整理", "scheduled_at": "2026-09-01T18:00:00",
            "task_kind": "subagent", "agent_name": "journal-daily-agent",
        })
        assert response.status_code == 200
        task = store.get_task(response.json()["task_id"])
        assert task["agent_name"] == "journal-daily-agent"
