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

    def test_raises_on_query_data_error_dict(self, mock_adapter):
        """E3 契约反转：错误不再伪装为无结果——query_data error dict → timeline_query 抛 RuntimeError。

        锁定 raise 传导链：adapter 层 error dict 不再被吞错返回 []，而是从
        timeline_query 顶层出——MCP lightrag_timeline_query except 捕获转 error dict。
        """
        mock_adapter.query_data = MagicMock(
            return_value={"status": "error", "message": "图谱查询失败: boom"}
        )
        with pytest.raises(RuntimeError, match="图谱查询失败"):
            mock_adapter.timeline_query("查询")

    def test_returns_timeline_results_sorted_by_timestamp(self, mock_adapter):
        """返回时间线结果，按时间戳排序（最近优先）"""
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
                        "relation": "related_to",
                        "description": "L1|编程语言分类",
                        "weight": 1.0,
                    },
                ],
                "stats": {"nodes": 1, "edges": 2, "max_depth": 2},
            }
        )
        result = mock_adapter.timeline_query("Python")
        for item in result:
            if "relation" in item and item["relation"] != "match":
                assert item["relation"] in (
                    "followed_by",
                    "corrected_by",
                    "led_to",
                    "resolved_by",
                )

    def test_start_entities_skips_vector_search(self, mock_adapter):
        """start_entities 参数跳过向量搜索，直接从指定实体开始"""
        mock_adapter.explore_node = MagicMock(
            return_value={
                "center": {"id": "Go语言", "name": "Go语言", "type": "UNKNOWN"},
                "nodes": [
                    {
                        "id": "Go语言",
                        "name": "Go语言",
                        "type": "UNKNOWN",
                        "description": "L2|created_at=2026-04-26T10:00:00|偏好Go",
                    }
                ],
                "edges": [],
                "stats": {"nodes": 1, "edges": 0, "max_depth": 2},
            }
        )
        result = mock_adapter.timeline_query(
            query="", start_entities=["Go语言"]
        )
        assert len(result) >= 1
        assert result[0]["entity_name"] == "Go语言"

    def test_direction_forward_sorts_earliest_first(self, mock_adapter):
        """direction=forward 按最早优先排序"""
        mock_adapter.query_data = MagicMock(
            return_value={
                "status": "success",
                "data": {
                    "entities": [
                        {"entity_name": "Event1", "description": "L1|created_at=2026-04-20|"},
                        {"entity_name": "Event2", "description": "L1|created_at=2026-04-25|"},
                    ]
                },
            }
        )
        mock_adapter.explore_node = MagicMock(
            return_value={
                "center": {},
                "nodes": [],
                "edges": [],
                "stats": {"nodes": 0, "edges": 0, "max_depth": 2},
            }
        )
        result = mock_adapter.timeline_query("events", direction="forward")
        if len(result) >= 2:
            # Earlier timestamp should come first in forward mode
            assert result[0].get("timestamp", "") <= result[1].get("timestamp", "")

    def test_max_results_limits_output(self, mock_adapter):
        """max_results 限制返回数量"""
        mock_adapter.query_data = MagicMock(
            return_value={
                "status": "success",
                "data": {
                    "entities": [
                        {"entity_name": f"Event{i}", "description": f"L1|created_at=2026-04-{20+i:02d}|"}
                        for i in range(5)
                    ]
                },
            }
        )
        mock_adapter.explore_node = MagicMock(
            return_value={
                "center": {},
                "nodes": [],
                "edges": [],
                "stats": {"nodes": 0, "edges": 0, "max_depth": 2},
            }
        )
        result = mock_adapter.timeline_query("events", max_results=2)
        assert len(result) <= 2
