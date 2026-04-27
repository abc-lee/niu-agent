"""Tests for default brain region creation."""
from unittest.mock import MagicMock
import pytest


class TestDefaultRegions:
    def test_default_regions_constant(self):
        """DEFAULT_REGIONS 应包含3个缺省脑区"""
        from niu_api.internal.region_manager import DEFAULT_REGIONS
        assert "聊天历史" in DEFAULT_REGIONS
        assert "文档库" in DEFAULT_REGIONS
        assert "知识体系" in DEFAULT_REGIONS

    def test_create_default_regions_creates_new(self):
        """脑区不存在时应创建"""
        from niu_api.internal.region_manager import create_default_regions

        mock_adapter = MagicMock()
        mock_ingester = MagicMock()

        # query_data returns no existing regions
        mock_adapter.query_data.return_value = {"data": {"entities": []}}

        result = create_default_regions(adapter=mock_adapter, ingester=mock_ingester)
        assert result["created"] == 3
        assert result["existing"] == 0
        assert mock_ingester.inject_entity.call_count == 3
        assert mock_ingester.inject_relation.call_count == 3

    def test_create_default_regions_skips_existing(self):
        """脑区已存在时应跳过"""
        from niu_api.internal.region_manager import create_default_regions

        mock_adapter = MagicMock()
        mock_ingester = MagicMock()

        # query_data returns existing regions
        mock_adapter.query_data.return_value = {
            "data": {
                "entities": [
                    {"entity_name": "brain:region:聊天历史"},
                    {"entity_name": "brain:region:文档库"},
                    {"entity_name": "brain:region:知识体系"},
                ]
            }
        }

        result = create_default_regions(adapter=mock_adapter, ingester=mock_ingester)
        assert result["created"] == 0
        assert result["existing"] == 3
        assert mock_ingester.inject_entity.call_count == 0

    def test_create_default_regions_partial_existing(self):
        """部分脑区已存在时只创建缺失的"""
        from niu_api.internal.region_manager import create_default_regions

        mock_adapter = MagicMock()
        mock_ingester = MagicMock()

        # Only 聊天历史 exists
        call_count = [0]

        def mock_query(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"data": {"entities": [{"entity_name": "brain:region:聊天历史"}]}}
            return {"data": {"entities": []}}

        mock_adapter.query_data.side_effect = mock_query

        result = create_default_regions(adapter=mock_adapter, ingester=mock_ingester)
        assert result["created"] == 2
        assert result["existing"] == 1
