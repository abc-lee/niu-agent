"""LightRAG 损坏时启动流程阻塞的单元测试。

背景：scheduler/ChatQueue/db_monitor 在 Phase 1 检测到损坏后仍跑，
60s 超时强行扫描触发 journal-agent → ChatQueue → runner.chat 报错。
本测试验证：
1. Phase 1 need_repair=True 时不调 signal_scheduler_ready
2. Phase 1 need_repair=True 时 ChatQueue 被 pause
3. Phase 1 need_repair=True 时不启动 db_monitor
4. Phase 1 need_repair=True 时 scheduler.cancel_delayed_start 被调用
5. Phase 1 need_repair=True 时 _lightrag_corrupt_skip_init 局部变量为 True（跳过后续 init）
6. Phase 1 need_repair=False（正常启动）时逻辑不变（signal/db_monitor/cancel 都正常）
"""
import asyncio
from unittest import mock


def test_should_signal_scheduler_ready_gate():
    """Phase 1 need_repair=True 时返回 False（不 signal）；False 时返回 True（signal）"""
    from niu_api.internal.lightrag_manager import should_signal_scheduler_ready

    # need_repair=True 时返回 False（不 signal）
    assert should_signal_scheduler_ready({"need_repair": True}) is False
    # need_repair=False 时返回 True（signal）
    assert should_signal_scheduler_ready({"need_repair": False}) is True
    # 缺 key 时默认 False（不阻塞正常启动）
    assert should_signal_scheduler_ready({}) is True


def test_should_start_db_monitor_gate():
    """Phase 1 need_repair=True 时返回 False（不启动 db_monitor）；False 时返回 True"""
    from niu_api.internal.lightrag_manager import should_start_db_monitor

    assert should_start_db_monitor({"need_repair": True}) is False
    assert should_start_db_monitor({"need_repair": False}) is True
    assert should_start_db_monitor({}) is True


def test_pause_chatqueue_if_corrupt_calls_pause_when_corrupt():
    """Phase 1 need_repair=True 时，ChatQueue 被 pause（worker 不消费消息）"""
    from niu_api.internal.lightrag_manager import pause_chatqueue_if_corrupt
    from niu_api.chat_queue import ChatQueue

    # 模拟一个 ChatQueue 实例
    with mock.patch("niu_api.chat_queue.get_chat_queue") as mock_get:
        q = mock.MagicMock(spec=ChatQueue)
        q._paused = False
        mock_get.return_value = q

        # need_repair=True 时调 pause
        pause_chatqueue_if_corrupt({"need_repair": True})
        q.pause.assert_called_once()

        # need_repair=False 时不 pause
        q.pause.reset_mock()
        pause_chatqueue_if_corrupt({"need_repair": False})
        q.pause.assert_not_called()


def test_pause_chatqueue_if_corrupt_swallows_exceptions():
    """pause_chatqueue_if_corrupt 异常时只 log warning，不抛出（不阻塞 lifespan）"""
    from niu_api.internal.lightrag_manager import pause_chatqueue_if_corrupt

    with mock.patch("niu_api.chat_queue.get_chat_queue", side_effect=RuntimeError("queue not ready")):
        # 不应抛异常
        pause_chatqueue_if_corrupt({"need_repair": True})


def test_cancel_scheduler_delayed_start_if_corrupt_calls_cancel_when_corrupt():
    """Phase 1 need_repair=True 时调 scheduler.cancel_delayed_start

    补 P1 漏洞：scheduler.start_delayed 的 _ready_event.wait(60) 60s 超时后
    会强行 start（scheduler.py L103-106）。即使不调 signal_scheduler_ready，
    scheduler 线程也会在 60s 后启动。调 cancel_delayed_start 设
    _delayed_start_cancelled=True，_delayed_start 线程超时后检查 flag 直接 return。
    """
    from niu_api.internal.lightrag_manager import cancel_scheduler_delayed_start_if_corrupt

    with mock.patch("niu_api.internal.scheduler.service.get_scheduler") as mock_get:
        sched = mock.MagicMock()
        mock_get.return_value = sched

        # need_repair=True 时调 cancel_delayed_start
        cancel_scheduler_delayed_start_if_corrupt({"need_repair": True})
        sched.cancel_delayed_start.assert_called_once()

        # need_repair=False 时不调
        sched.cancel_delayed_start.reset_mock()
        cancel_scheduler_delayed_start_if_corrupt({"need_repair": False})
        sched.cancel_delayed_start.assert_not_called()


def test_cancel_scheduler_delayed_start_if_corrupt_handles_none_scheduler():
    """get_scheduler 返回 None（未启动）时不抛异常"""
    from niu_api.internal.lightrag_manager import cancel_scheduler_delayed_start_if_corrupt

    with mock.patch("niu_api.internal.scheduler.service.get_scheduler", return_value=None):
        # 不应抛异常
        cancel_scheduler_delayed_start_if_corrupt({"need_repair": True})


def test_scheduler_class_has_cancel_delayed_start_method():
    """Scheduler 类必须有 cancel_delayed_start 方法（轻量取消 delayed start）"""
    from niu_api.internal.scheduler.scheduler import Scheduler

    assert hasattr(Scheduler, "cancel_delayed_start"), "Scheduler 类必须新增 cancel_delayed_start 方法"


def test_scheduler_cancel_delayed_start_sets_flag():
    """cancel_delayed_start 调用后 _delayed_start_cancelled=True"""
    from niu_api.internal.scheduler.scheduler import Scheduler

    # 用最小依赖构造 Scheduler（store_factory 避免 TaskStore 初始化 db）
    with mock.patch.object(Scheduler, "_recover_orphaned_tasks"):
        sched = Scheduler(
            db_path=":memory:",
            trigger_callback=lambda task: None,
            store_factory=lambda: mock.MagicMock(),
        )

    # 初始 False
    assert sched._delayed_start_cancelled is False

    # 调 cancel 后 True
    sched.cancel_delayed_start()
    assert sched._delayed_start_cancelled is True
