"""Tests for Scheduler.check_and_trigger with sequential execution"""
import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


def _make_scheduler(db_path, callback, mock_store):
    """Construct a Scheduler bypassing __init__ to avoid DB/TaskStore dependency"""
    from niu_api.internal.scheduler.scheduler import Scheduler, _CALLBACK_TIMEOUT
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.db_path = db_path
    scheduler.trigger_callback = callback
    scheduler.store = mock_store
    scheduler.running = True
    scheduler.thread = None
    scheduler._lock = __import__("threading").RLock()
    scheduler._check_lock = __import__("threading").Lock()
    scheduler._executor = __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=2)
    scheduler._delayed_start_cancelled = False
    # Task 8 新增字段：失败计数器（必须同步 fixture，否则 result is None 分支会 AttributeError）
    scheduler._task_fail_count = {}
    scheduler._TASK_FAIL_THRESHOLD = 3
    scheduler._ready_event = __import__("threading").Event()
    # _store_factory 是 commit ff04843f 引入的字段，fixture 之前漏了
    scheduler._store_factory = None
    # Task 2 新增属性：错峰等待轮询非忙 + 二次确认防抖
    scheduler._busy_poll_interval = 2
    scheduler._double_confirm_delay = 3
    scheduler._stagger_max_wait = 600
    return scheduler, _CALLBACK_TIMEOUT


@pytest.fixture
def mock_scheduler(tmp_path):
    db_path = str(tmp_path / "scheduler.db")
    callback = MagicMock(return_value="ok")
    mock_store = MagicMock()
    scheduler, timeout = _make_scheduler(db_path, callback, mock_store)
    yield scheduler, callback, mock_store, timeout
    # teardown：shutdown executor 避免线程池泄漏
    scheduler._executor.shutdown(wait=False)


class TestCheckAndTriggerSequential:
    def test_multiple_due_tasks_execute_sequentially(self, mock_scheduler):
        """多个到期任务顺序执行，不是同时触发"""
        scheduler, callback, mock_store, _ = mock_scheduler

        due_tasks = [
            {
                "id": f"task-{i}",
                "content": f"task{i}",
                "is_recurring": True,
                "cron_expr": "0 3 * * *",
                "scheduled_at": (datetime.now() - timedelta(hours=5 - i)).isoformat(),
            }
            for i in range(4)
        ]

        mock_store.get_overdue_tasks.return_value = due_tasks
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "task-0",
            "status": "in_progress",
            "scheduled_at": due_tasks[0]["scheduled_at"],
            "last_executed_date": None,
        }
        mock_store.update_last_executed_date.return_value = True

        start_time = time.time()
        scheduler.check_and_trigger()
        elapsed = time.time() - start_time

        # 4 tasks with 2s stagger = ~6s minimum (3 intervals)
        assert elapsed >= scheduler._overdue_stagger_interval * 3 - 1
        assert callback.call_count == 4

    def test_single_due_task_no_stagger_wait(self, mock_scheduler):
        """单个到期任务不需要间隔等待"""
        scheduler, callback, mock_store, _ = mock_scheduler

        mock_store.get_overdue_tasks.return_value = [
            {
                "id": "task-1",
                "content": "测试定时提醒：请检查系统状态",
                "is_recurring": True,
                "cron_expr": "0 3 * * *",
                "scheduled_at": (datetime.now() - timedelta(hours=5)).isoformat(),
            }
        ]
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "task-1",
            "status": "in_progress",
            "scheduled_at": (datetime.now() - timedelta(hours=5)).isoformat(),
            "last_executed_date": None,
        }
        mock_store.update_last_executed_date.return_value = True

        start_time = time.time()
        scheduler.check_and_trigger()
        elapsed = time.time() - start_time

        assert elapsed < scheduler._overdue_stagger_interval
        assert callback.call_count == 1

    def test_no_due_tasks_returns_immediately(self, mock_scheduler):
        """没有到期任务时立即返回"""
        scheduler, callback, mock_store, _ = mock_scheduler
        mock_store.get_overdue_tasks.return_value = []
        scheduler.check_and_trigger()
        assert callback.call_count == 0

    def test_stagger_wait_interruptible_by_stop(self, mock_scheduler):
        """间隔等待可被 stop() 中断"""
        scheduler, callback, mock_store, _ = mock_scheduler

        mock_store.get_overdue_tasks.return_value = [
            {
                "id": "task-1",
                "content": "task1",
                "is_recurring": True,
                "cron_expr": "0 3 * * *",
                "scheduled_at": (datetime.now() - timedelta(hours=5)).isoformat(),
            },
            {
                "id": "task-2",
                "content": "task2",
                "is_recurring": True,
                "cron_expr": "0 4 * * *",
                "scheduled_at": (datetime.now() - timedelta(hours=4)).isoformat(),
            },
        ]
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "task-1",
            "status": "in_progress",
            "scheduled_at": (datetime.now() - timedelta(hours=5)).isoformat(),
            "last_executed_date": None,
        }
        mock_store.update_last_executed_date.return_value = True

        def stop_after_first(task_id, **_kwargs):
            if task_id == "task-1":
                scheduler.running = False
            return True

        mock_store.update_task.side_effect = stop_after_first

        start_time = time.time()
        scheduler.check_and_trigger()
        elapsed = time.time() - start_time

        assert elapsed < 15
        assert callback.call_count <= 1

    def test_cas_prevents_double_trigger(self, mock_scheduler):
        """CAS 机制防止同一任务被重复触发"""
        scheduler, callback, mock_store, _ = mock_scheduler

        mock_store.get_overdue_tasks.return_value = [
            {
                "id": "task-1",
                "content": "task1",
                "is_recurring": True,
                "cron_expr": "0 3 * * *",
                "scheduled_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            }
        ]
        mock_store.update_task.return_value = False

        scheduler.check_and_trigger()
        assert callback.call_count == 0

    def test_already_executed_today_skips_and_reschedules(self, mock_scheduler):
        """当天已执行的循环任务跳过执行，reschedule 到下次"""
        scheduler, callback, mock_store, _ = mock_scheduler

        today = __import__("datetime").date.today().isoformat()
        mock_store.get_overdue_tasks.return_value = [
            {
                "id": "task-1",
                "content": "task1",
                "is_recurring": True,
                "cron_expr": "0 3 * * *",
                "scheduled_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            }
        ]
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "task-1",
            "status": "in_progress",
            "scheduled_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            "last_executed_date": today,
        }

        scheduler.check_and_trigger()
        assert callback.call_count == 0
        mock_store.update_task.assert_called()

    def test_callback_timeout_is_120s(self, mock_scheduler):
        """回调超时为120秒"""
        _, _, _, timeout = mock_scheduler
        assert timeout == 120

    def test_one_time_task_executed_and_deleted(self, mock_scheduler):
        """一次性任务执行后删除"""
        scheduler, callback, mock_store, _ = mock_scheduler

        mock_store.get_overdue_tasks.return_value = [
            {
                "id": "task-1",
                "content": "remind me",
                "is_recurring": False,
                "scheduled_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            }
        ]
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "task-1",
            "status": "in_progress",
            "scheduled_at": (datetime.now() - timedelta(hours=1)).isoformat(),
        }

        scheduler.check_and_trigger()
        assert callback.call_count == 1
        mock_store.delete_task_permanent.assert_called_once_with("task-1")

    def test_callback_failure_marks_task_failed(self, mock_scheduler):
        """回调失败时循环任务 reschedule 到下次 cron（Task 8 行为变更：失败 3 次才标 failed）"""
        scheduler, callback, mock_store, _ = mock_scheduler

        callback.return_value = None

        mock_store.get_overdue_tasks.return_value = [
            {
                "id": "task-1",
                "content": "task1",
                "is_recurring": True,
                "cron_expr": "0 3 * * *",
                "scheduled_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            }
        ]
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "task-1",
            "status": "in_progress",
            "scheduled_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            "last_executed_date": None,
        }

        scheduler.check_and_trigger()
        # Task 8 改动：循环任务失败 1 次不标 failed，而是 reschedule 到下次 cron
        # 失败计数器累加到 _task_fail_count，达阈值 3 才标 failed
        reschedule_calls = [c for c in mock_store.update_task.call_args_list
                            if "pending" in str(c)]
        assert len(reschedule_calls) >= 1, "失败 1 次应 reschedule 而非标 failed"
        # 验证失败计数器已累加
        assert scheduler._task_fail_count.get("task-1") == 1

    def test_start_and_stop_with_lock_protection(self, tmp_path):
        """start() 和 stop() 使用锁保护 running 标志"""
        from niu_api.internal.scheduler.scheduler import Scheduler
        from niu_api.internal.scheduler.task_store import TaskStore

        db_path = str(tmp_path / "test.db")
        # Create real DB so _cleanup_old_tasks doesn't crash
        real_store = TaskStore(db_path)
        scheduler = Scheduler(db_path=db_path, trigger_callback=lambda t: "ok", store=real_store)
        scheduler._overdue_stagger_interval = 600

        # start() should set running = True under lock
        scheduler.start()
        assert scheduler.running is True

        # stop() should set running = False under lock
        scheduler.stop()
        assert scheduler.running is False

    def test_concurrent_check_and_trigger_skipped(self, mock_scheduler):
        """并发调用 check_and_trigger 时，第二次调用被跳过"""
        scheduler, callback, mock_store, _ = mock_scheduler

        mock_store.get_overdue_tasks.return_value = [
            {
                "id": "task-1",
                "content": "task1",
                "is_recurring": True,
                "cron_expr": "0 3 * * *",
                "scheduled_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            }
        ]
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "task-1",
            "status": "in_progress",
            "scheduled_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            "last_executed_date": None,
        }
        mock_store.update_last_executed_date.return_value = True

        # First call acquires _check_lock
        import threading
        results = []

        def call_in_thread():
            r = scheduler.check_and_trigger()
            results.append(r)

        # Manually hold the lock to simulate a long-running check_and_trigger
        scheduler._check_lock.acquire()
        # Second call should be skipped (lock already held)
        scheduler.check_and_trigger()
        assert callback.call_count == 0  # Skipped because lock is held

        # Release lock and verify normal call works
        scheduler._check_lock.release()
        scheduler.check_and_trigger()
        assert callback.call_count == 1


class TestIsBackendBusy:
    """测试 _is_backend_busy 通过 run_coroutine_threadsafe 桥接读取"""

    def test_returns_false_when_chat_lock_free(self, mock_scheduler):
        """_chat_lock 空闲时返回 False"""
        scheduler, _, _, _ = mock_scheduler
        import asyncio
        from unittest.mock import patch, MagicMock

        fake_loop = MagicMock()
        fake_loop.is_closed.return_value = False
        fake_future = MagicMock()
        fake_future.result.return_value = False  # _chat_lock.locked() = False

        with patch('niu_api.chat._main_loop', fake_loop), \
             patch('asyncio.run_coroutine_threadsafe', return_value=fake_future):
            assert scheduler._is_backend_busy() is False

    def test_returns_true_when_chat_lock_held(self, mock_scheduler):
        """_chat_lock 被持有时返回 True"""
        scheduler, _, _, _ = mock_scheduler
        from unittest.mock import patch, MagicMock

        fake_loop = MagicMock()
        fake_loop.is_closed.return_value = False
        fake_future = MagicMock()
        fake_future.result.return_value = True

        with patch('niu_api.chat._main_loop', fake_loop), \
             patch('asyncio.run_coroutine_threadsafe', return_value=fake_future):
            assert scheduler._is_backend_busy() is True

    def test_returns_false_when_loop_unavailable(self, mock_scheduler):
        """主 loop 为 None 时返回 False（不阻塞调度）"""
        scheduler, _, _, _ = mock_scheduler
        from unittest.mock import patch

        with patch('niu_api.chat._main_loop', None):
            assert scheduler._is_backend_busy() is False

    def test_returns_false_on_query_timeout(self, mock_scheduler):
        """桥接 future.result 超时返回 False（不阻塞调度）"""
        scheduler, _, _, _ = mock_scheduler
        from unittest.mock import patch, MagicMock
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        fake_loop = MagicMock()
        fake_loop.is_closed.return_value = False
        fake_future = MagicMock()
        fake_future.result.side_effect = FuturesTimeoutError()

        with patch('niu_api.chat._main_loop', fake_loop), \
             patch('asyncio.run_coroutine_threadsafe', return_value=fake_future):
            assert scheduler._is_backend_busy() is False


class TestStaggerWaitBackendIdle:
    """测试错峰等待改为等后端非忙+二次确认（持锁）"""

    def test_executes_next_after_double_confirm_idle(self, mock_scheduler):
        """后端空闲→等3s→仍空闲→执行下一条；验证 _is_backend_busy 调用次数"""
        scheduler, callback, mock_store, _ = mock_scheduler
        scheduler._double_confirm_delay = 1
        scheduler._busy_poll_interval = 1

        due_tasks = [
            {"id": "t0", "content": "t0", "is_recurring": True,
             "cron_expr": "0 3 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=5)).isoformat()},
            {"id": "t1", "content": "t1", "is_recurring": True,
             "cron_expr": "0 4 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=4)).isoformat()},
        ]
        mock_store.get_overdue_tasks.return_value = due_tasks
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "t0", "status": "in_progress",
            "scheduled_at": due_tasks[0]["scheduled_at"],
            "last_executed_date": None,
        }
        mock_store.update_last_executed_date.return_value = True

        with patch.object(scheduler, '_is_backend_busy', return_value=False) as mock_busy:
            scheduler.check_and_trigger()

        # i=1 错峰等待：首次查询(False) + 二次确认查询(False) = 2 次
        assert callback.call_count == 2
        assert mock_busy.call_count == 2

    def test_rechecks_when_subagent_takes_lock_during_confirm(self, mock_scheduler):
        """二次确认时若后端又忙（子Agent抢占）→ 继续等；验证调用序列"""
        scheduler, callback, mock_store, _ = mock_scheduler
        scheduler._double_confirm_delay = 1
        scheduler._busy_poll_interval = 1

        due_tasks = [
            {"id": "t0", "content": "t0", "is_recurring": True,
             "cron_expr": "0 3 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=5)).isoformat()},
            {"id": "t1", "content": "t1", "is_recurring": True,
             "cron_expr": "0 4 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=4)).isoformat()},
        ]
        mock_store.get_overdue_tasks.return_value = due_tasks
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "t0", "status": "in_progress",
            "scheduled_at": due_tasks[0]["scheduled_at"],
            "last_executed_date": None,
        }
        mock_store.update_last_executed_date.return_value = True

        # 首次 False→二次确认 True（被抢占）→轮询 False→二次确认 False
        busy_sequence = [False, True, False, False]
        with patch.object(scheduler, '_is_backend_busy', side_effect=busy_sequence) as mock_busy:
            scheduler.check_and_trigger()

        assert callback.call_count == 2
        assert mock_busy.call_count == 4  # 首次+确认(失败) + 轮询 + 首次+确认(成功)

    def test_waits_while_backend_busy(self, mock_scheduler):
        """后端忙碌时轮询等待，变空闲后二次确认执行"""
        scheduler, callback, mock_store, _ = mock_scheduler
        scheduler._double_confirm_delay = 1
        scheduler._busy_poll_interval = 1

        due_tasks = [
            {"id": "t0", "content": "t0", "is_recurring": True,
             "cron_expr": "0 3 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=5)).isoformat()},
            {"id": "t1", "content": "t1", "is_recurring": True,
             "cron_expr": "0 4 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=4)).isoformat()},
        ]
        mock_store.get_overdue_tasks.return_value = due_tasks
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "t0", "status": "in_progress",
            "scheduled_at": due_tasks[0]["scheduled_at"],
            "last_executed_date": None,
        }
        mock_store.update_last_executed_date.return_value = True

        # 2次忙→False→二次确认False
        busy_sequence = [True, True, False, False]
        with patch.object(scheduler, '_is_backend_busy', side_effect=busy_sequence) as mock_busy:
            scheduler.check_and_trigger()

        assert callback.call_count == 2
        assert mock_busy.call_count == 4

    def test_stagger_wait_interruptible_during_double_confirm(self, mock_scheduler):
        """二次确认 sleep 期间 stop() 能快速中断（<2s）"""
        scheduler, callback, mock_store, _ = mock_scheduler
        scheduler._double_confirm_delay = 3  # 生产值，测试中断响应
        scheduler._busy_poll_interval = 1

        due_tasks = [
            {"id": "t0", "content": "t0", "is_recurring": True,
             "cron_expr": "0 3 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=5)).isoformat()},
            {"id": "t1", "content": "t1", "is_recurring": True,
             "cron_expr": "0 4 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=4)).isoformat()},
        ]
        mock_store.get_overdue_tasks.return_value = due_tasks
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "t0", "status": "in_progress",
            "scheduled_at": due_tasks[0]["scheduled_at"],
            "last_executed_date": None,
        }
        mock_store.update_last_executed_date.return_value = True

        # 后端空闲（进入二次确认），0.5s 后 stop
        import threading
        def stop_after_delay():
            time.sleep(0.5)
            scheduler.running = False
        threading.Thread(target=stop_after_delay, daemon=True).start()

        with patch.object(scheduler, '_is_backend_busy', return_value=False):
            scheduler.check_and_trigger()

        # 只执行第一个任务（i=0），第二个在二次确认 sleep 期间被中断
        assert callback.call_count == 1

    def test_fallback_timeout_forces_next(self, mock_scheduler):
        """后端一直忙超过总超时上限，强制执行下一条"""
        scheduler, callback, mock_store, _ = mock_scheduler
        scheduler._double_confirm_delay = 1
        scheduler._busy_poll_interval = 1
        scheduler._stagger_max_wait = 2

        due_tasks = [
            {"id": "t0", "content": "t0", "is_recurring": True,
             "cron_expr": "0 3 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=5)).isoformat()},
            {"id": "t1", "content": "t1", "is_recurring": True,
             "cron_expr": "0 4 * * *",
             "scheduled_at": (datetime.now() - timedelta(hours=4)).isoformat()},
        ]
        mock_store.get_overdue_tasks.return_value = due_tasks
        mock_store.update_task.return_value = True
        mock_store.get_task.return_value = {
            "id": "t0", "status": "in_progress",
            "scheduled_at": due_tasks[0]["scheduled_at"],
            "last_executed_date": None,
        }
        mock_store.update_last_executed_date.return_value = True

        with patch.object(scheduler, '_is_backend_busy', return_value=True):
            start = time.time()
            scheduler.check_and_trigger()
            elapsed = time.time() - start

        assert callback.call_count == 2
        assert elapsed >= 2  # 至少等了总超时