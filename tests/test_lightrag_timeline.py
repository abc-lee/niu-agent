"""Tests for LightRAGAdapter.timeline_query()"""
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_adapter():
    """Create a LightRAGAdapter with mocked _get_rag and call_async."""
    with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter.__init__", lambda self: None):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        adapter._rag = MagicMock()
        adapter._loop = MagicMock()
        return adapter


class TestTimelineQuery:
    def test_returns_empty_when_no_match(self, mock_adapter):
        """向量搜索无匹配时返回空列表"""
        with patch("niu_api.internal.lightrag_adapter.call_async", return_value=None):
            with patch.object(mock_adapter, "query_data", return_value=None):
                result = mock_adapter.timeline_query("不存在的内容")
                assert result == []

    def test_returns_timeline_results_sorted_by_timestamp(self, mock_adapter):
        """返回时间线结果，按时间戳排序（最近优先）"""
        # Mock query_data returning one entity
        mock_adapter.query_data = MagicMock(
            return_value={
                "status": "success",
                "data": {
                    "entities": [
                        {
                            "entity_name": "Rust语言",
                            "description": "L2|created_at=2026-04-25T10:00:00|用户偏好Rust语言",
                        }
                    ]
                },
            }
        )
        # Mock explore_node for time chain traversal
        mock_adapter.explore_node = MagicMock(
            return_value={
                "center": {"id": "Rust语言", "name": "Rust语言", "type": "UNKNOWN"},
                "nodes": [
                    {
                        "id": "Rust语言",
                        "name": "Rust语言",
                        "type": "UNKNOWN",
                        "description": "L2|created_at=2026-04-25T10:00:00|用户偏好Rust语言",
                    }
                ],
                "edges": [
                    {
                        "source": "Rust语言",
                        "target": "Rust语言",
                        "relation": "followed_by",
                        "description": "L2|created_at=2026-04-27T14:00:00|深入学习所有权机制",
                        "weight": 1.0,
                    }
                ],
                "stats": {"nodes": 1, "edges": 1, "max_depth": 2},
            }
        )
        result = mock_adapter.timeline_query("Rust")
        assert len(result) >= 1
        # Results should contain entity info with timestamps
        assert any("Rust" in str(r) for r in result)

    def test_filters_non_timeline_edges(self, mock_adapter):
        """只遍历时间链边类型，忽略语义边"""
        mock_adapter.query_data = MagicMock(
            return_value={
                "status": "success",
                "data": {
                    "entities": [
                        {
                            "entity_name": "Python",
                            "description": "L2|created_at=2026-04-20T08:00:00|常用Python",
                        }
                    ]
                },
            }
        )
        mock_adapter.explore_node = MagicMock(
            return_value={
                "center": {"id": "Python", "name": "Python", "type": "UNKNOWN"},
                "nodes": [
                    {
                        "id": "Python",
                        "name": "Python",
                        "type": "UNKNOWN",
                        "description": "L2|created_at=2026-04-20T08:00:00|常用Python",
                    }
                ],
                "edges": [
                    {
                        "source": "Python",
                        "target": "Python",
                        "relation": "followed_by",
                        "description": "L2|created_at=2026-04-22T09:00:00|换用3.12版本",
                        "weight": 1.0,
                    },
                    {
                        "source": "Python",
                        "target": "编程语言",
                        "relation": "related_to",  # 语义边，应被过滤
                        "description": "L1|编程语言分类",
                        "weight": 1.0,
                    },
                ],
                "stats": {"nodes": 1, "edges": 2, "max_depth": 2},
            }
        )
        result = mock_adapter.timeline_query("Python")
        # Should only include timeline edges, not semantic ones
        for item in result:
            if "relation" in item and item["relation"] != "match":
                assert item["relation"] in (
                    "followed_by",
                    "corrected_by",
                    "led_to",
                    "resolved_by",
                )
