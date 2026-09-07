# tests/test_compression_intent.py
import threading
from agent.compression_intent import (
    request_compression, consume_compression, peek_compression, reset_compression_intent,
)

def test_request_consume_roundtrip():
    reset_compression_intent()
    assert consume_compression() == (False, "")
    request_compression("manual")
    assert peek_compression() is True
    assert consume_compression() == (True, "manual")
    assert peek_compression() is False  # 消费后无残留意图
    assert consume_compression() == (False, "")  # 消费后清除

def test_repeat_request_overwrites_reason():
    reset_compression_intent()
    request_compression("auto")
    request_compression("manual")
    assert consume_compression() == (True, "manual")

def test_thread_safety():
    # 并发 request 风暴（只写不读）：验证跨线程可见性 + 锁不挂，
    # 终态断言——最后一次 request 被主线程消费、随后无残留意图。
    reset_compression_intent()
    errors = []
    def worker():
        try:
            for _ in range(100):
                request_compression("auto")
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert consume_compression() == (True, "auto")  # 最后一次 request 可见且被消费
    assert consume_compression() == (False, "")     # 无残留意图
