"""
Tests for LightRAG retrieval migration.

Validates:
- search_multi_lightrag() groups entities correctly
- _format_lightrag_entities_for_prompt() formats properly
- _build_tool_scores_from_lightrag() assigns rank-based proxy scores
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
                    {"entity_name": "Python", "entity_type": "skill", "description": "Python programming"},
                    {"entity_name": "Rust", "entity_type": "skill", "description": "Rust programming"},
                ],
                "relationships": [],
                "chunks": [],
            }
        }

        with patch.object(adapter, "query_data", return_value=mock_result):
            result = adapter.search_multi_lightrag("python", mode="hybrid", top_k=20)

        assert len(result["skill"]) == 2
        assert len(result["knowledge"]) == 0
        assert len(result["other"]) == 0

    def test_groups_tool_entities(self):
        """Tool entities are grouped into 'knowledge' bucket."""
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

        assert len(result["knowledge"]) == 1
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
                    {"entity_name": "Git", "entity_type": "skill"},
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
        assert len(result["knowledge"]) == 3
        assert len(result["other"]) == 0

    def test_returns_empty_on_none_query_data(self):
        """Returns empty buckets when query_data returns None."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        with patch.object(adapter, "query_data", return_value=None):
            result = adapter.search_multi_lightrag("test")

        assert result == {"skill": [], "knowledge": [], "other": []}

    def test_returns_empty_on_empty_entities(self):
        """Returns empty buckets when no entities found."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_result = {"data": {"entities": [], "relationships": [], "chunks": []}}

        with patch.object(adapter, "query_data", return_value=mock_result):
            result = adapter.search_multi_lightrag("nonexistent")

        assert result == {"skill": [], "knowledge": [], "other": []}


# ============== _format_lightrag_entities_for_prompt tests ==============


class TestFormatLightragEntities:
    """Test NiuRunner._format_lightrag_entities_for_prompt()."""

    @pytest.fixture
    def runner(self):
        """Create a minimal NiuRunner mock for testing."""
        from agent.runner import NiuRunner

        with patch("agent.runner.get_skill_sync"), \
             patch("agent.runner.create_client"), \
             patch("agent.runner.get_system_prompt", return_value=""), \
             patch("agent.runner.get_tools_schema", return_value=[]), \
             patch("agent.runner.NiuHandler"):
            r = NiuRunner.__new__(NiuRunner)
            return r

    def test_formats_skill_entities(self, runner):
        """Skill entities are formatted with file path annotation."""
        entities = [
            {"entity_name": "python", "description": "Python programming"},
        ]
        text, seen = runner._format_lightrag_entities_for_prompt(
            entities, "相关技能", set(),
        )
        assert "python" in text
        assert "Python programming" in text
        assert "~/.niu/skills/python.md" in text

    def test_dedup_against_seen_names(self, runner):
        """Already-seen names are skipped."""
        entities = [
            {"entity_name": "python", "description": "Python"},
        ]
        text, seen = runner._format_lightrag_entities_for_prompt(
            entities, "相关技能", {"python"},
        )
        assert text == ""  # deduped

    def test_filters_mcp_tool_entity_type(self, runner):
        """Entities with entity_type=mcp_tool or tool are filtered out (case-insensitive)."""
        entities = [
            {"entity_name": "file-parser", "entity_type": "mcp_tool", "description": "Parses files"},
            {"entity_name": "browser-automation", "entity_type": "Tool", "description": "Browser tool"},
            {"entity_name": "python", "description": "Python programming"},
        ]
        text, seen = runner._format_lightrag_entities_for_prompt(
            entities, "参考知识", set(),
        )
        assert "file-parser" not in text
        assert "browser-automation" not in text
        assert "python" in text

    def test_filters_blacklisted_entity_name(self, runner):
        """Entities with blacklisted names are filtered out."""
        entities = [
            {"entity_name": "agent_loop.py", "description": "Main loop"},
            {"entity_name": "主Agent", "description": "Main agent"},
            {"entity_name": "python", "description": "Python programming"},
        ]
        text, seen = runner._format_lightrag_entities_for_prompt(
            entities, "参考知识", set(),
        )
        assert "agent_loop.py" not in text
        assert "主Agent" not in text
        assert "python" in text

    def test_empty_entities_returns_empty(self, runner):
        """Empty entity list returns empty string."""
        text, seen = runner._format_lightrag_entities_for_prompt(
            [], "相关技能", set(),
        )
        assert text == ""


# ============== _build_tool_scores_from_lightrag tests ==============



# ============== Injector list_resources / delete_resource migration tests ==============


class TestInjectorListResources:
    """Test list_resources endpoint after migration to LightRAG."""

    @pytest.fixture
    def mock_adapter(self):
        """Create a mocked LightRAGAdapter for testing."""
        with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as cls:
            instance = MagicMock()
            cls.return_value = instance
            yield instance

    @pytest.mark.asyncio
    async def test_list_all_resources(self, mock_adapter):
        """Listing without resource_type queries skill only (disk mode: mcp_tool removed)."""
        from niu_api.injector import list_resources

        mock_adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {"id": "Python", "entity_type": "skill", "description": "Python programming"},
            ],
        }

        result = await list_resources()
        assert len(result.resources) == 1
        assert result.resources[0]["type"] == "skill"

    @pytest.mark.asyncio
    async def test_list_by_type_skill(self, mock_adapter):
        """Filtering by resource_type=skill only queries skill entities."""
        from niu_api.injector import list_resources

        mock_adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {"id": "Git", "entity_type": "skill", "description": "Git version control"},
            ],
        }

        result = await list_resources(resource_type="skill")
        assert len(result.resources) == 1
        assert result.resources[0]["type"] == "skill"
        assert result.resources[0]["name"] == "Git"
        # list_entities should be called with entity_type="skill"
        mock_adapter.list_entities.assert_called_once_with(
            list_type="entities", entity_type="skill", limit=100,
        )

    @pytest.mark.asyncio
    async def test_list_by_type_mcp_tool_returns_empty(self, mock_adapter):
        """resource_type=mcp_tool returns empty in disk mode (no longer mapped to LightRAG)."""
        from niu_api.injector import list_resources

        result = await list_resources(resource_type="mcp_tool")
        assert result.resources == []
        mock_adapter.list_entities.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_unmapped_type_returns_empty(self, mock_adapter):
        """Unmapped category like 'l1' returns empty results."""
        from niu_api.injector import list_resources

        result = await list_resources(resource_type="l1")
        assert result.resources == []
        # list_entities should NOT be called for unmapped types
        mock_adapter.list_entities.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_handles_lightrag_error(self, mock_adapter):
        """Gracefully handles LightRAG errors by returning empty list."""
        from niu_api.injector import list_resources

        mock_adapter.list_entities.side_effect = Exception("LightRAG down")

        result = await list_resources(resource_type="skill")
        assert result.resources == []

    @pytest.mark.asyncio
    async def test_list_handles_status_error(self, mock_adapter):
        """Gracefully handles list_entities returning error status."""
        from niu_api.injector import list_resources

        mock_adapter.list_entities.return_value = {
            "status": "error",
            "message": "LightRAG not available",
        }

        result = await list_resources(resource_type="skill")
        assert result.resources == []

    @pytest.mark.asyncio
    async def test_description_truncated_to_200(self, mock_adapter):
        """Description is truncated to 200 chars."""
        from niu_api.injector import list_resources

        long_desc = "x" * 300
        mock_adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {"id": "LongSkill", "entity_type": "skill", "description": long_desc},
            ],
        }

        result = await list_resources(resource_type="skill")
        assert len(result.resources[0]["description"]) == 200



class TestInjectDynamicResourcesUsesLightRAG:
    """Verify _inject_dynamic_resources uses LightRAG graph retrieval, NOT vector_search."""

    @pytest.fixture
    def runner(self):
        """Create a minimal NiuRunner mock for testing."""
        from agent.runner import NiuRunner

        with patch("agent.runner.get_skill_sync"), \
             patch("agent.runner.create_client"), \
             patch("agent.runner.get_system_prompt", return_value=""), \
             patch("agent.runner.get_tools_schema", return_value=[]), \
             patch("agent.runner.NiuHandler"):
            r = NiuRunner.__new__(NiuRunner)
            return r

    def test_no_vector_search_import_in_runner(self):
        """runner.py must NOT import vector_search or VectorSearchAdapter."""
        import ast
        from pathlib import Path

        runner_path = Path("E:/tools/ai-bot/agent/runner.py")
        source = runner_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "vector_search" not in (node.module or ""), (
                    f"runner.py must not import from vector_search, "
                    f"found: from {node.module}"
                )
                for alias in node.names:
                    assert alias.name != "VectorSearchAdapter", (
                        f"runner.py must not import VectorSearchAdapter, "
                        f"found in: from {node.module}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "vector_search" not in alias.name, (
                        f"runner.py must not import vector_search, "
                        f"found: import {alias.name}"
                    )

    def test_calls_search_multi_lightrag(self, runner):
        """_inject_dynamic_resources must call LightRAGAdapter.search_multi_lightrag."""
        with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_adapter_cls, \
             patch("niu_api.internal.brain_graph.get_brain_graph", side_effect=Exception("no brain")), \
             patch("niu_api.internal.region_injector.BrainContextInjector", side_effect=Exception("no region")):
            mock_adapter = MagicMock()
            mock_adapter.search_multi_lightrag.return_value = {"skill": [], "knowledge": [], "other": []}
            mock_adapter.search_interaction_habits.return_value = []
            mock_adapter_cls.return_value = mock_adapter

            runner._inject_dynamic_resources("test query")

            mock_adapter.search_multi_lightrag.assert_called_once()
            call_kwargs = mock_adapter.search_multi_lightrag.call_args
            assert call_kwargs[0][0] == "test query" or call_kwargs.kwargs.get("query") == "test query"

    def test_calls_search_interaction_habits(self, runner):
        """_inject_dynamic_resources must call LightRAGAdapter.search_interaction_habits."""
        with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_adapter_cls, \
             patch("niu_api.internal.brain_graph.get_brain_graph", side_effect=Exception("no brain")), \
             patch("niu_api.internal.region_injector.BrainContextInjector", side_effect=Exception("no region")):
            mock_adapter = MagicMock()
            mock_adapter.search_multi_lightrag.return_value = {"skill": [], "knowledge": [], "other": []}
            mock_adapter.search_interaction_habits.return_value = []
            mock_adapter_cls.return_value = mock_adapter

            runner._inject_dynamic_resources("test query")

            mock_adapter.search_interaction_habits.assert_called_once()

    def test_brain_region_uses_lightrag_adapter(self, runner):
        """Brain region activation must use LightRAGAdapter (not vector_search)."""
        with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_adapter_cls, \
             patch("niu_api.internal.lightrag_adapter.LightRAGIngester") as mock_ingester_cls, \
             patch("agent.brain_tools.get_activation_mgr") as mock_get_mgr, \
             patch("niu_api.internal.region_manager.RegionManager") as mock_rm_cls, \
             patch("niu_api.internal.region_injector.BrainContextInjector") as mock_injector_cls, \
             patch("niu_api.internal.brain_graph.get_brain_graph", side_effect=Exception("no brain")):
            mock_adapter = MagicMock()
            mock_adapter.search_multi_lightrag.return_value = {"skill": [], "knowledge": [], "other": []}
            mock_adapter.search_interaction_habits.return_value = []
            mock_adapter_cls.return_value = mock_adapter

            mock_ingester = MagicMock()
            mock_ingester_cls.return_value = mock_ingester

            mock_get_mgr.return_value = MagicMock()

            mock_injector = MagicMock()
            mock_injector.inject_brain_context.return_value = "brain context text"
            mock_injector_cls.return_value = mock_injector

            injection, _ = runner._inject_dynamic_resources("test query")

            # LightRAGAdapter must have been instantiated (at least once for main search + brain)
            assert mock_adapter_cls.call_count >= 1
            # BrainContextInjector must have been constructed with LightRAGAdapter
            mock_injector_cls.assert_called_once()
            call_kwargs = mock_injector_cls.call_args.kwargs
            assert "adapter" in call_kwargs
            # RegionManager must have been constructed with LightRAGAdapter (positional args)
            mock_rm_cls.assert_called_once()
            rm_args = mock_rm_cls.call_args.args
            assert len(rm_args) >= 1, "RegionManager must receive at least 1 positional arg (adapter)"
            # Brain context text must appear in injection
            assert "脑区激活上下文" in injection

    def test_does_not_call_vector_search(self, runner):
        """_inject_dynamic_resources must NOT use VectorSearchAdapter or vector_search."""
        with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_adapter_cls, \
             patch("niu_api.internal.brain_graph.get_brain_graph", side_effect=Exception("no brain")):
            mock_adapter = MagicMock()
            mock_adapter.search_multi_lightrag.return_value = {"skill": [], "knowledge": [], "other": []}
            mock_adapter.search_interaction_habits.return_value = []
            mock_adapter_cls.return_value = mock_adapter

            runner._inject_dynamic_resources("test query")

            # The function should complete without error even though vector_search
            # module no longer exists — it must not depend on vector_search at all.

    def test_on_turn_end_uses_lightrag(self, runner):
        """_on_turn_end must call _inject_dynamic_resources (which uses LightRAG)."""
        runner.base_system_prompt = "system prompt"
        runner._memory_dirty = MagicMock()
        runner._memory_dirty.is_set.return_value = False

        with patch.object(runner, "_inject_dynamic_resources", return_value=("injected text", {})) as mock_inject, \
             patch.object(runner, "_extract_context_from_messages", return_value="context"):
            messages = [{"role": "system", "content": "system prompt"}]
            runner._on_turn_end(messages, [], 1)

            mock_inject.assert_called_once_with("context")
            assert "injected text" in messages[0]["content"]


class TestInjectorDeleteResource:
    """Test delete_resource endpoint after migration to LightRAG."""

    @pytest.fixture
    def mock_adapter(self):
        """Create a mocked LightRAGAdapter for testing."""
        with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as cls:
            instance = MagicMock()
            cls.return_value = instance
            yield instance

    @pytest.mark.asyncio
    async def test_delete_success(self, mock_adapter):
        """Successful deletion returns success status."""
        from niu_api.injector import delete_resource

        mock_adapter.delete_entity.return_value = {
            "status": "ok",
            "entity_name": "tool:server/tool1",
            "result": "deleted",
        }

        result = await delete_resource("tool:server/tool1")
        assert result["status"] == "success"
        assert result["resource_id"] == "tool:server/tool1"
        mock_adapter.delete_entity.assert_called_once_with("tool:server/tool1")

    @pytest.mark.asyncio
    async def test_delete_lightrag_error_raises_500(self, mock_adapter):
        """LightRAG deletion failure raises HTTP 500."""
        from niu_api.injector import delete_resource
        from fastapi import HTTPException

        mock_adapter.delete_entity.return_value = {
            "status": "error",
            "message": "Entity not found",
        }

        with pytest.raises(HTTPException) as exc_info:
            await delete_resource("nonexistent")
        assert exc_info.value.status_code == 500
        assert "Entity not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_delete_exception_raises_500(self, mock_adapter):
        """Unexpected exception raises HTTP 500."""
        from niu_api.injector import delete_resource
        from fastapi import HTTPException

        mock_adapter.delete_entity.side_effect = RuntimeError("boom")

        with pytest.raises(HTTPException) as exc_info:
            await delete_resource("something")
        assert exc_info.value.status_code == 500


