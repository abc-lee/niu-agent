# tests/test_compression_extract_gate.py
"""提炼前置：压缩前 F1 欠账提炼完成才放行压实。"""
from unittest.mock import patch, MagicMock
from agent.generic.agent_loop import _extract_f1_before_compress


def test_no_f1_arrears_returns_true_without_calling_extractor():
    """F1 无未提炼内容 → 提炼前置通过，不调 extractor。"""
    ctx = MagicMock()
    with patch("agent.generic.agent_loop._f1_has_arrears", return_value=False) as m_has, \
         patch("agent.generic.agent_loop._call_extractor_sync") as m_ext:
        ok = _extract_f1_before_compress([], ctx)
    assert ok is True
    m_has.assert_called_once()
    m_ext.assert_not_called()


def test_f1_arrears_calls_extractor_and_returns_true_on_success():
    """F1 有欠账 → 调 extractor → 三守卫通过 → 剪 F1 → True。"""
    ctx = MagicMock()
    ctx._llm_config = {"model": "test"}
    with patch("agent.generic.agent_loop._f1_has_arrears", return_value=True), \
         patch("agent.generic.agent_loop._call_extractor_sync", return_value="ok"), \
         patch("agent.generic.agent_loop._extractor_guards_pass", return_value=True) as m_guard, \
         patch("agent.generic.agent_loop._relay_cut_f1") as m_cut:
        ok = _extract_f1_before_compress([], ctx)
    assert ok is True
    m_guard.assert_called_once()
    m_cut.assert_called_once()


def test_f1_extractor_failure_returns_false_no_cut():
    """extractor 失败 → 守卫不过 → 不剪 F1 → False（调用方不压实）。"""
    ctx = MagicMock()
    ctx._llm_config = {"model": "test"}
    with patch("agent.generic.agent_loop._f1_has_arrears", return_value=True), \
         patch("agent.generic.agent_loop._call_extractor_sync", return_value="[溢出] agent ..."), \
         patch("agent.generic.agent_loop._extractor_guards_pass", return_value=False), \
         patch("agent.generic.agent_loop._relay_cut_f1") as m_cut:
        ok = _extract_f1_before_compress([], ctx)
    assert ok is False
    m_cut.assert_not_called()
