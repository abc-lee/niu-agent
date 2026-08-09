"""会话日期链补链：纯函数规划器测试。零 mock，符合项目风格。"""
from datetime import date

from agent.runner import _build_session_chain_ops


def test_window_cutoff_inclusive_today_minus_9():
    """窗口 = 含今天最近 10 个日历天：today-9 含、today-10 不含。"""
    # 构造：实体名列表（日期后缀会话）
    names = ["2026-07-30会话", "2026-07-31会话", "2026-08-09会话"]
    deletes, creates = _build_session_chain_ops(names, {}, today=date(2026, 8, 9))
    # 窗口 = >= 2026-07-31：2026-07-30 排除
    assert "2026-07-30会话" not in [c[0] for c in creates] + [d[0] for d in deletes]
    # today-9（2026-07-31）必须入窗：相邻补边 2026-07-31→2026-08-09 生效
    assert creates == [("2026-07-31会话", "2026-08-09会话")]


def test_create_adjacent_edges():
    names = ["2026-08-07会话", "2026-08-09会话"]  # 8-8 缺失
    deletes, creates = _build_session_chain_ops(names, {}, today=date(2026, 8, 9))
    assert deletes == []
    assert creates == [("2026-08-07会话", "2026-08-09会话")]


def test_existing_edge_skipped():
    names = ["2026-08-07会话", "2026-08-09会话"]
    existing = {("2026-08-07会话", "2026-08-09会话"): {"followed_by"}}
    deletes, creates = _build_session_chain_ops(names, existing, today=date(2026, 8, 9))
    assert creates == []


def test_break_spanning_edge_when_middle_appears():
    """中间日期实体出现：跨越边 8-7→8-9 断开，重建 8-7→8-8、8-8→8-9。"""
    names = ["2026-08-07会话", "2026-08-08会话", "2026-08-09会话"]
    existing = {
        ("2026-08-07会话", "2026-08-09会话"): {"followed_by"},  # 旧跨越边
        ("2026-08-07会话", "2026-08-08会话"): {"followed_by"},
    }
    deletes, creates = _build_session_chain_ops(names, existing, today=date(2026, 8, 9))
    assert deletes == [("2026-08-07会话", "2026-08-09会话")]
    assert creates == [("2026-08-08会话", "2026-08-09会话")]


def test_spanning_edge_kept_when_middle_missing():
    """缺失日长边保留：8-7→8-9 无中间实体不删。"""
    names = ["2026-08-07会话", "2026-08-09会话"]
    existing = {("2026-08-07会话", "2026-08-09会话"): {"followed_by"}}
    deletes, creates = _build_session_chain_ops(names, existing, today=date(2026, 8, 9))
    assert deletes == []
    assert creates == []


def test_spanning_edge_not_broken_when_other_keywords():
    """断边安全：两实体间含非 followed_by 边时不删（避免误删语义边）。"""
    names = ["2026-08-07会话", "2026-08-08会话", "2026-08-09会话"]
    existing = {
        ("2026-08-07会话", "2026-08-09会话"): {"followed_by", "related_to"},
    }
    deletes, creates = _build_session_chain_ops(names, existing, today=date(2026, 8, 9))
    assert deletes == []


def test_empty_and_single():
    assert _build_session_chain_ops([], {}, today=date(2026, 8, 9)) == ([], [])
    assert _build_session_chain_ops(["2026-08-09会话"], {}, today=date(2026, 8, 9)) == ([], [])
