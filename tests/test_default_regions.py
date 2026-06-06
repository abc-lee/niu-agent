"""Tests for default brain region creation."""
from unittest.mock import MagicMock, patch
import pytest


class TestDefaultRegions:
    def test_default_regions_config(self):
        """get_default_regions_config() 应返回6个缺省脑区"""
        from niu_api.internal.region_manager import get_default_regions_config
        # Mock preferences.json not having brain_regions section — fallback to hardcoded
        with patch("builtins.open", side_effect=FileNotFoundError):
            defaults = get_default_regions_config()
        labels = [d["label"] for d in defaults]
        assert "聊天历史" in labels
        assert "文档库" in labels
        assert "知识体系" in labels
        assert len(defaults) == 6

    def test_create_default_regions_creates_new(self):
        """脑区不存在时应创建6个"""
        from niu_api.internal.region_manager import create_default_regions

        mock_adapter = MagicMock()
        mock_ingester = MagicMock()

        with patch("niu_api.internal.region_manager.get_brain_regions", return_value=[]):
            result = create_default_regions(adapter=mock_adapter, ingester=mock_ingester)
        assert result["created"] == 6
        assert result["existing"] == 0
        assert mock_ingester.inject_custom_kg.call_count == 1

    def test_create_default_regions_skips_existing(self):
        """脑区已存在时应全部跳过"""
        from niu_api.internal.region_manager import create_default_regions

        mock_adapter = MagicMock()
        mock_ingester = MagicMock()

        all_regions = ["聊天历史脑区", "文档库脑区", "知识体系脑区",
                       "人际关系脑区", "工作事务脑区", "生活事务脑区"]
        with patch("niu_api.internal.region_manager.get_brain_regions",
                    return_value=all_regions):
            result = create_default_regions(adapter=mock_adapter, ingester=mock_ingester)
        assert result["created"] == 0
        assert result["existing"] == 6
        assert mock_ingester.inject_custom_kg.call_count == 0

    def test_create_default_regions_partial_existing(self):
        """部分脑区已存在时只创建缺失的"""
        from niu_api.internal.region_manager import create_default_regions

        mock_adapter = MagicMock()
        mock_ingester = MagicMock()

        with patch("niu_api.internal.region_manager.get_brain_regions",
                    return_value=["聊天历史脑区", "文档库脑区"]):
            result = create_default_regions(adapter=mock_adapter, ingester=mock_ingester)
        assert result["created"] == 4
        assert result["existing"] == 2
        assert mock_ingester.inject_custom_kg.call_count == 1
