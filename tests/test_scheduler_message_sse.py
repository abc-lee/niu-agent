"""scheduler 触发的消息走 SSE 推送测试
验证 source="scheduler" 路径下 user 和 assistant 消息都调 notify_new_message
（不 mock notify_new_message，用真实函数 + mock _event_subscribers 验证 put_nowait）
"""
import asyncio
import uuid
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from niu_api.chat_queue import ChatQueue
from niu_api.chat import _event_subscribers


class _FakeStore:
    def __init__(self):
        self.messages = []
    async def add_message(self, role, content, **kwargs):
        msg_id = str(uuid.uuid4())
        self.messages.append({"id": msg_id, "role": role, "content": content})
        return msg_id


@pytest.mark.asyncio
async def test_scheduler_user_message_pushes_sse():
    """scheduler 路径下 user 消息写入 DB 后推 SSE"""
    q = ChatQueue(runner=MagicMock())
    await q.start()

    # mock runner.chat 正常返回
    def _ok(*args, **kwargs):
        yield "assistant reply"
    q._runner.chat = MagicMock(side_effect=_ok)
    q._runner.last_return_value = None
    q._runner._persisted_msgs = None

    fake_store = _FakeStore()

    # 用真实 notify_new_message（不 patch）+ mock _event_subscribers 验证 put_nowait
    test_queue = asyncio.Queue(maxsize=10)
    _event_subscribers.append(test_queue)

    try:
        with patch("niu_api.chat_queue.get_message_store", new=AsyncMock(return_value=fake_store)):
            with patch("agent.context_manager.get_context_manager", new=AsyncMock()) as mock_cm:
                mock_cm.return_value.get_context_for_chat = AsyncMock(return_value=[])

                result = await asyncio.wait_for(
                    q.enqueue_and_wait(content="[定时任务] 吃药", source="scheduler", session_id="default"),
                    timeout=5
                )

        # 验证 SSE 事件被推送到订阅者队列
        events = []
        while not test_queue.empty():
            events.append(test_queue.get_nowait())

        # 应该有 user 事件 + assistant 事件
        user_events = [e for e in events if e.get("role") == "user"]
        assistant_events = [e for e in events if e.get("role") == "assistant"]

        assert len(user_events) >= 1, f"应该有 user SSE 事件，实际 {events}"
        assert len(assistant_events) >= 1, f"应该有 assistant SSE 事件，实际 {events}"
    finally:
        if test_queue in _event_subscribers:
            _event_subscribers.remove(test_queue)

    await q.stop()


@pytest.mark.asyncio
async def test_scheduler_assistant_reply_pushes_sse():
    """scheduler 路径下 assistant 回复写入 DB 后推 SSE"""
    q = ChatQueue(runner=MagicMock())
    await q.start()

    def _ok(*args, **kwargs):
        yield "⏰ 提醒时间到啦！"
    q._runner.chat = MagicMock(side_effect=_ok)
    q._runner.last_return_value = None
    q._runner._persisted_msgs = None

    fake_store = _FakeStore()
    test_queue = asyncio.Queue(maxsize=10)
    _event_subscribers.append(test_queue)

    try:
        with patch("niu_api.chat_queue.get_message_store", new=AsyncMock(return_value=fake_store)):
            with patch("agent.context_manager.get_context_manager", new=AsyncMock()) as mock_cm:
                mock_cm.return_value.get_context_for_chat = AsyncMock(return_value=[])

                await asyncio.wait_for(
                    q.enqueue_and_wait(content="test", source="scheduler", session_id="default"),
                    timeout=5
                )

        events = []
        while not test_queue.empty():
            events.append(test_queue.get_nowait())

        assistant_events = [e for e in events if e.get("role") == "assistant"]
        assert len(assistant_events) >= 1
        assert "⏰ 提醒时间到啦！" in assistant_events[0]["content"]
    finally:
        if test_queue in _event_subscribers:
            _event_subscribers.remove(test_queue)

    await q.stop()


@pytest.mark.asyncio
async def test_scheduler_assistant_reply_with_rv_messages_pushes_sse():
    """scheduler 路径下 rv 含 messages 时（if 分支）也调 notify_new_message 推 SSE

    覆盖 persist_agent_reply 的 if 分支（L260-315）：
    runner.last_return_value 含 messages 时遍历 rv["messages"][history_len+1:]
    逐条持久化，在 elif last_assistant_id 子分支调 notify_new_message 推 SSE。

    与 test_scheduler_assistant_reply_pushes_sse（elif 分支，rv=None）互补。
    """
    q = ChatQueue(runner=MagicMock())
    await q.start()

    def _ok(*args, **kwargs):
        yield "assistant reply"
    q._runner.chat = MagicMock(side_effect=_ok)
    # rv 含 messages：第 0 条 user 被 history_len+1=1 切片跳过，第 1 条 assistant 被持久化
    q._runner.last_return_value = {
        "messages": [
            {"role": "user", "content": "[定时任务] 吃药"},
            {"role": "assistant", "content": "assistant reply"},
        ]
    }
    # _persisted_msgs=None 让 if 分支走 elif last_assistant_id 子分支调 notify_new_message
    q._runner._persisted_msgs = None

    fake_store = _FakeStore()
    test_queue = asyncio.Queue(maxsize=10)
    _event_subscribers.append(test_queue)

    try:
        with patch("niu_api.chat_queue.get_message_store", new=AsyncMock(return_value=fake_store)):
            with patch("agent.context_manager.get_context_manager", new=AsyncMock()) as mock_cm:
                mock_cm.return_value.get_context_for_chat = AsyncMock(return_value=[])

                await asyncio.wait_for(
                    q.enqueue_and_wait(content="[定时任务] 吃药", source="scheduler", session_id="default"),
                    timeout=5
                )

        events = []
        while not test_queue.empty():
            events.append(test_queue.get_nowait())

        assistant_events = [e for e in events if e.get("role") == "assistant"]
        assert len(assistant_events) >= 1, (
            f"if 分支也应推 assistant SSE 事件，实际 events={events}"
        )
        # 验证推送的是 rv["messages"] 里的 assistant content（strip_at_messages 后保持原样）
        assert assistant_events[0]["content"] == "assistant reply"
        # 验证 source 走 electron 通道（Task 5.1 修复后所有 source 都走 electron）
        assert assistant_events[0]["source"] == "electron"
    finally:
        if test_queue in _event_subscribers:
            _event_subscribers.remove(test_queue)

    await q.stop()
