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


def test_thread_safe_push_pop():
    """多线程并发 push/pop 不抛异常。"""
    q = MainAgentRequestQueue()
    errors = []

    def producer():
        try:
            for i in range(100):
                q.push(f"[子名-{i:04d}] 内容 {i}")
        except Exception as e:
            errors.append(e)

    def consumer():
        try:
            for _ in range(100):
                q.pop()
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=producer)
    t2 = threading.Thread(target=consumer)
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert errors == []


def test_peek_does_not_remove():
    """peek 查看队首但不移除。"""
    q = MainAgentRequestQueue()
    q.push("[子名] 内容")

    assert q.peek() == "[子名] 内容"
    assert not q.is_empty()  # 没移除
    assert q.pop() == "[子名] 内容"
