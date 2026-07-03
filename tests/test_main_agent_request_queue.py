import threading
from agent.main_agent_request_queue import MainAgentRequestQueue


def test_push_and_pop_fifo():
    """push 后 pop 按 FIFO 顺序返回。"""
    q = MainAgentRequestQueue()
    q.push("[file-processor-a1b2] 问题 1")
    q.push("[file-processor-c3d4] 问题 2")

    assert q.pop() == "[file-processor-a1b2] 问题 1"
    assert q.pop() == "[file-processor-c3d4] 问题 2"
    assert q.pop() is None  # 队列空


def test_pop_empty_returns_none():
    """空队列 pop 返回 None，不阻塞。"""
    q = MainAgentRequestQueue()
    assert q.pop() is None


def test_is_empty():
    """is_empty 正确反映队列状态。"""
    q = MainAgentRequestQueue()
    assert q.is_empty()
    q.push("[子名] 内容")
    assert not q.is_empty()
    q.pop()
    assert q.is_empty()


def test_thread_safe_push_pop_data_integrity():
    """多线程并发 push/pop 验证数据完整性：无丢失、无重复、总数==100。

    只断言不抛异常是弱测试——queue.Queue 本就线程安全不抛异常。
    真正验证是数据完整性：producer push 100 条唯一 content，consumer 收集 pop 结果，
    最终断言无丢失（总数==100）、无重复（set 大小==100）。
    """
    q = MainAgentRequestQueue()
    errors = []
    NUM_ITEMS = 100
    expected = {f"[子名-{i:04d}] 内容 {i}" for i in range(NUM_ITEMS)}

    collected = []
    collected_lock = threading.Lock()

    def producer():
        try:
            for i in range(NUM_ITEMS):
                q.push(f"[子名-{i:04d}] 内容 {i}")
        except Exception as e:
            errors.append(e)

    def consumer():
        try:
            for _ in range(NUM_ITEMS):
                item = q.pop()
                if item is not None:
                    with collected_lock:
                        collected.append(item)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=producer)
    t2 = threading.Thread(target=consumer)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # consumer 可能比 producer 快，collected 不足 NUM_ITEMS 时补 pop 剩余
    while not q.is_empty():
        item = q.pop()
        if item is not None:
            collected.append(item)

    assert errors == [], f"并发测试发现错误：{errors}"
    assert len(collected) == NUM_ITEMS, f"丢失消息：collected {len(collected)}/{NUM_ITEMS}"
    assert set(collected) == expected, f"消息内容不一致（可能有重复或错乱）：{set(collected) ^ expected}"


def test_peek_does_not_remove():
    """peek 查看队首但不移除。"""
    q = MainAgentRequestQueue()
    q.push("[子名] 内容")

    assert q.peek() == "[子名] 内容"
    assert not q.is_empty()  # 没移除
    assert q.pop() == "[子名] 内容"
