"""验证 db_monitor.route_message：
1. 主 Agent 回答消息（@子名 来自主Agent）路由到 PendingAskRegistry.set_answer
2. /stop 消息路由时同时 cancel_pending_ask 解除 ask_main_agent 阻塞
3. 主 Agent 补充上下文（无 pending future）降级推 supplement queue
4. 孤儿回答（子 Agent 已退出）丢弃不推回主 Agent
5. 普通补充消息（其他 sender）推 supplement queue
"""
from agent.ask_main_agent import TERMINATED_SIGNAL, get_pending_ask_registry
from agent.subagent_registry import SubagentRegistry
from agent.subagent_supplement import SubagentSupplementQueue
from niu_api import db_monitor


def test_route_message_main_answer_to_pending_ask():
    """主 Agent 回答 (@子名 内容) 来自主Agent → 路由到 PendingAskRegistry.set_answer。"""
    sq = SubagentSupplementQueue("test-route-0001")
    name = SubagentRegistry.register("test-route", supplement_queue=sq, is_sync=False)

    assert sq.drain() == []

    try:
        reg = get_pending_ask_registry()
        future = reg.register(name)

        db_monitor.route_message(target=name, sender="主Agent", content="这是回答")

        answer = future.wait(timeout=1.0)
        assert answer == "这是回答"

        assert sq.drain() == []
    finally:
        SubagentRegistry.unregister(name)


def test_route_message_stop_cancels_pending_ask():
    """/stop 消息路由时同时 cancel_pending_ask，避免 ask_main_agent 死锁。"""
    sq = SubagentSupplementQueue("test-stop-0001")
    name = SubagentRegistry.register("test-stop", supplement_queue=sq, is_sync=False)

    try:
        reg = get_pending_ask_registry()
        future = reg.register(name)

        db_monitor.route_message(target=name, sender="主Agent", content="/stop")

        answer = future.wait(timeout=1.0)
        assert answer == TERMINATED_SIGNAL

        items = sq.drain()
        assert len(items) == 1
        assert items[0].is_terminate is True
        assert items[0].content == "/stop"
    finally:
        SubagentRegistry.unregister(name)


def test_route_message_main_supplement_when_no_pending_ask():
    """主 Agent 补充上下文（无 pending future）→ 降级推 supplement queue，不推回主 Agent。"""
    sq = SubagentSupplementQueue("test-supp-main-0001")
    name = SubagentRegistry.register("test-supp-main", supplement_queue=sq, is_sync=False)

    try:
        db_monitor.route_message(target=name, sender="主Agent", content="注意，文件路径改为 /tmp/x.pdf")

        items = sq.drain()
        assert len(items) == 1
        assert items[0].content == "注意，文件路径改为 /tmp/x.pdf"
        assert items[0].is_terminate is False
        assert items[0].sender == "主Agent"
    finally:
        SubagentRegistry.unregister(name)


def test_route_message_orphan_answer_dropped():
    """主 Agent 回答路由时子 Agent 已不在注册表（孤儿回答）→ sender==主Agent 时丢弃，不推回主 Agent 避免死循环。"""
    # 不注册子 Agent，直接路由（模拟子 Agent 已退出）
    # 不应抛异常，不应推回主 Agent supplement queue
    db_monitor.route_message(target="nonexistent-xxxx", sender="主Agent", content="回答")
    # 不抛异常即可


def test_route_message_normal_supplement_to_subagent():
    """普通补充消息（@子名 内容，不是 /stop 也不是 ask 回答）→ 推 supplement queue。"""
    sq = SubagentSupplementQueue("test-supp-0001")
    name = SubagentRegistry.register("test-supp", supplement_queue=sq, is_sync=False)

    try:
        db_monitor.route_message(target=name, sender="other-agent-aaaa", content="补充信息")

        items = sq.drain()
        assert len(items) == 1
        assert items[0].content == "补充信息"
        assert items[0].is_terminate is False
        assert items[0].sender == "other-agent-aaaa"
    finally:
        SubagentRegistry.unregister(name)
