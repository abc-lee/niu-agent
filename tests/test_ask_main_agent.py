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
    """注销后 future 不再可路由。"""
    reg = PendingAskRegistry()
    f = reg.register("file-processor-a1b2")
    reg.unregister("file-processor-a1b2")

    # 注销后 set_answer 不抛异常，但 future 永远拿不到（已不在 dict）
    reg.set_answer("file-processor-a1b2", "回答")
    assert f.wait(timeout=0.1) is None  # 超时，没拿到


def test_registry_cancel_missing_unique_name_no_error():
    """cancel 不存在的 unique_name 不抛异常（异步子 Agent 可能没问主就崩溃）。"""
    reg = PendingAskRegistry()
    reg.cancel_pending_ask("nonexistent-name")  # 不抛异常
