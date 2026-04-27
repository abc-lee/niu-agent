"""Tests for brain graph forgetting curve operations."""
from unittest.mock import MagicMock, patch
import pytest


class TestBrainDecay:
    @patch("niu_api.internal.brain_graph.LightRAGAdapter")
    def test_decay_edges_returns_counts(self, mock_adapter_cls):
        """decay_edges 应返回衰减和清理候选计数"""
        from niu_api.internal.brain_graph import BrainGraph
        brain = BrainGraph()
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter

        result = brain.decay_edges()
        assert "decayed" in result
        assert "cleanup_candidates" in result

    @patch("niu_api.internal.brain_graph.LightRAGAdapter")
    def test_consolidate_l0_to_l1_returns_count(self, mock_adapter_cls):
        """consolidate_l0_to_l1 应返回升级计数"""
        from niu_api.internal.brain_graph import BrainGraph
        brain = BrainGraph()
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter

        result = brain.consolidate_l0_to_l1()
        assert "promoted" in result

    @patch("niu_api.internal.brain_graph.LightRAGAdapter")
    def test_cleanup_low_weight_returns_counts(self, mock_adapter_cls):
        """cleanup_low_weight 应返回移除计数"""
        from niu_api.internal.brain_graph import BrainGraph
        brain = BrainGraph()
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter

        result = brain.cleanup_low_weight()
        assert "removed_entities" in result
        assert "removed_edges" in result

    def test_extract_level(self):
        """_extract_level 应从描述前缀提取级别"""
        from niu_api.internal.brain_graph import BrainGraph
        assert BrainGraph._extract_level("L0|created_at=2026-04-27|access_count=1") == "L0"
        assert BrainGraph._extract_level("L1|created_at=2026-04-27|access_count=5") == "L1"
        assert BrainGraph._extract_level("L2|created_at=2026-04-27|") == "L2"
        assert BrainGraph._extract_level("no level info") == ""
        assert BrainGraph._extract_level("") == ""

    def test_extract_access_count(self):
        """_extract_access_count 应从描述前缀提取访问次数"""
        from niu_api.internal.brain_graph import BrainGraph
        assert BrainGraph._extract_access_count("L0|created_at=2026-04-27|access_count=3") == 3
        assert BrainGraph._extract_access_count("L0|created_at=2026-04-27|access_count=0") == 0
        assert BrainGraph._extract_access_count("L0|no_access_count") == 0
        assert BrainGraph._extract_access_count("") == 0
