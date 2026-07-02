"""SubagentSupplementQueue 单元测试。"""
import threading
from agent.subagent_supplement import SubagentSupplementQueue, SubagentSupplementItem


def test_push_and_drain():
    q = SubagentSupplementQueue("file-processor-a1b2")
    q.push("补充内容1")
    q.push("补充内容2", is_terminate=True, sender="主Agent")
    items = q.drain()
    assert len(items) == 2
    assert items[0].content == "补充内容1"
    assert items[0].is_terminate is False
    assert items[0].sender == "主Agent"  # 默认 sender
    assert items[1].content == "补充内容2"
    assert items[1].is_terminate is True
    assert items[1].sender == "主Agent"


def test_drain_empty():
    q = SubagentSupplementQueue("test")
    items = q.drain()
    assert items == []


def test_drain_consumes_all():
    q = SubagentSupplementQueue("test")
    q.push("a")
    q.push("b")
    q.push("c")
    first = q.drain()
    assert len(first) == 3
    second = q.drain()
    assert second == []


def test_push_thread_safety():
    """多线程同时 push，drain 应拿到全部。"""
    q = SubagentSupplementQueue("test")

    def producer(n):
        for i in range(100):
            q.push(f"msg-{n}-{i}")

    threads = [threading.Thread(target=producer, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    items = q.drain()
    assert len(items) == 500


def test_unique_name():
    q = SubagentSupplementQueue("file-processor-a1b2")
    assert q.unique_name == "file-processor-a1b2"
