"""lightrag_manager LLM 门控 flag 单测：进程启动默认 True（lifespan 显式置 False）。

2026-11-02：启动期 response_format 后台探测已移除（能力探测改由设置页保存链路
触发），本 flag 的读者只剩 /api/llm-status——原先围绕
_trigger_background_probe_if_needed 早返/恢复的两个用例随之删除。"""
from niu_api.internal.lightrag_manager import _llm_gate_ready


def test_flag_default_true():
    """进程启动默认 True（lifespan 显式 set False 前不误拦正常路径）"""
    assert _llm_gate_ready is True
