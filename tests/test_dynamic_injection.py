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


class TestDecayPoolCategoryCorrection:
    """Test that inject() updates category when entity is re-injected with correct category."""

    def test_category_updated_when_lower_score(self):
        """Even with lower score, category must be updated if different."""
        from agent.decay_pool import DecayPool

        pool = DecayPool()
        # First inject as "knowledge" (wrong category)
        pool.inject(
            entity_name="report-skill",
            entity_dict={"entity_name": "report-skill", "entity_type": "Skill", "description": "test"},
            category="knowledge",
            source="vector",
            vector_score=0.8,
        )
        # Re-inject with correct category "skill" but lower score
        pool.inject(
            entity_name="report-skill",
            entity_dict={"entity_name": "report-skill", "entity_type": "Skill", "description": "test"},
            category="skill",
            source="vector",
            vector_score=0.5,
        )
        # Category should be "skill" now
        entry = pool._entries["report-skill"]
        assert entry.category == "skill"


class TestInjectDynamicResourcesSkillRetrieval:
    """Test that _inject_dynamic_resources retrieves skills independently."""

    def test_skill_retrieval_uses_search_by_file_path(self):
        """Skill retrieval must use search_by_file_path, not search_multi_lightrag."""
        from agent.runner import NiuRunner

        runner = NiuRunner.__new__(NiuRunner)
        runner._decay_pool = MagicMock()
        runner._decay_pool.decay = MagicMock()
        runner._decay_pool.inject = MagicMock()
        runner._decay_pool.get_top_by_category = MagicMock(return_value=[])
        runner._decay_pool.get_top_by_source = MagicMock(return_value=[])
        runner._brain_adapter = MagicMock()
        runner._brain_adapter.activate_for_query = MagicMock()
        runner._brain_adapter.format_region_map_only = MagicMock(return_value="")
        runner._format_running_subagents_section = MagicMock(return_value="")
        runner._get_brain_injector = MagicMock(return_value=None)
        runner._format_lightrag_entities_for_prompt = MagicMock(return_value=("", set()))
        runner._INJECT_ENTITY_TYPE_BLACKLIST = set()
        runner._INJECT_ENTITY_NAME_BLACKLIST = set()

        call_log = []

        def mock_search_multi(query, mode="local", top_k=20, keywords=None):
            call_log.append(("search_multi_lightrag", query, top_k))
            return {"skill": [], "knowledge": [], "other": []}

        def mock_search_by_fp(query, file_path_contains, top_k=10, keywords=None):
            call_log.append(("search_by_file_path", query, file_path_contains, top_k))
            return [{"entity_name": "report-skill", "entity_type": "Skill", "file_path": "skill_sync", "description": "test", "distance": 0.55}]

        runner._brain_adapter.search_multi_lightrag = mock_search_multi
        runner._brain_adapter.search_by_file_path = mock_search_by_fp

        runner._inject_dynamic_resources("test context")

        # Verify search_by_file_path was called for skills
        skill_calls = [c for c in call_log if c[0] == "search_by_file_path"]
        assert len(skill_calls) == 1
        assert "skill_sync" in skill_calls[0][2]

        # Verify search_multi_lightrag was NOT called with the old all-in-one approach
        # (it should only be used for knowledge, not skills)
        multi_calls = [c for c in call_log if c[0] == "search_multi_lightrag"]
        # knowledge still uses search_multi_lightrag
        assert len(multi_calls) >= 1
