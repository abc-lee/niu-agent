"""Tests for stop flag mechanism."""
import threading
import time
from agent.runner import request_stop, clear_stop, is_stop_requested


def test_initial_state_is_not_stopped():
    """Stop flag should be clear initially."""
    clear_stop()
    assert is_stop_requested() is False


def test_request_stop_sets_flag():
    """request_stop() should set the flag."""
    clear_stop()
    request_stop()
    assert is_stop_requested() is True


def test_clear_stop_resets_flag():
    """clear_stop() should reset the flag."""
    request_stop()
    clear_stop()
    assert is_stop_requested() is False


def test_stop_flag_is_thread_safe():
    """Stop flag should be thread-safe."""
    clear_stop()
    results = []

    def set_flag():
        time.sleep(0.01)
        request_stop()
        results.append("set")

    t = threading.Thread(target=set_flag)
    t.start()
    # Spin until flag is set
    while not is_stop_requested():
        time.sleep(0.001)
    results.append("seen")
    t.join()
    assert results == ["set", "seen"]
