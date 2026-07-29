import threading

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
    """多线程并发 update + snapshot 不抛异常，且 snapshot 不出现"撕裂"（current_turn 和 last_llm_response 必属同一轮）。

    注意：仅断言不抛异常是假测试——Python threading.Lock 本就不抛异常，去掉锁也能通过。
    真正的验证是"撕裂检测"：snapshot 读到的 current_turn 和 last_llm_response 必属同一轮。
    无锁实现下 update 的两次 setattr 之间可能被 snapshot 打断，读到撕裂状态。

    循环次数选择 100000：实测 CPython GIL 下 100 次循环无法可靠触发 GIL 切换（撕不到），
    10000 次循环无锁实现概率性出现撕裂（~30% 失败率），100000 次循环无锁实现稳定出现 ~20000+ 次撕裂读，
    有锁实现 0 撕裂。
    """
    ctx = SubagentMemoryContext()
    errors = []

    def updater():
        try:
            for i in range(100000):
                # 同时更新两个字段，无锁时可能被 snapshot 打断读到撕裂状态
                ctx.update(current_turn=i, last_llm_response=f"r{i}")
        except Exception as e:
            errors.append(e)

    def snapshotter():
        try:
            for _ in range(100000):
                snap = ctx.snapshot()
                # 撕裂检测：current_turn 和 last_llm_response 必属同一轮
                i = snap["current_turn"]
                if i > 0:
                    expected_resp = f"r{i}"
                    actual_resp = snap["last_llm_response"]
                    if actual_resp != expected_resp:
                        errors.append(f"torn read: turn={i}, resp={actual_resp}")
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=updater)
    t2 = threading.Thread(target=snapshotter)
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert errors == [], f"并发测试发现错误：{errors}"
