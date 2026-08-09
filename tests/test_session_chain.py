"""会话日期链补链：纯函数规划器测试。零 mock，符合项目风格。"""
from datetime import date
from unittest.mock import MagicMock, patch

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


def test_ensure_session_chain_creates_and_breaks(monkeypatch):
    """集成：列举→断跨越边→批量补相邻边。mock adapter/ingester。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)

    adapter = MagicMock()
    adapter.list_entities_by_name_regex.return_value = {
        "status": "ok",
        "data": [
            {"entity_name": "2026-08-07会话"},
            {"entity_name": "2026-08-08会话"},
            {"entity_name": "2026-08-09会话"},
        ],
    }
    # 已有边：8-7→8-9 跨越（仅 followed_by）、8-7→8-8 相邻
    def fake_has_edge(src, tgt):
        return (src, tgt) in {
            ("2026-08-07会话", "2026-08-09会话"),
            ("2026-08-07会话", "2026-08-08会话"),
        }

    adapter.has_edge.side_effect = fake_has_edge
    adapter.get_edge_keywords_between.return_value = ["followed_by"]
    ingester = MagicMock()

    # _ensure_session_chain 内是局部 import：patch 真实解析源模块
    with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter", return_value=adapter), \
         patch("niu_api.internal.lightrag_adapter.LightRAGIngester", return_value=ingester), \
         patch("datetime.date") as mock_date:
        mock_date.today.return_value = date(2026, 8, 9)
        runner._ensure_session_chain()

    # 断 8-7→8-9
    adapter.delete_relation.assert_called_once_with("2026-08-07会话", "2026-08-09会话")
    # 批量建 8-8→8-9
    assert ingester.inject_custom_kg.call_count == 1
    rels = ingester.inject_custom_kg.call_args.kwargs["relationships"]
    assert rels == [{
        "src_id": "2026-08-08会话",
        "tgt_id": "2026-08-09会话",
        "keywords": "followed_by",
        "description": "2026-08-08会话 之后是 2026-08-09会话",
        "source_id": "nap_session_chain",
        "file_path": "nap_session_chain",
    }]


def test_ensure_session_chain_noop_when_no_entities(monkeypatch):
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    adapter = MagicMock()
    adapter.list_entities_by_name_regex.return_value = {"status": "ok", "data": []}
    ingester = MagicMock()
    with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter", return_value=adapter), \
         patch("niu_api.internal.lightrag_adapter.LightRAGIngester", return_value=ingester):
        runner._ensure_session_chain()  # 不应抛异常
    adapter.delete_relation.assert_not_called()
    ingester.inject_custom_kg.assert_not_called()


def test_ensure_session_chain_list_failure_returns_quietly(monkeypatch):
    """列举失败（status != ok）：warning + return，不调 delete/inject。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    adapter = MagicMock()
    adapter.list_entities_by_name_regex.return_value = {"status": "error", "message": "LightRAG not available"}
    ingester = MagicMock()
    with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter", return_value=adapter), \
         patch("niu_api.internal.lightrag_adapter.LightRAGIngester", return_value=ingester):
        runner._ensure_session_chain()  # 不应抛异常
    adapter.delete_relation.assert_not_called()
    ingester.inject_custom_kg.assert_not_called()


def test_ensure_session_chain_keeps_semantic_edge(monkeypatch):
    """两实体间含非 followed_by 语义边：delete_relation 不被调用（防误删）。"""
    from agent.runner import NiuRunner

    runner = NiuRunner.__new__(NiuRunner)
    adapter = MagicMock()
    adapter.list_entities_by_name_regex.return_value = {
        "status": "ok",
        "data": [
            {"entity_name": "2026-08-07会话"},
            {"entity_name": "2026-08-08会话"},
            {"entity_name": "2026-08-09会话"},
        ],
    }

    def fake_has_edge(src, tgt):
        # 8-7→8-9 有边（含 followed_by + related_to）；8-7→8-8、8-8→8-9 无边
        return (src, tgt) == ("2026-08-07会话", "2026-08-09会话")

    def fake_keywords(src, tgt):
        return ["followed_by", "related_to"]

    adapter.has_edge.side_effect = fake_has_edge
    adapter.get_edge_keywords_between.side_effect = fake_keywords
    ingester = MagicMock()
    with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter", return_value=adapter), \
         patch("niu_api.internal.lightrag_adapter.LightRAGIngester", return_value=ingester), \
         patch("datetime.date") as mock_date:
        mock_date.today.return_value = date(2026, 8, 9)
        runner._ensure_session_chain()
    # 语义边保护：不删 8-7→8-9（kws ⊄ {followed_by}）
    adapter.delete_relation.assert_not_called()
    # 补相邻边仍执行：8-7→8-8、8-8→8-9
    assert ingester.inject_custom_kg.call_count == 1
    rels = ingester.inject_custom_kg.call_args.kwargs["relationships"]
    assert [(r["src_id"], r["tgt_id"]) for r in rels] == [
        ("2026-08-07会话", "2026-08-08会话"),
        ("2026-08-08会话", "2026-08-09会话"),
    ]
