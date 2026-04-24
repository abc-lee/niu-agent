"""
Tests for LightRAG retrieval migration.

Validates:
- search_multi_lightrag() groups entities correctly
- _format_lightrag_entities_for_prompt() formats properly
- _build_tool_scores_from_lightrag() assigns rank-based proxy scores
- _apply_query_patterns() delegates to vector_search
- _search_tool_signal_skills_lightrag() searches by tool names
"""

import pytest
from unittest.mock import MagicMock, patch


# ============== search_multi_lightrag tests ==============


class TestSearchMultiLightrag:
    """Test LightRAGAdapter.search_multi_lightrag() grouping logic."""

    def test_groups_skill_entities(self):
        """Skill entities are grouped into 'skill' bucket."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_result = {
            "data": {
                "entities": [
                    {"entity_name": "skill:python", "entity_type": "skill", "description": "Python programming"},
                    {"entity_name": "skill:rust", "entity_type": "skill", "description": "Rust programming"},
                ],
                "relationships": [],
                "chunks": [],
            }
        }

        with patch.object(adapter, "query_data", return_value=mock_result):
            result = adapter.search_multi_lightrag("python", mode="hybrid", top_k=20)

        assert len(result["skill"]) == 2
        assert len(result["mcp_tool"]) == 0
        assert len(result["knowledge"]) == 0

    def test_groups_tool_entities(self):
        """Tool entities are grouped into 'mcp_tool' bucket."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_result = {
            "data": {
                "entities": [
                    {"entity_name": "tool:file-parser/parse", "entity_type": "tool", "description": "Parse files"},
                ],
                "relationships": [],
                "chunks": [],
            }
        }

        with patch.object(adapter, "query_data", return_value=mock_result):
            result = adapter.search_multi_lightrag("parse file", mode="hybrid")

        assert len(result["mcp_tool"]) == 1
        assert len(result["skill"]) == 0

    def test_groups_knowledge_and_concept_entities(self):
        """Knowledge and concept entities both go into 'knowledge' bucket."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_result = {
            "data": {
                "entities": [
                    {"entity_name": "Python", "entity_type": "knowledge", "description": "Python language"},
                    {"entity_name": "OOP", "entity_type": "concept", "description": "Object-oriented programming"},
                ],
                "relationships": [],
                "chunks": [],
            }
        }

        with patch.object(adapter, "query_data", return_value=mock_result):
            result = adapter.search_multi_lightrag("python oop")

        assert len(result["knowledge"]) == 2

    def test_mixed_entity_types(self):
        """Mixed entity types are correctly grouped."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_result = {
            "data": {
                "entities": [
                    {"entity_name": "skill:git", "entity_type": "skill"},
                    {"entity_name": "tool:git/commit", "entity_type": "tool"},
                    {"entity_name": "Git", "entity_type": "knowledge"},
                    {"entity_name": "Alice", "entity_type": "person"},  # Not mapped
                ],
                "relationships": [],
                "chunks": [],
            }
        }

        with patch.object(adapter, "query_data", return_value=mock_result):
            result = adapter.search_multi_lightrag("git")

        assert len(result["skill"]) == 1
        assert len(result["mcp_tool"]) == 1
        assert len(result["knowledge"]) == 1

    def test_returns_empty_on_none_query_data(self):
        """Returns empty buckets when query_data returns None."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        with patch.object(adapter, "query_data", return_value=None):
            result = adapter.search_multi_lightrag("test")

        assert result == {"skill": [], "mcp_tool": [], "knowledge": []}

    def test_returns_empty_on_empty_entities(self):
        """Returns empty buckets when no entities found."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_result = {"data": {"entities": [], "relationships": [], "chunks": []}}

        with patch.object(adapter, "query_data", return_value=mock_result):
            result = adapter.search_multi_lightrag("nonexistent")

        assert result == {"skill": [], "mcp_tool": [], "knowledge": []}


# ============== _format_lightrag_entities_for_prompt tests ==============


class TestFormatLightragEntities:
    """Test NiuRunner._format_lightrag_entities_for_prompt()."""

    @pytest.fixture
    def runner(self):
        """Create a minimal NiuRunner mock for testing."""
        from agent.runner import NiuRunner

        with patch("agent.runner.get_vector_search"), \
             patch("agent.runner.get_skill_sync"), \
             patch("agent.runner.create_client"), \
             patch("agent.runner.get_system_prompt", return_value=""), \
             patch("agent.runner.get_tools_schema", return_value=[]), \
             patch("agent.runner.NiuHandler"):
            r = NiuRunner.__new__(NiuRunner)
            return r

    def test_formats_skill_entities(self, runner):
        """Skill entities are formatted with (来源: 知识图谱) marker."""
        entities = [
            {"entity_name": "skill:python", "description": "Python programming"},
        ]
        text, seen = runner._format_lightrag_entities_for_prompt(
            entities, "相关技能", set(),
        )
        assert "python" in text
        assert "来源: 知识图谱" in text
        assert "Python programming" in text

    def test_strips_type_prefix(self, runner):
        """Type prefixes (skill:, tool:, knowledge:) are stripped for display."""
        entities = [
            {"entity_name": "skill:git", "description": "Git version control"},
        ]
        text, seen = runner._format_lightrag_entities_for_prompt(
            entities, "相关技能", set(),
        )
        assert "skill:git" not in text  # prefix stripped
        assert "git" in text

    def test_dedup_against_seen_names(self, runner):
        """Already-seen names are skipped."""
        entities = [
            {"entity_name": "skill:python", "description": "Python"},
        ]
        text, seen = runner._format_lightrag_entities_for_prompt(
            entities, "相关技能", {"python"},
        )
        assert text == ""  # deduped

    def test_empty_entities_returns_empty(self, runner):
        """Empty entity list returns empty string."""
        text, seen = runner._format_lightrag_entities_for_prompt(
            [], "相关技能", set(),
        )
        assert text == ""


# ============== _build_tool_scores_from_lightrag tests ==============


class TestBuildToolScoresFromLightrag:
    """Test NiuRunner._build_tool_scores_from_lightrag() rank-based scoring."""

    @pytest.fixture
    def runner(self):
        from agent.runner import NiuRunner

        with patch("agent.runner.get_vector_search"), \
             patch("agent.runner.get_skill_sync"), \
             patch("agent.runner.create_client"), \
             patch("agent.runner.get_system_prompt", return_value=""), \
             patch("agent.runner.get_tools_schema", return_value=[]), \
             patch("agent.runner.NiuHandler"), \
             patch("agent.runner.get_registry"):
            r = NiuRunner.__new__(NiuRunner)
            return r

    def test_top5_gets_score_70(self, runner):
        """Top-5 tool entities get proxy score 70."""
        lightrag_results = {
            "mcp_tool": [
                {"entity_name": "tool:server/tool1", "entity_type": "tool"},
            ],
        }
        mock_registry = MagicMock()
        mock_registry.get_visibility.return_value = "dynamic"
        with patch("agent.runner.get_registry", return_value=mock_registry):
            scores = runner._build_tool_scores_from_lightrag(lightrag_results)
        assert scores.get("server/tool1") == 70

    def test_top6_10_gets_score_55(self, runner):
        """Entities at rank 6-10 get proxy score 55."""
        tools = [
            {"entity_name": f"tool:server/tool{i}", "entity_type": "tool"}
            for i in range(8)
        ]
        lightrag_results = {"mcp_tool": tools}
        mock_registry = MagicMock()
        mock_registry.get_visibility.return_value = "dynamic"
        with patch("agent.runner.get_registry", return_value=mock_registry):
            scores = runner._build_tool_scores_from_lightrag(lightrag_results)
        assert scores.get("server/tool0") == 70  # rank 0
        assert scores.get("server/tool5") == 55  # rank 5

    def test_hidden_tools_excluded(self, runner):
        """Tools with visibility='hidden' are excluded from scores."""
        lightrag_results = {
            "mcp_tool": [
                {"entity_name": "tool:server/hidden_tool", "entity_type": "tool"},
            ],
        }
        mock_registry = MagicMock()
        mock_registry.get_visibility.return_value = "hidden"
        with patch("agent.runner.get_registry", return_value=mock_registry):
            scores = runner._build_tool_scores_from_lightrag(lightrag_results)
        assert "server/hidden_tool" not in scores

    def test_non_tool_prefix_ignored(self, runner):
        """Entities without 'tool:' prefix are ignored."""
        lightrag_results = {
            "mcp_tool": [
                {"entity_name": "some_random_entity", "entity_type": "tool"},
            ],
        }
        mock_registry = MagicMock()
        with patch("agent.runner.get_registry", return_value=mock_registry):
            scores = runner._build_tool_scores_from_lightrag(lightrag_results)
        assert len(scores) == 0


# ============== _apply_query_patterns tests ==============


class TestApplyQueryPatterns:
    """Test NiuRunner._apply_query_patterns()."""

    @pytest.fixture
    def runner(self):
        from agent.runner import NiuRunner

        with patch("agent.runner.get_vector_search") as mock_vs, \
             patch("agent.runner.get_skill_sync"), \
             patch("agent.runner.create_client"), \
             patch("agent.runner.get_system_prompt", return_value=""), \
             patch("agent.runner.get_tools_schema", return_value=[]), \
             patch("agent.runner.NiuHandler"):
            r = NiuRunner.__new__(NiuRunner)
            r.vector_search = MagicMock()
            return r

    def test_returns_original_when_no_pattern(self, runner):
        """Returns original context when no query_pattern matches."""
        runner.vector_search.search.return_value = []
        result = runner._apply_query_patterns("hello world")
        assert result == "hello world"

    def test_returns_expanded_when_pattern_matches(self, runner):
        """Returns expanded_query when a pattern matches."""
        mock_result = MagicMock()
        mock_result.metadata = {"expanded_query": "photo, image, picture"}
        runner.vector_search.search.return_value = [mock_result]
        result = runner._apply_query_patterns("photo")
        assert result == "photo, image, picture"

    def test_returns_original_on_exception(self, runner):
        """Returns original context when vector_search raises."""
        runner.vector_search.search.side_effect = Exception("db error")
        result = runner._apply_query_patterns("test")
        assert result == "test"
