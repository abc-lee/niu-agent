"""lightrag_manager LLM 门控 flag 单测：进程启动默认 True（lifespan 显式置 False）。

2026-11-02：启动期 response_format 后台探测已移除（能力探测改由设置页保存链路
触发），本 flag 的读者只剩 /api/llm-status——原先围绕
_trigger_background_probe_if_needed 早返/恢复的两个用例随之删除。
现补 flag 往返用例：set False → get False、恢复 True → get True。"""
from niu_api.internal.lightrag_manager import (
    _llm_gate_ready,
    get_llm_gate_ready,
    set_llm_gate_ready,
)


def test_flag_default_true():
    """进程启动默认 True（lifespan 显式 set False 前不误拦正常路径）"""
    assert _llm_gate_ready is True


def test_flag_set_false():
    """set_llm_gate_ready(False) 后 get_llm_gate_ready() 为 False（lifespan 启动期门控）"""
    set_llm_gate_ready(False)
    try:
        assert get_llm_gate_ready() is False
    finally:
        set_llm_gate_ready(True)


def test_flag_restore_true():
    """恢复 set_llm_gate_ready(True) 后 get_llm_gate_ready() 为 True（探测结束后放行）"""
    set_llm_gate_ready(False)
    set_llm_gate_ready(True)
    assert get_llm_gate_ready() is True
