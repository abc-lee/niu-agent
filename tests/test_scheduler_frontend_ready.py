"""scheduler _delayed_start 等 frontend_ready_event 测试"""
import time
from unittest.mock import patch

import pytest

from niu_api.internal.scheduler.scheduler import Scheduler


class _FakeStore:
    """与真实 TaskStore 签名一致"""
    def get_overdue_tasks(self):
        return []
    def recover_orphaned_tasks(self):
        return 0
    def retry_failed_tasks(self, retry_interval_seconds=300):
        return 0
    def cleanup_old_tasks(self, days=100):
        return 0


class _FakeExecutor:
    def submit(self, fn, *args):
        class _F:
            def result(self, timeout=None):
                return None
        return _F()
    def shutdown(self, wait=True):
        pass


@pytest.fixture(autouse=True)
def reset_frontend_ready_event():
    """每个测试前重置 frontend_ready_event"""
    from niu_api.chat import frontend_ready_event
    frontend_ready_event.clear()
    yield
    frontend_ready_event.clear()


def test_delayed_start_waits_for_frontend_ready(tmp_path):
    """signal 后 scheduler 等 frontend_ready_event 才 start"""
    db_path = str(tmp_path / "tasks.db")
    s = Scheduler(db_path=db_path, trigger_callback=lambda t: None, store_factory=lambda: _FakeStore())
    s._executor = _FakeExecutor()

    s.start_delayed()
    s._ready_event.set()

    # 此时 scheduler 还不应 start（等 frontend_ready）
    time.sleep(0.5)
    assert not s.running, "scheduler 不应在 frontend_ready 之前 start"

    # 通知 frontend ready
    from niu_api.chat import frontend_ready_event
    frontend_ready_event.set()

    # 等待 Phase 3 sleep(2) + start
    time.sleep(3)
    assert s.running, "scheduler 应在 frontend_ready 后 start"

    s.stop()


def test_delayed_start_frontend_ready_timeout(tmp_path):
    """frontend_ready 60s 超时后强制 start（用 patch.object mock 避免依赖 CPython 实现细节）"""
    db_path = str(tmp_path / "tasks.db")
    s = Scheduler(db_path=db_path, trigger_callback=lambda t: None, store_factory=lambda: _FakeStore())
    s._executor = _FakeExecutor()

    # mock frontend_ready_event.wait 直接返回 False（模拟超时）
    from niu_api.internal.scheduler import scheduler as scheduler_module
    with patch.object(scheduler_module.frontend_ready_event, 'wait', return_value=False):
        s.start_delayed()
        s._ready_event.set()

        # 等 Phase 3 sleep(2) + start
        time.sleep(3)
        assert s.running, "60s 超时后应强制 start"

    s.stop()


def test_delayed_start_signal_timeout_aborts(tmp_path):
    """signal 180s 超时后不强行 start，直接 return（用 patch.object mock）"""
    db_path = str(tmp_path / "tasks.db")
    s = Scheduler(db_path=db_path, trigger_callback=lambda t: None, store_factory=lambda: _FakeStore())
    s._executor = _FakeExecutor()

    # mock _ready_event.wait 直接返回 False（模拟超时）
    with patch.object(s._ready_event, 'wait', return_value=False):
        s.start_delayed()
        time.sleep(1)
        assert not s.running, "signal 超时后 scheduler 不应 start"

        # 清理：cancel delayed start 避免后台线程残留
        s.cancel_delayed_start()

    s.stop()
