"""验证 db_monitor 链路 A：主 Agent 闲置检测 + MainAgentRequestQueue 消费 + 推 SSE。"""
import asyncio
import unittest.mock as mock

from niu_api import db_monitor
from agent.main_agent_request_queue import get_main_agent_request_queue
from agent.subagent_registry import SubagentRegistry
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
    """主 Agent 闲时消费队列 + 推 SSE。

    阶段二 D1：db_monitor 推 SSE 前会检查 content 前缀 [unique_name] 对应的子 Agent
    是否仍在注册表。不在则丢弃。所以这里需要先注册一个 instance 才能 push + drain。
    """
    # 先注册一个子 Agent 到 SubagentRegistry（unique_name="test_drain_sub"）
    from agent.subagent_supplement import SubagentSupplementQueue
    sq = SubagentSupplementQueue(unique_name="")
    unique_name = SubagentRegistry.register(
        agent_type="test-agent",
        supplement_queue=sq,
        memory_context=None,
        is_sync=True,
        task="test",
    )
    sq.unique_name = unique_name
    try:
        q = get_main_agent_request_queue()
        while q.pop() is not None:
            pass
        q.push(f"[{unique_name}] 测试消息")

        pushed = []

        def fake_notify(msg_id, role, content, source="electron"):
            pushed.append((role, content, source))
            return True  # 阶段二 C1：返回 True 表示推送成功

        monkeypatch.setattr("niu_api.chat.notify_new_message_sync", fake_notify)

        asyncio.new_event_loop().run_until_complete(db_monitor._drain_main_agent_request_queue())

        assert len(pushed) == 1
        assert pushed[0][0] == "subagent_msg"
        assert pushed[0][1] == f"[{unique_name}] 测试消息"
        assert pushed[0][2] == "subagent"
        assert q.is_empty()
    finally:
        SubagentRegistry.unregister(unique_name)


def test_drain_drops_message_when_subagent_unregistered(monkeypatch):
    """阶段二 D1：子 Agent 已注销时丢弃队列中的 ask 请求，不推 SSE。"""
    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass
    # 用一个未注册的 unique_name 推队列（ask 请求格式，非完成通知）
    q.push("[unregistered_sub] 这是一条 ask 请求")

    with mock.patch("niu_api.chat.notify_new_message_sync") as fake_notify:
        asyncio.new_event_loop().run_until_complete(db_monitor._drain_main_agent_request_queue())
        fake_notify.assert_not_called()

    # 消息应已被 pop 丢弃
    assert q.is_empty()


def test_drain_keeps_completion_notification_when_subagent_unregistered(monkeypatch):
    """Critical-1：完成通知（已完成/异常结束/被取消）即使子 Agent 已注销也不丢弃。

    场景：_run_subagent_async 完成 → push "[子名] 已完成" 到队列 →
    finally 立即 SubagentRegistry.unregister → db_monitor 200ms 后才 _drain。
    此时注册表查不到子 Agent，但完成通知必须推给主 Agent，否则主 Agent 永远收不到。
    """
    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass
    # 子 Agent 不在注册表（模拟 finally unregister 后的场景）
    q.push("[completed_sub] 已完成：文件处理结束")

    pushed = []

    def fake_notify(msg_id, role, content, source="electron"):
        pushed.append((role, content, source))
        return True

    monkeypatch.setattr("niu_api.chat.notify_new_message_sync", fake_notify)

    asyncio.new_event_loop().run_until_complete(db_monitor._drain_main_agent_request_queue())

    # 完成通知应被推 SSE，不丢弃
    assert len(pushed) == 1
    assert pushed[0][0] == "subagent_msg"
    assert pushed[0][1] == "[completed_sub] 已完成：文件处理结束"
    assert pushed[0][2] == "subagent"
    assert q.is_empty()


def test_drain_keeps_terminated_notification_when_subagent_unregistered(monkeypatch):
    """Critical-1：异常结束/被取消通知即使子 Agent 已注销也不丢弃。

    _drain 每次只消费队列头部一条消息，所以这里分两次 drain 验证两种通知。
    """
    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass
    q.push("[cancelled_sub] 异常结束：LLM 调用失败")

    pushed = []

    def fake_notify(msg_id, role, content, source="electron"):
        pushed.append(content)
        return True

    monkeypatch.setattr("niu_api.chat.notify_new_message_sync", fake_notify)

    asyncio.new_event_loop().run_until_complete(db_monitor._drain_main_agent_request_queue())

    assert len(pushed) == 1
    assert pushed[0] == "[cancelled_sub] 异常结束：LLM 调用失败"
    assert q.is_empty()

    # 第二条：被取消
    q.push("[cancelled_sub2] 被取消：用户主动停止")
    asyncio.new_event_loop().run_until_complete(db_monitor._drain_main_agent_request_queue())

    assert len(pushed) == 2
    assert pushed[1] == "[cancelled_sub2] 被取消：用户主动停止"
    assert q.is_empty()


def test_drain_skipped_when_notify_returns_false(monkeypatch):
    """阶段二 C1：notify_new_message_sync 返回 False 时，消息留队列下次重试。"""
    from agent.subagent_supplement import SubagentSupplementQueue
    sq = SubagentSupplementQueue(unique_name="")
    unique_name = SubagentRegistry.register(
        agent_type="test-agent",
        supplement_queue=sq,
        memory_context=None,
        is_sync=True,
        task="test",
    )
    sq.unique_name = unique_name
    try:
        q = get_main_agent_request_queue()
        while q.pop() is not None:
            pass
        q.push(f"[{unique_name}] 测试消息")

        # 模拟主 loop 不可用导致 notify 返回 False
        monkeypatch.setattr(
            "niu_api.chat.notify_new_message_sync",
            lambda *args, **kwargs: False,
        )

        asyncio.new_event_loop().run_until_complete(db_monitor._drain_main_agent_request_queue())

        # 消息应留队列（未被 pop）
        assert not q.is_empty()
        assert q.peek() == f"[{unique_name}] 测试消息"
    finally:
        SubagentRegistry.unregister(unique_name)
        # 清理队列避免影响其他测试
        while q.pop() is not None:
            pass


def test_drain_skipped_when_queue_empty():
    """队列空时不推 SSE。"""
    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass

    with mock.patch("niu_api.chat.notify_new_message_sync") as fake_notify:
        asyncio.new_event_loop().run_until_complete(db_monitor._drain_main_agent_request_queue())
        fake_notify.assert_not_called()
