"""trigger_callback 失败重试测试
用 AsyncMock 让 enqueue_and_wait 表现为 async 函数返回 str（类型正确）。
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from niu_api.internal.scheduler.service import trigger_callback


@pytest.mark.asyncio
async def test_trigger_callback_retries_on_failure():
    """第一次失败后 10s 重试一次，重试成功返回 reply"""
    task = {"id": "task-1", "content": "测试任务"}

    call_count = {"n": 0}

    async def _fake_enqueue(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None  # 第一次失败
        else:
            return "重试成功"

    fake_q = MagicMock()
    fake_q.enqueue_and_wait = _fake_enqueue

    with patch("niu_api.chat_queue.get_chat_queue", return_value=fake_q):
        with patch("niu_api.chat._main_loop", asyncio.get_running_loop()):
            with patch("niu_api.alerts.add_pending_alert"):
                # mock time.sleep 避免真等 10s
                with patch("niu_api.internal.scheduler.service.time.sleep"):
                    # trigger_callback 是同步函数，用 run_in_executor 调用
                    result = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: trigger_callback(task)
                    )

    assert result == "重试成功"
    assert call_count["n"] == 2, f"应调用 2 次，实际 {call_count['n']}"


@pytest.mark.asyncio
async def test_trigger_callback_both_attempts_fail_returns_none():
    """两次都失败返回 None"""
    task = {"id": "task-1", "content": "测试任务"}

    call_count = {"n": 0}

    async def _fake_enqueue(*args, **kwargs):
        call_count["n"] += 1
        return None  # 永远失败

    fake_q = MagicMock()
    fake_q.enqueue_and_wait = _fake_enqueue

    with patch("niu_api.chat_queue.get_chat_queue", return_value=fake_q):
        with patch("niu_api.chat._main_loop", asyncio.get_running_loop()):
            with patch("niu_api.alerts.add_pending_alert"):
                with patch("niu_api.internal.scheduler.service.time.sleep"):
                    result = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: trigger_callback(task)
                    )

    assert result is None
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_trigger_callback_first_attempt_success_no_retry():
    """第一次成功不重试"""
    task = {"id": "task-1", "content": "测试任务"}

    call_count = {"n": 0}

    async def _fake_enqueue(*args, **kwargs):
        call_count["n"] += 1
        return "首次成功"

    fake_q = MagicMock()
    fake_q.enqueue_and_wait = _fake_enqueue

    with patch("niu_api.chat_queue.get_chat_queue", return_value=fake_q):
        with patch("niu_api.chat._main_loop", asyncio.get_running_loop()):
            with patch("niu_api.alerts.add_pending_alert"):
                with patch("niu_api.internal.scheduler.service.time.sleep"):
                    result = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: trigger_callback(task)
                    )

    assert result == "首次成功"
    assert call_count["n"] == 1, f"应调用 1 次，实际 {call_count['n']}"
