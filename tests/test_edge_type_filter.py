"""Tests for explore_node edge_types filtering."""
from unittest.mock import MagicMock, patch
import pytest


class TestEdgeTypeFilter:
    def test_filter_by_edge_types(self):
        """edge_types 参数应过滤返回的边"""
        with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter.__init__", lambda self: None):
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            adapter = LightRAGAdapter()

        # Mock _get_rag to return a rag instance
        mock_rag = MagicMock()
        adapter._get_rag = MagicMock(return_value=mock_rag)

        with patch("niu_api.internal.lightrag_adapter.call_async") as mock_call:
            mock_node = MagicMock()
            mock_node.id = "TestEntity"
            mock_node.properties = {"entity_type": "Event", "description": ""}

            mock_edge_semantic = MagicMock()
            mock_edge_semantic.source = "A"
            mock_edge_semantic.target = "B"
            mock_edge_semantic.properties = {"keywords": "USED_FOR", "description": "", "weight": 1.0}

            mock_edge_timeline = MagicMock()
            mock_edge_timeline.source = "B"
            mock_edge_timeline.target = "C"
            mock_edge_timeline.properties = {"keywords": "followed_by", "description": "", "weight": 1.0}

            mock_kg = MagicMock()
            mock_kg.nodes = [mock_node]
            mock_kg.edges = [mock_edge_semantic, mock_edge_timeline]
            mock_call.return_value = mock_kg

            result = adapter.explore_node(
                entity_name="TestEntity", depth=1,
                edge_types=["USED_FOR"],
            )
            assert len(result["edges"]) == 1
            assert result["edges"][0]["relation"] == "USED_FOR"

    def test_no_filter_returns_all_edges(self):
        """不指定 edge_types 时返回所有边"""
        with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter.__init__", lambda self: None):
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            adapter = LightRAGAdapter()

        mock_rag = MagicMock()
        adapter._get_rag = MagicMock(return_value=mock_rag)

        with patch("niu_api.internal.lightrag_adapter.call_async") as mock_call:
            mock_node = MagicMock()
            mock_node.id = "TestEntity"
            mock_node.properties = {"entity_type": "Event", "description": ""}

            mock_edge1 = MagicMock()
            mock_edge1.source = "A"
            mock_edge1.target = "B"
            mock_edge1.properties = {"keywords": "USED_FOR", "description": "", "weight": 1.0}

            mock_edge2 = MagicMock()
            mock_edge2.source = "B"
            mock_edge2.target = "C"
            mock_edge2.properties = {"keywords": "followed_by", "description": "", "weight": 1.0}

            mock_kg = MagicMock()
            mock_kg.nodes = [mock_node]
            mock_kg.edges = [mock_edge1, mock_edge2]
            mock_call.return_value = mock_kg

            result = adapter.explore_node(entity_name="TestEntity", depth=1)
            assert len(result["edges"]) == 2

    def test_filter_with_empty_list_returns_no_edges(self):
        """edge_types=[] 时返回无边"""
        with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter.__init__", lambda self: None):
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            adapter = LightRAGAdapter()

        mock_rag = MagicMock()
        adapter._get_rag = MagicMock(return_value=mock_rag)

        with patch("niu_api.internal.lightrag_adapter.call_async") as mock_call:
            mock_node = MagicMock()
            mock_node.id = "TestEntity"
            mock_node.properties = {"entity_type": "Event", "description": ""}

            mock_edge = MagicMock()
            mock_edge.source = "A"
            mock_edge.target = "B"
            mock_edge.properties = {"keywords": "USED_FOR", "description": "", "weight": 1.0}

            mock_kg = MagicMock()
            mock_kg.nodes = [mock_node]
            mock_kg.edges = [mock_edge]
            mock_call.return_value = mock_kg

            result = adapter.explore_node(
                entity_name="TestEntity", depth=1,
                edge_types=[],
            )
            assert len(result["edges"]) == 0
