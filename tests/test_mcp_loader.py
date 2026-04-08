"""
Test MCP Loader - P0 Tests

MCP Loader loads all required MCP modules at startup with strict validation.
"""

import pytest
import sys
import os
from typing import Dict, Any, List

sys.path.insert(0, "E:/tools/ai-bot")


# ============================================================================
# Mock Module for Testing
# ============================================================================

class MockMCPModule:
    """Mock MCP server module for testing"""

    @staticmethod
    def get_tool_schemas() -> List[Dict[str, Any]]:
        """Return tool schemas in the expected format"""
        return [
            {
                "name": "mock_tool",
                "description": "A mock tool for testing",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "param": {"type": "string", "description": "Test parameter"}
                    },
                    "required": ["param"]
                }
            }
        ]

    @staticmethod
    def mock_tool(param: str) -> Dict[str, Any]:
        """Mock tool implementation"""
        return {"status": "success", "param": param}


# ============================================================================
# Test Cases
# ============================================================================

@pytest.mark.p0
class TestMCPLoaderBasics:
    """Test basic MCP loader functionality"""

    def test_import_mcp_loader(self):
        """Test that load_mcp_tools can be imported"""
        from agent.mcp_loader import load_mcp_tools
        assert load_mcp_tools is not None

    def test_load_mcp_tools_returns_registry(self, monkeypatch):
        """Test that load_mcp_tools returns a ToolRegistry instance"""
        # Create a mock module
        import sys
        sys.modules['niu_test_server'] = MockMCPModule

        try:
            from agent.mcp_loader import load_mcp_tools
            from agent.tool_registry import ToolRegistry, get_registry, reset_registry

            # Reset registry before test
            reset_registry()

            # Use parameter instead of modifying REQUIRED_SERVERS
            registry = load_mcp_tools([("test-server", "niu_test_server")])
            assert registry is not None
            assert isinstance(registry, ToolRegistry)

            # Verify global registry is set
            global_registry = get_registry()
            assert global_registry is registry
        finally:
            if 'niu_test_server' in sys.modules:
                del sys.modules['niu_test_server']

    def test_load_mcp_tools_with_mock_module(self, monkeypatch):
        """Test loading MCP tools with a mock module"""
        # Create a mock module in sys.modules
        import sys
        sys.modules['niu_test_server'] = MockMCPModule

        try:
            from agent.mcp_loader import load_mcp_tools
            from agent.tool_registry import reset_registry

            # Reset registry before test
            reset_registry()

            # Use parameter instead of modifying REQUIRED_SERVERS
            registry = load_mcp_tools([("test-server", "niu_test_server")])

            # Check that tool was loaded
            schemas = registry.get_schemas()
            assert len(schemas) == 1
            assert schemas[0]["name"] == "test-server/mock_tool"

            # Check that tool function works
            tool_fn = registry.get("test-server/mock_tool")
            assert tool_fn is not None
            result = tool_fn(param="hello")
            assert result == {"status": "success", "param": "hello"}

        finally:
            # Clean up mock module
            if 'niu_test_server' in sys.modules:
                del sys.modules['niu_test_server']


@pytest.mark.p0
class TestMCPLoaderErrorHandling:
    """Test error handling in MCP loader"""

    def test_load_mcp_tools_missing_module_raises_runtime_error(self, monkeypatch):
        """Test that missing module raises RuntimeError with details"""
        from agent.mcp_loader import load_mcp_tools

        # Use parameter with non-existent module
        with pytest.raises(RuntimeError) as exc_info:
            load_mcp_tools([("missing-server", "niu_nonexistent_module")])

        # Check error message contains server name
        assert "missing-server" in str(exc_info.value)
        assert "import failed" in str(exc_info.value)

    def test_load_mcp_tools_partial_failure_raises_runtime_error(self, monkeypatch):
        """Test that partial module loading failure raises RuntimeError"""
        import sys

        # Create one valid mock module
        sys.modules['niu_valid_server'] = MockMCPModule

        try:
            from agent.mcp_loader import load_mcp_tools

            # Mix valid and invalid servers using parameter
            with pytest.raises(RuntimeError) as exc_info:
                load_mcp_tools([
                    ("valid-server", "niu_valid_server"),
                    ("invalid-server", "niu_nonexistent_module")
                ])

            # Check error message lists the failed server
            error_msg = str(exc_info.value)
            assert "invalid-server" in error_msg
            assert "import failed" in error_msg

        finally:
            if 'niu_valid_server' in sys.modules:
                del sys.modules['niu_valid_server']


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "p0"])
