"""Tests for subagent LightRAG native migration."""


class TestLightragGetDocument:
    """Test lightrag_get_document tool."""

    def test_get_document_schema_exists(self):
        """lightrag_get_document should be in TOOL_SCHEMAS."""
        from niu_lightrag_server import TOOL_SCHEMAS
        assert "lightrag_get_document" in TOOL_SCHEMAS

    def test_get_document_schema_has_required_params(self):
        """lightrag_get_document should require doc_id parameter."""
        from niu_lightrag_server import TOOL_SCHEMAS
        schema = TOOL_SCHEMAS["lightrag_get_document"]
        assert "doc_id" in schema["input_schema"]["properties"]
        assert "doc_id" in schema["input_schema"]["required"]

    def test_get_document_schema_description(self):
        """lightrag_get_document should have meaningful description."""
        from niu_lightrag_server import TOOL_SCHEMAS
        schema = TOOL_SCHEMAS["lightrag_get_document"]
        assert "完整文档" in schema["description"] or "full doc" in schema["description"].lower()

    def test_get_document_in_tool_functions(self):
        """lightrag_get_document should be in _TOOL_FUNCTIONS."""
        from niu_lightrag_server import _TOOL_FUNCTIONS
        assert "lightrag_get_document" in _TOOL_FUNCTIONS


class TestLightragDeleteDocument:
    """Test lightrag_delete_document tool."""

    def test_delete_document_schema_exists(self):
        """lightrag_delete_document should be in TOOL_SCHEMAS."""
        from niu_lightrag_server import TOOL_SCHEMAS
        assert "lightrag_delete_document" in TOOL_SCHEMAS

    def test_delete_document_schema_has_required_params(self):
        """lightrag_delete_document should require doc_id parameter."""
        from niu_lightrag_server import TOOL_SCHEMAS
        schema = TOOL_SCHEMAS["lightrag_delete_document"]
        assert "doc_id" in schema["input_schema"]["properties"]
        assert "doc_id" in schema["input_schema"]["required"]

    def test_delete_document_schema_description(self):
        """lightrag_delete_document should mention cascade deletion."""
        from niu_lightrag_server import TOOL_SCHEMAS
        schema = TOOL_SCHEMAS["lightrag_delete_document"]
        desc = schema["description"].lower()
        assert "级联" in desc or "cascade" in desc or "文档" in desc

    def test_delete_document_in_tool_functions(self):
        """lightrag_delete_document should be in _TOOL_FUNCTIONS."""
        from niu_lightrag_server import _TOOL_FUNCTIONS
        assert "lightrag_delete_document" in _TOOL_FUNCTIONS


class TestSubagentMigrationIntegration:
    """Integration tests for the complete migration."""

    def test_all_subagent_configs_valid(self):
        """All sub-agent config files should parse correctly."""
        from agent.subagent import get_subagent_config
        for name in ["dream-evolver", "entity-extractor", "context-manager", "event-manager"]:
            cfg = get_subagent_config(name)
            assert cfg["name"] == name
            assert "mcpServers" in cfg

    def test_context_manager_no_lightrag(self):
        """context-manager should NOT have lightrag-server (pure compressor)."""
        from agent.subagent import get_subagent_config
        cfg = get_subagent_config("context-manager")
        assert "lightrag-server" not in cfg["mcpServers"]

    def test_dream_evolver_has_lightrag(self):
        """dream-evolver should have lightrag-server."""
        from agent.subagent import get_subagent_config
        cfg = get_subagent_config("dream-evolver")
        assert "lightrag-server" in cfg["mcpServers"]

    def test_kg_enricher_removed(self):
        """kg-enricher config file should not exist."""
        from pathlib import Path
        assert not Path("config/agents/kg-enricher.md").exists()

    def test_handler_aliases_correct(self):
        """handler.py _TOOL_ALIASES should not contain vector-store entries (removed)."""
        from agent.handler import NiuHandler
        aliases = NiuHandler._TOOL_ALIASES
        assert "vector-store/get_document" not in aliases
        assert "vector-store/delete_document" not in aliases

    def test_deprecated_aliases_correct(self):
        """DEPRECATED_ALIASES should use correct new mappings."""
        from niu_lightrag_server import DEPRECATED_ALIASES
        assert DEPRECATED_ALIASES.get("get_document") == "lightrag_get_document"
        assert DEPRECATED_ALIASES.get("delete_document") == "lightrag_delete_document"
        assert "update_metadata" not in DEPRECATED_ALIASES

    def test_lightrag_new_tools_in_schemas(self):
        """lightrag_get_document and lightrag_delete_document should be in TOOL_SCHEMAS."""
        from niu_lightrag_server import TOOL_SCHEMAS
        assert "lightrag_get_document" in TOOL_SCHEMAS
        assert "lightrag_delete_document" in TOOL_SCHEMAS

    def test_region_manager_has_decay_structural_edges(self):
        """RegionManager should have decay_structural_edges method, and module-level _decay_brain_region_edges function."""
        from niu_api.internal.region_manager import RegionManager, _decay_brain_region_edges
        assert hasattr(RegionManager, "decay_structural_edges")
        assert callable(_decay_brain_region_edges)