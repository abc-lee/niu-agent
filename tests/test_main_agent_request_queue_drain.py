"""验证 db_monitor 链路 A：主 Agent 闲置检测 + MainAgentRequestQueue 消费 + 推 SSE。"""
import asyncio
import unittest.mock as mock

from niu_api import db_monitor
from agent.main_agent_request_queue import get_main_agent_request_queue
from niu_api.compat import _chat_lock


def test_drain_skipped_when_main_agent_busy():
    """主 Agent 忙时（_chat_lock.locked）不消费队列。"""
    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass
    q.push("[子名] 测试消息")

    async def busy_and_drain():
        await _chat_lock.acquire()
        try:
            await db_monitor._drain_main_agent_request_queue()
        finally:
            _chat_lock.release()

    asyncio.new_event_loop().run_until_complete(busy_and_drain())

    assert q.peek() == "[子名] 测试消息"
    q.pop()


def test_drain_consumes_when_main_agent_idle(monkeypatch):
    """主 Agent 闲时消费队列 + 推 SSE。"""
    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass
    q.push("[子名] 测试消息")

    pushed = []

    def fake_notify(msg_id, role, content, source="electron"):
        pushed.append((role, content, source))

    monkeypatch.setattr("niu_api.chat.notify_new_message_sync", fake_notify)

    asyncio.new_event_loop().run_until_complete(db_monitor._drain_main_agent_request_queue())

    assert len(pushed) == 1
    assert pushed[0][0] == "subagent_msg"
    assert pushed[0][1] == "[子名] 测试消息"
    assert pushed[0][2] == "subagent"
    assert q.is_empty()


def test_drain_skipped_when_queue_empty():
    """队列空时不推 SSE。"""
    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass

    with mock.patch("niu_api.chat.notify_new_message_sync") as fake_notify:
        asyncio.new_event_loop().run_until_complete(db_monitor._drain_main_agent_request_queue())
        fake_notify.assert_not_called()
