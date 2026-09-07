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
    assert consume_compression() == (False, "")  # 消费后清除

def test_repeat_request_overwrites_reason():
    reset_compression_intent()
    request_compression("auto")
    request_compression("manual")
    assert consume_compression() == (True, "manual")

def test_thread_safety():
    reset_compression_intent()
    errors = []
    def worker():
        try:
            for _ in range(100):
                request_compression("auto")
                consume_compression()
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
