"""run_interruptibly 单元测试——统一可中断执行器。

覆盖：正常完成 / stop 放弃 / 异常上抛 / stop 恰好发生在完成瞬间 / 重复 stop 检查。
全部 mock，不调真实 LLM。
"""
import threading
import time

import pytest

from agent.generic.interruptible import run_interruptibly


def _never_stop():
    return False


def test_completes_with_result():
    """正常完成：返回 (True, result)。"""
    completed, result = run_interruptibly(lambda: 42, _never_stop, timeout=0.05)
    assert completed is True
    assert result == 42


def test_abandons_on_stop():
    """stop_check 置位：放弃等待，返回 (False, None)；后台线程继续跑（不阻塞返回）。"""
    stop_flag = {"v": False}

    def _flip():
        stop_flag["v"] = True

    def _slow():
        time.sleep(0.5)  # 慢于轮询间隔
        return "late"

    threading.Timer(0.05, _flip).start()
    started = time.monotonic()
    completed, result = run_interruptibly(_slow, lambda: stop_flag["v"], timeout=0.02)
    elapsed = time.monotonic() - started
    assert completed is False
    assert result is None
    assert elapsed < 0.3  # ≤0.2s 内放弃（轮询间隔 + 余量）


def test_never_stops_waits_for_completion():
    """stop 永不置位：即使慢于轮询间隔也等 fn 完成。"""
    started = time.monotonic()
    completed, result = run_interruptibly(lambda: "done", _never_stop, timeout=0.01)
    assert completed is True
    assert result == "done"
    assert time.monotonic() - started < 1.0


def test_exception_propagates():
    """fn 抛异常：原样上抛（completed=True 语义：fn 已结束）。"""

    def _boom():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_interruptibly(_boom, _never_stop, timeout=0.05)


def test_stop_immediately_after_start():
    """stop 一开始就置位：立即放弃（R4-P1 预检——不启动后台线程）。"""
    executed = {"v": False}

    def _slow_side_effect():
        executed["v"] = True
        time.sleep(0.5)

    completed, result = run_interruptibly(_slow_side_effect, lambda: True, timeout=0.02)
    assert completed is False
    assert executed["v"] is False  # R4-P1：预检跳过，线程未启动
    assert result is None


def test_args_kwargs_passed():
    """args/kwargs 透传给 fn。"""
    completed, result = run_interruptibly(
        lambda a, b: a + b, _never_stop, timeout=0.05, args=(1,), kwargs={"b": 2}
    )
    assert completed is True
    assert result == 3


def test_stop_and_completion_race_returns_result():
    """stop 与 fn 完成竞态：fn 已完成（结果已入队）→ 返回 (True, result)（fn 已完成，结果不丢）。"""
    stop_flag = {"v": False}

    def _fast_with_flip():
        stop_flag["v"] = True  # fn 完成瞬间 stop 也置位
        return "done"

    completed, result = run_interruptibly(_fast_with_flip, lambda: stop_flag["v"], timeout=0.01)
    assert completed is True
    assert result == "done"
