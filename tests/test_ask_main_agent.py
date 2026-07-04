import threading
import time
from agent.ask_main_agent import AskMainAgentFuture, PendingAskRegistry, TERMINATED_SIGNAL


def test_future_wait_blocks_until_set_answer():
    """future.wait() 阻塞，set_answer 后解除。"""
    future = AskMainAgentFuture()

    result = {}
    def waiter():
        result["answer"] = future.wait(timeout=2.0)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)  # 确保 waiter 已进入 wait
    assert not result  # 还没结果

    future.set_answer("这是主 Agent 的回答")
    t.join(timeout=2.0)

    assert result["answer"] == "这是主 Agent 的回答"


def test_future_wait_timeout_returns_none():
    """超时返回 None。"""
    future = AskMainAgentFuture()
    result = future.wait(timeout=0.05)
    assert result is None


def test_registry_register_and_set_answer():
    """注册 future 后，set_answer 按 unique_name 路由到正确 future。"""
    reg = PendingAskRegistry()
    f1 = reg.register("file-processor-a1b2")
    f2 = reg.register("context-manager-c3d4")

    reg.set_answer("file-processor-a1b2", "回答 1")
    reg.set_answer("context-manager-c3d4", "回答 2")

    assert f1.wait(timeout=1.0) == "回答 1"
    assert f2.wait(timeout=1.0) == "回答 2"


def test_registry_cancel_pending_ask():
    """cancel_pending_ask 给 future 设 'terminated' 信号，工具返回终止状态。"""
    reg = PendingAskRegistry()
    f = reg.register("file-processor-a1b2")

    reg.cancel_pending_ask("file-processor-a1b2")

    answer = f.wait(timeout=1.0)
    assert answer == TERMINATED_SIGNAL


def test_registry_unregister_removes_future():
    """注销后 future 不再可路由，set_answer 返回 False（孤儿回答路径）。"""
    reg = PendingAskRegistry()
    f = reg.register("file-processor-a1b2")
    reg.unregister("file-processor-a1b2")

    # 注销后 set_answer 返回 False（找不到 future）
    found = reg.set_answer("file-processor-a1b2", "回答")
    assert found is False
    # future 永远拿不到（已不在 dict）
    assert f.wait(timeout=0.1) is None


def test_registry_cancel_missing_unique_name_no_error():
    """cancel 不存在的 unique_name 不抛异常（异步子 Agent 可能没问主就崩溃）。"""
    reg = PendingAskRegistry()
    reg.cancel_pending_ask("nonexistent-name")  # 不抛异常


def test_registry_register_duplicate_unique_name_terminates_old_future():
    """register 同一 unique_name 两次，旧 future 收到 TERMINATED_SIGNAL 解除阻塞，避免泄漏。"""
    reg = PendingAskRegistry()
    f1 = reg.register("file-processor-a1b2")
    f2 = reg.register("file-processor-a1b2")  # 重复注册

    # 旧 future 应被设 TERMINATED_SIGNAL
    assert f1.wait(timeout=1.0) == TERMINATED_SIGNAL

    # 新 future 还在等（没被解除）
    assert f2.wait(timeout=0.1) is None

    # set_answer 路由到新 future
    reg.set_answer("file-processor-a1b2", "回答")
    assert f2.wait(timeout=1.0) == "回答"


def test_ask_main_agent_tool_returns_answer():
    """_ask_main_agent_impl：注册 future → 推 MainAgentRequestQueue → 阻塞 → set_answer 后返回回答。

    @niu content 拦截路径下，agent_loop._intercept_at_prefix_content 检测到 @niu 前缀后
    直接调本函数（同步，无 MCP 工具派发）。
    """
    from agent.subagent import _ask_main_agent_impl
    from agent.ask_main_agent import get_pending_ask_registry
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue
    from agent.main_agent_request_queue import get_main_agent_request_queue
    import threading
    import time

    # 清空队列
    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass

    sq = SubagentSupplementQueue("test-ask-0001")
    name = SubagentRegistry.register("test-ask", supplement_queue=sq, is_sync=False)

    try:
        # 在另一个线程模拟主 Agent 回答（0.5 秒后）
        def answer_later():
            time.sleep(0.5)
            get_pending_ask_registry().set_answer(name, "这是主 Agent 的回答")

        t = threading.Thread(target=answer_later)
        t.start()

        # 调 _ask_main_agent_impl（阻塞 0.5 秒后拿到回答）
        result = _ask_main_agent_impl("这是问题", unique_name=name)

        t.join()

        assert "这是主 Agent 的回答" in result

        # 验证消息推入了 MainAgentRequestQueue（content 格式 "[子名] 问题"）
        queued = q.pop()
        assert queued is not None
        assert name in queued
        assert "这是问题" in queued
    finally:
        SubagentRegistry.unregister(name)


def test_ask_main_agent_tool_terminated_returns_terminated_status():
    """_ask_main_agent_impl 被 cancel 时返回 terminated 状态 + 设置 _ask_terminated 标记。"""
    from agent.subagent import _ask_main_agent_impl
    from agent.ask_main_agent import get_pending_ask_registry, TERMINATED_SIGNAL
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue
    from agent.main_agent_request_queue import get_main_agent_request_queue
    import threading
    import time

    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass

    sq = SubagentSupplementQueue("test-cancel-0001")
    name = SubagentRegistry.register("test-cancel", supplement_queue=sq, is_sync=False)

    try:
        # 在另一个线程模拟 /stop（cancel_pending_ask）
        def cancel_later():
            time.sleep(0.5)
            get_pending_ask_registry().cancel_pending_ask(name)

        t = threading.Thread(target=cancel_later)
        t.start()

        result = _ask_main_agent_impl("这是问题", unique_name=name)

        t.join()

        assert "terminated" in result.lower() or "终止" in result

        # 验证 _ask_terminated 标记已设置
        instance = SubagentRegistry.get(name)
        assert instance is not None
        assert getattr(instance, "_ask_terminated", False) is True
    finally:
        SubagentRegistry.unregister(name)


def test_ask_main_agent_after_cancel_does_not_deadlock():
    """cancel 后 LLM 又触发 @niu 拦截不死锁——直接返回 terminated 状态（_ask_terminated 标记）。

    场景：子 Agent @niu 拦截被 cancel → _ask_main_agent_impl 返回 terminated → LLM 没走终止总结
    反而又输出 @niu content → _intercept_at_prefix_content 再次调 _ask_main_agent_impl
    → 应直接返回 terminated 不阻塞（否则 /stop 在 queue 但子 Agent 阻塞在
    _ask_main_agent_impl 不会 drain → 死锁）
    """
    from agent.subagent import _ask_main_agent_impl
    from agent.ask_main_agent import get_pending_ask_registry
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue
    from agent.main_agent_request_queue import get_main_agent_request_queue
    import threading
    import time

    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass

    sq = SubagentSupplementQueue("test-reask-0001")
    name = SubagentRegistry.register("test-reask", supplement_queue=sq, is_sync=False)

    try:
        # 第一次 ask_main_agent，0.5 秒后 cancel
        def cancel_later():
            time.sleep(0.5)
            get_pending_ask_registry().cancel_pending_ask(name)

        t1 = threading.Thread(target=cancel_later)
        t1.start()
        result1 = _ask_main_agent_impl("第一次问题", unique_name=name)
        t1.join()
        assert "终止" in result1

        # 第二次 ask_main_agent——应立即返回 terminated 不阻塞（_ask_terminated 标记）
        start = time.time()
        result2 = _ask_main_agent_impl("第二次问题", unique_name=name)
        elapsed = time.time() - start

        # 应在 1 秒内返回（不阻塞 300 秒）
        assert elapsed < 1.0, f"第二次 ask_main_agent 应立即返回，实际耗时 {elapsed}"
        assert "终止" in result2
    finally:
        SubagentRegistry.unregister(name)


def test_cancel_pending_ask_sets_terminated_flag_when_no_future():
    """cancel_pending_ask 在 future 不存在时也设置 _ask_terminated 标记。

    场景：/stop 在 _ask_main_agent_impl 检查标记与 register 之间到达，
    cancel_pending_ask 找不到 future，但应设置 instance._ask_terminated 标记，
    让后续 _ask_main_agent_impl 调用立即短路，避免阻塞满 300s。
    """
    from agent.ask_main_agent import get_pending_ask_registry
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue("test-cancel-flag-0001")
    name = SubagentRegistry.register("test-cancel-flag", supplement_queue=sq, is_sync=False)

    try:
        reg = get_pending_ask_registry()
        # 不注册 future（子 Agent 没在问主），直接 cancel
        reg.cancel_pending_ask(name)

        # instance._ask_terminated 应被设置
        instance = SubagentRegistry.get(name)
        assert instance is not None
        assert getattr(instance, "_ask_terminated", False) is True
    finally:
        SubagentRegistry.unregister(name)
