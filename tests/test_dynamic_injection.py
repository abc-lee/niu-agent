"""Tests for dynamic injection — per-type retrieval with filter_lambda."""

from unittest.mock import MagicMock, patch
from niu_api.internal.lightrag_adapter import LightRAGAdapter


class TestSearchByFilePath:
    """Test search_by_file_path — pre-filter by file_path then top-k."""

    @patch.object(LightRAGAdapter, 'query_data')
    def test_filter_lambda_passed_to_query_data(self, mock_query):
        """filter_lambda must be passed to query_data for pre-filtering."""
        adapter = LightRAGAdapter.__new__(LightRAGAdapter)
        mock_query.return_value = {"data": {"entities": [], "relationships": [], "chunks": []}}

        adapter.search_by_file_path("test query", file_path_contains="skill_sync", top_k=10)

        call_kwargs = mock_query.call_args.kwargs
        assert "filter_lambda" in call_kwargs
        filter_fn = call_kwargs["filter_lambda"]
        assert callable(filter_fn)
        # Verify the filter function checks file_path
        assert filter_fn({"file_path": "skill_sync"}) is True
        assert filter_fn({"file_path": "some_doc.md"}) is False
        assert filter_fn({"file_path": None}) is False

    @patch.object(LightRAGAdapter, 'query_data')
    def test_returns_list_of_entities(self, mock_query):
        """Should return list of entity dicts, not categorized dict."""
        adapter = LightRAGAdapter.__new__(LightRAGAdapter)
        mock_query.return_value = {
            "data": {
                "entities": [
                    {"entity_name": "report-skill", "entity_type": "Skill", "file_path": "skill_sync"},
                    {"entity_name": "note-management", "entity_type": "Skill", "file_path": "skill_sync"},
                ],
                "relationships": [],
                "chunks": [],
            }
        }

        result = adapter.search_by_file_path("日志", file_path_contains="skill_sync", top_k=10)

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["entity_name"] == "report-skill"
