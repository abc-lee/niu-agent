"""压缩 task prompt 瘦身测试：去方法论重复、禁止报告、保留输出契约与参数。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from niu_api.compat import _build_force_prompt, _build_mode2_prompt


def _mode2(**kw):
    defaults = {
        "display_tokens": 100000, "compress_target_tokens": 60000,
        "usage_percent": 80.0, "compress_history": [{"role": "user", "content": "[idx:1] 100tokens hi"}],
    }
    defaults.update(kw)
    return _build_mode2_prompt(**defaults)


def _force(**kw):
    defaults = {
        "display_tokens": 100000, "compress_target_tokens": 60000,
        "usage_percent": 80.0, "force_history": [{"role": "user", "content": "[idx:1] 100tokens hi"}],
        "last_compress_id": None, "dream_idx_in_force": 0,
    }
    defaults.update(kw)
    return _build_force_prompt(**defaults)


def test_mode2_removes_methodology_duplication():
    """方法论（三份划分/会话单元/级联规则）不再内联重复——system 已有。"""
    p = _mode2()
    assert "压缩方法论" not in p
    assert "划分优先级" not in p
    assert "会话单元" not in p
    assert "工具输出随父 assistant" not in p
    assert "旧摘要" not in p


def test_mode2_forbids_analysis_report():
    """禁止 <analysis>/报告/解释——旧版强制'先写 analysis'已移除。"""
    p = _mode2()
    assert "先在 <analysis> 块里写分析过程" not in p
    assert "禁止输出任何其他内容" in p
    assert "禁止 <analysis>" in p


def test_mode2_keeps_output_contract_and_params():
    """保留两行输出契约 + 任务参数插值。"""
    p = _mode2()
    assert "keep=" in p
    assert "update=" in p
    assert "分号" in p or "；" in p
    assert "100000" in p and "60000" in p and "40000" in p  # 当前/目标/需释放
    assert "消息数：1" in p  # 消息数插值精确断言（原宽松断言≈没测）


def test_force_keeps_cursor_contract():
    """force 保留三行契约（含 cursor=）。"""
    p = _force()
    assert "cursor=" in p
    assert "keep=" in p
    assert "update=" in p


def test_force_keeps_dream_boundary_and_cursor_info():
    """force 保留 dream 安全边界与上次压缩游标两行。"""
    p = _force(dream_idx_in_force=42, last_compress_id="abc-123")
    assert "42" in p
    assert "dream" in p.lower() or "未提取知识" in p
    assert "abc-123" in p
    p2 = _force(dream_idx_in_force=0, last_compress_id=None)
    assert "（无，从最早消息开始）" in p2


def test_force_forbids_analysis():
    """force 同样禁止报告（旧强制 analysis 块已移除）。"""
    p = _force()
    assert "先在 <analysis> 块里写分析过程" not in p
    assert "禁止输出任何其他内容" in p
