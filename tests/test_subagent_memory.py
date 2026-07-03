import threading
import time
from agent.subagent_memory import SubagentMemoryContext


def test_snapshot_returns_consistent_state():
    """snapshot 一次性拷贝，主 Agent 读到一致状态（不会 current_turn=5 但 last_llm_response 还是 turn 4）。"""
    ctx = SubagentMemoryContext()
    ctx.update(last_llm_request="req-turn-3", last_llm_response="resp-turn-3", current_turn=3, last_tool_name="read")

    snap = ctx.snapshot()
    assert snap["last_llm_request"] == "req-turn-3"
    assert snap["last_llm_response"] == "resp-turn-3"
    assert snap["current_turn"] == 3
    assert snap["last_tool_name"] == "read"


def test_update_modifies_fields():
    ctx = SubagentMemoryContext()
    ctx.update(current_turn=1, last_llm_response="hello")
    assert ctx.snapshot()["current_turn"] == 1
    assert ctx.snapshot()["last_llm_response"] == "hello"
    # 未更新的字段保持 None
    assert ctx.snapshot()["last_llm_request"] is None


def test_snapshot_is_copy_not_reference():
    """snapshot 返回的 dict 修改不影响内部状态。"""
    ctx = SubagentMemoryContext()
    ctx.update(current_turn=5)
    snap = ctx.snapshot()
    snap["current_turn"] = 999
    assert ctx.snapshot()["current_turn"] == 5


def test_concurrent_update_and_snapshot_thread_safe():
    """多线程并发 update + snapshot 不抛异常。"""
    ctx = SubagentMemoryContext()
    errors = []

    def updater():
        try:
            for i in range(100):
                ctx.update(current_turn=i, last_llm_response=f"r{i}")
        except Exception as e:
            errors.append(e)

    def snapshotter():
        try:
            for _ in range(100):
                ctx.snapshot()
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=updater)
    t2 = threading.Thread(target=snapshotter)
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert errors == []
