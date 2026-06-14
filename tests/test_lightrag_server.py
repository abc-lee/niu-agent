"""
Tests for lightrag-server MCP module.

TDD RED phase: Tests define the lightrag-server API contract.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ============== Module Import Helper ==============

# Add the lightrag-server src directory to sys.path
_LIGHTRAG_SERVER_SRC = str(
    Path(__file__).parent.parent / "mcp-servers" / "lightrag-server" / "src"
)


def _import_module():
    """Import the lightrag-server module with mocked dependencies."""
    # Mock heavy dependencies before import
    mock_adapter = MagicMock()
    mock_manager = MagicMock()

    with patch.dict("sys.modules", {
        "niu_api": MagicMock(),
        "niu_api.internal": MagicMock(),
        "niu_api.internal.lightrag_adapter": mock_adapter,
        "niu_api.internal.lightrag_manager": mock_manager,
    }):
        if _LIGHTRAG_SERVER_SRC not in sys.path:
            sys.path.insert(0, _LIGHTRAG_SERVER_SRC)

        import importlib
        import niu_lightrag_server
        importlib.reload(niu_lightrag_server)
        return niu_lightrag_server


# ============== TOOL_SCHEMAS ==============


class TestToolSchemas:
    """Test TOOL_SCHEMAS structure and completeness."""

    def test_schemas_count(self):
        """Should have exactly 16 tool schemas."""
        mod = _import_module()
        assert len(mod.TOOL_SCHEMAS) == 16

    def test_all_tools_have_required_fields(self):
        """Each schema must have name, description, input_schema."""
        mod = _import_module()
        for name, schema in mod.TOOL_SCHEMAS.items():
            assert "name" in schema, f"{name} missing 'name'"
            assert "description" in schema, f"{name} missing 'description'"
            assert "input_schema" in schema, f"{name} missing 'input_schema'"
            assert schema["name"] == name

    def test_query_schema_has_mode(self):
        """lightrag_query schema must have mode parameter with bypass support."""
        mod = _import_module()
        schema = mod.TOOL_SCHEMAS["lightrag_query"]
        props = schema["input_schema"]["properties"]
        assert "query" in props
        assert "mode" in props
        assert props["mode"]["default"] == "mix"
        assert "bypass" in props["mode"]["enum"]

    def test_insert_schema_has_content(self):
        """lightrag_insert schema must have content parameter."""
        mod = _import_module()
        schema = mod.TOOL_SCHEMAS["lightrag_insert"]
        props = schema["input_schema"]["properties"]
        assert "content" in props
        assert "content" in schema["input_schema"].get("required", [])

    def test_insert_custom_kg_schema(self):
        """lightrag_insert_custom_kg must have entities/relationships/chunks."""
        mod = _import_module()
        schema = mod.TOOL_SCHEMAS["lightrag_insert_custom_kg"]
        props = schema["input_schema"]["properties"]
        assert "entities" in props
        assert "relationships" in props
        assert "chunks" in props

    def test_get_tool_schemas_returns_list(self):
        """get_tool_schemas() must return a list of 16 schemas."""
        mod = _import_module()
        schemas = mod.get_tool_schemas()
        assert isinstance(schemas, list)
        assert len(schemas) == 16


# ============== Tool Functions ==============


class TestLightragQuery:
    """Test lightrag_query tool function."""

    def test_delegates_to_adapter(self):
        """lightrag_query should delegate to LightRAGAdapter.query()."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.query.return_value = "test result"
        mod._adapter = mock_adapter

        result = mod.lightrag_query(query="test query", mode="mix")

        mock_adapter.query.assert_called_once_with(
            query="test query", mode="mix",
            only_need_context=True, top_k=5,
            response_type="%s" % "Multiple Paragraphs",
        )
        assert result == "test result"

    def test_custom_params(self):
        """lightrag_query should pass custom parameters."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.query.return_value = "result"
        mod._adapter = mock_adapter

        mod.lightrag_query(
            query="test", mode="local",
            only_need_context=False, top_k=10,
            response_type="Bullet Points",
        )

        mock_adapter.query.assert_called_once_with(
            query="test", mode="local",
            only_need_context=False, top_k=10,
            response_type="Bullet Points",
        )


class TestLightragQueryData:
    """Test lightrag_query_data tool function."""

    def test_delegates_to_adapter(self):
        """lightrag_query_data should delegate to LightRAGAdapter.query_data()."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.query_data.return_value = {"data": {"entities": []}}
        mod._adapter = mock_adapter

        result = mod.lightrag_query_data(query="test", mode="local", top_k=10)

        mock_adapter.query_data.assert_called_once_with(
            query="test", mode="local", top_k=10, keywords=None,
        )


class TestLightragSearchEntities:
    """Test lightrag_search_entities tool function."""

    def test_search_with_fields(self):
        """Should pass fields parameter to adapter."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.query_data.return_value = {
            "data": {"entities": [{"entity_name": "Python", "entity_type": "skill"}]}
        }
        mod._adapter = mock_adapter
        mod.LightRAGAdapter._is_no_result = MagicMock(return_value=False)

        result = mod.lightrag_search_entities(query="python", fields=["entity_name", "entity_type"])

        mock_adapter.query_data.assert_called_once_with(
            query="python", mode="local", top_k=10, keywords=None, fields=["entity_name", "entity_type"]
        )
        assert result["status"] == "ok"

    def test_search_without_type_filter(self):
        """Should return all entities when no filter."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.query_data.return_value = {
            "data": {"entities": [{"name": "Python"}]}
        }
        mod._adapter = mock_adapter
        mod.LightRAGAdapter._is_no_result = MagicMock(return_value=False)

        result = mod.lightrag_search_entities(query="python")

        assert isinstance(result, dict)
        assert result["status"] == "ok"

    def test_search_no_results(self):
        """Should return no_results when no results found."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.query_data.return_value = None
        mod._adapter = mock_adapter
        mod.LightRAGAdapter._is_no_result = MagicMock(return_value=True)

        result = mod.lightrag_search_entities(query="nonexistent")

        assert result["status"] == "no_results"


class TestLightragGetGraph:
    """Test lightrag_get_graph tool function."""

    def test_explore_action(self):
        """explore action should call adapter.explore_node()."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.explore_node.return_value = {"nodes": [], "edges": []}
        mod._adapter = mock_adapter

        result = mod.lightrag_get_graph(action="explore", entity_name="Python", depth=2)

        mock_adapter.explore_node.assert_called_once_with(entity_name="Python", depth=2, edge_types=None)

    def test_snapshot_action(self):
        """snapshot action should call adapter.get_graph_snapshot()."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.get_graph_snapshot.return_value = {"nodes": [], "edges": []}
        mod._adapter = mock_adapter

        result = mod.lightrag_get_graph(action="snapshot", limit=100)

        mock_adapter.get_graph_snapshot.assert_called_once_with(limit=100)

    def test_explore_without_entity_name(self):
        """explore without entity_name should return error with consistent shape."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mod._adapter = mock_adapter

        result = mod.lightrag_get_graph(action="explore", entity_name="")

        assert result["status"] == "error"
        assert result["center"] is None

    def test_invalid_action(self):
        """Invalid action should return error."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mod._adapter = mock_adapter

        result = mod.lightrag_get_graph(action="invalid_action")

        assert result["status"] == "error"
        assert "Invalid action" in result["message"]


class TestLightragInsert:
    """Test lightrag_insert tool function."""

    def test_delegates_to_ingester(self):
        """lightrag_insert should delegate to LightRAGIngester.inject_document()."""
        mod = _import_module()
        mock_ingester = MagicMock()
        mock_ingester.inject_document.return_value = {"status": "ok"}
        mod._ingester = mock_ingester

        result = mod.lightrag_insert(content="test content", doc_id="doc1")

        mock_ingester.inject_document.assert_called_once_with(
            content="test content", doc_id="doc1", file_path=None,
        )


class TestLightragInsertCustomKg:
    """Test lightrag_insert_custom_kg tool function."""

    def test_delegates_to_ingester(self):
        """lightrag_insert_custom_kg should delegate to LightRAGIngester.inject_custom_kg()."""
        mod = _import_module()
        mock_ingester = MagicMock()
        mock_ingester.inject_custom_kg.return_value = {"status": "ok"}
        mod._ingester = mock_ingester

        entities = [{"entity_name": "Python", "entity_type": "skill"}]
        rels = [{"src_id": "A", "tgt_id": "B", "keywords": "uses"}]

        result = mod.lightrag_insert_custom_kg(entities=entities, relationships=rels)

        mock_ingester.inject_custom_kg.assert_called_once_with(
            entities=entities, relationships=rels, chunks=[], source_id="custom_kg",
        )

    def test_empty_params_default(self):
        """Should default to empty lists when no params provided."""
        mod = _import_module()
        mock_ingester = MagicMock()
        mock_ingester.inject_custom_kg.return_value = {"status": "ok"}
        mod._ingester = mock_ingester

        result = mod.lightrag_insert_custom_kg()

        mock_ingester.inject_custom_kg.assert_called_once_with(
            entities=[], relationships=[], chunks=[], source_id="custom_kg",
        )


class TestLightragInsertEntity:
    """Test lightrag_insert_entity tool function."""

    def test_delegates_to_ingester(self):
        """lightrag_insert_entity should delegate to inject_custom_kg with entity + anchor."""
        mod = _import_module()
        mock_ingester = MagicMock()
        mock_ingester.inject_custom_kg.return_value = {"status": "ok"}
        mod._ingester = mock_ingester

        result = mod.lightrag_insert_entity(name="Python", entity_type="skill")

        mock_ingester.inject_custom_kg.assert_called_once_with(
            entities=[{
                "entity_name": "Python", "entity_type": "skill",
                "description": "", "source_id": "custom_kg", "file_path": "custom_kg",
            }],
            relationships=[],
            chunks=[], source_id="custom_kg",
        )


class TestLightragInsertRelation:
    """Test lightrag_insert_relation tool function."""

    def test_delegates_to_ingester(self):
        """lightrag_insert_relation should delegate to inject_custom_kg with relationship."""
        mod = _import_module()
        mock_ingester = MagicMock()
        mock_ingester.inject_custom_kg.return_value = {"status": "ok"}
        mod._ingester = mock_ingester

        result = mod.lightrag_insert_relation(
            src_id="Python", tgt_id="Django", relation="has_framework",
        )

        mock_ingester.inject_custom_kg.assert_called_once_with(
            entities=[],
            relationships=[{
                "src_id": "Python", "tgt_id": "Django",
                "keywords": "has_framework", "description": "",
                "source_id": "custom_kg", "file_path": "custom_kg",
            }],
            chunks=[], source_id="custom_kg",
        )


class TestLightragDeleteEntity:
    """Test lightrag_delete_entity tool function."""

    def test_delegates_to_adapter(self):
        """lightrag_delete_entity should delegate to adapter.delete_entity()."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.delete_entity.return_value = {"status": "ok", "entity_name": "OldEntity"}
        mod._adapter = mock_adapter

        result = mod.lightrag_delete_entity(entity_name="OldEntity")

        mock_adapter.delete_entity.assert_called_once_with("OldEntity")
        assert result["status"] == "ok"

    def test_rag_not_available(self):
        """Should return error when LightRAG is not available."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.delete_entity.return_value = {"status": "error", "message": "LightRAG not available"}
        mod._adapter = mock_adapter

        result = mod.lightrag_delete_entity(entity_name="OldEntity")

        assert result["status"] == "error"


class TestLightragDocumentStatus:
    """Test lightrag_document_status tool function."""

    def test_delegates_to_adapter(self):
        """lightrag_document_status should delegate to adapter.document_status()."""
        mod = _import_module()
        mock_adapter = MagicMock()
        expected = {"pending": 0, "processing": 1, "processed": 10, "failed": 0}
        mock_adapter.document_status.return_value = expected
        mod._adapter = mock_adapter

        result = mod.lightrag_document_status()

        mock_adapter.document_status.assert_called_once()
        assert result == expected

    def test_rag_not_available(self):
        """Should return error when LightRAG is not available."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.document_status.return_value = {"status": "error", "message": "LightRAG not available"}
        mod._adapter = mock_adapter

        result = mod.lightrag_document_status()

        assert result["status"] == "error"


class TestLightragListEntities:
    """Test lightrag_list_entities tool function."""

    def test_list_labels(self):
        """list_type='labels' should delegate to adapter.list_entities()."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.list_entities.return_value = {"status": "ok", "data": ["person", "concept"]}
        mod._adapter = mock_adapter

        result = mod.lightrag_list_entities(list_type="labels")

        mock_adapter.list_entities.assert_called_once_with(
            list_type="labels", entity_type="", limit=50,
        )
        assert result["status"] == "ok"
        assert result["data"] == ["person", "concept"]

    def test_rag_not_available(self):
        """Should return error when LightRAG is not available."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.list_entities.return_value = {"status": "error", "message": "LightRAG not available"}
        mod._adapter = mock_adapter

        result = mod.lightrag_list_entities()

        assert result["status"] == "error"

    def test_invalid_list_type(self):
        """Invalid list_type should return error."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mod._adapter = mock_adapter

        result = mod.lightrag_list_entities(list_type="invalid_type")

        assert result["status"] == "error"
        assert "Invalid list_type" in result["message"]


class TestLightragMergeEntities:
    """Test lightrag_merge_entities tool function."""

    def test_delegates_to_adapter(self):
        """lightrag_merge_entities should delegate to adapter.merge_entities()."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.merge_entities.return_value = {"status": "ok", "target_entity": "李磊"}
        mod._adapter = mock_adapter

        result = mod.lightrag_merge_entities(
            source_entities=["小李", "李某某"],
            target_entity="李磊",
        )

        mock_adapter.merge_entities.assert_called_once_with(
            source_entities=["小李", "李某某"], target_entity="李磊",
        )
        assert result["status"] == "ok"
        assert result["target_entity"] == "李磊"

    def test_rag_not_available(self):
        """Should return error when LightRAG is not available."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.merge_entities.return_value = {"status": "error", "message": "LightRAG not available"}
        mod._adapter = mock_adapter

        result = mod.lightrag_merge_entities(
            source_entities=["A"], target_entity="B",
        )

        assert result["status"] == "error"


# ============== call_tool Dispatcher ==============


class TestCallTool:
    """Test call_tool dispatcher."""

    def test_dispatches_to_correct_function(self):
        """call_tool should route to the correct tool function."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.query.return_value = "result"
        mod._adapter = mock_adapter

        result = mod.call_tool("lightrag_query", {"query": "test"})

        mock_adapter.query.assert_called_once()

    def test_unknown_tool_raises(self):
        """call_tool with unknown name should raise ValueError."""
        mod = _import_module()

        with pytest.raises(ValueError, match="Unknown lightrag-server tool"):
            mod.call_tool("nonexistent_tool", {})

    def test_extra_arguments_filtered(self):
        """call_tool should filter out unknown arguments."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.query.return_value = "result"
        mod._adapter = mock_adapter

        # Pass an extra argument that lightrag_query doesn't accept
        result = mod.call_tool("lightrag_query", {
            "query": "test",
            "nonexistent_param": "should_be_filtered",
        })

        mock_adapter.query.assert_called_once_with(
            query="test", mode="mix",
            only_need_context=True, top_k=5,
            response_type="%s" % "Multiple Paragraphs",
        )

    def test_all_tools_dispatchable(self):
        """All 12 tool names should be dispatchable via call_tool."""
        mod = _import_module()
        for name in mod.TOOL_SCHEMAS:
            assert name in mod._TOOL_FUNCTIONS, f"{name} not in _TOOL_FUNCTIONS"


# ============== Backward Compatibility Aliases ==============


class TestDeprecatedAliases:
    """Test backward compatibility alias mapping."""

    def test_aliases_exist(self):
        """DEPRECATED_ALIASES should map old tool names to new ones."""
        mod = _import_module()
        assert len(mod.DEPRECATED_ALIASES) > 0

    def test_all_aliases_map_to_valid_tools(self):
        """Every alias should map to an existing tool name."""
        mod = _import_module()
        for alias, target in mod.DEPRECATED_ALIASES.items():
            assert target in mod.TOOL_SCHEMAS, (
                f"Alias '{alias}' maps to '{target}' which is not a valid tool"
            )

    def test_vector_store_aliases(self):
        """Key vector-store aliases should be present."""
        mod = _import_module()
        aliases = mod.DEPRECATED_ALIASES
        assert "search_documents" in aliases
        assert aliases["search_documents"] == "lightrag_query"

    def test_kg_server_aliases(self):
        """Key kg-server aliases should be present."""
        mod = _import_module()
        aliases = mod.DEPRECATED_ALIASES
        assert "create_entity" in aliases
        assert aliases["create_entity"] == "lightrag_insert_entity"
        assert "explore_node" in aliases
        assert aliases["explore_node"] == "lightrag_get_graph"
