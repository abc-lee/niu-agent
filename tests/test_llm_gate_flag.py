"""lightrag_manager LLM 门控 flag 单测：set_llm_gate_ready + probe 入口早返。

probe daemon 是 _trigger_background_probe_if_needed 内局部闭包 _probe_in_background
经 threading.Thread(target=..., daemon=True, name="response-format-probe")
启动——局部闭包不可 patch，模块级无 _probe_background_worker 符号；
patch 全局 threading.Thread 断言 .start 调用（lightrag_manager 模块级
import threading——函数内 Thread 调用经共享模块对象被拦截）。"""
from unittest.mock import patch

from niu_api.internal.lightrag_manager import _llm_gate_ready, set_llm_gate_ready


def test_flag_default_true():
    """进程启动默认 True（lifespan 显式 set False 前不误拦正常路径）"""
    assert _llm_gate_ready is True


def test_set_false_skips_probe():
    """set_llm_gate_ready(False) 后 probe 零线程 spawn（早返）"""
    from niu_api.internal.lightrag_manager import _trigger_background_probe_if_needed

    set_llm_gate_ready(False)
    try:
        with patch("niu_api.internal.lightrag_manager.threading.Thread") as mock_thread:
            _trigger_background_probe_if_needed()
        mock_thread.assert_not_called()
    finally:
        set_llm_gate_ready(True)


def test_flag_reset_restores_probe():
    """set_llm_gate_ready(True) 后 probe 恢复（新进程/重启语义）"""
    from niu_api.internal.lightrag_manager import _trigger_background_probe_if_needed

    set_llm_gate_ready(False)
    set_llm_gate_ready(True)
    try:
        with patch("niu_api.internal.lightrag_manager.threading.Thread") as mock_thread:
            _trigger_background_probe_if_needed()
        mock_thread.assert_called_once()
    finally:
        set_llm_gate_ready(True)
