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
