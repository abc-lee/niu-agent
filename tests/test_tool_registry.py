"""
Test ToolRegistry - P0 Tests

ToolRegistry is the foundation for in-process MCP tool management.
"""

import pytest
import sys
from typing import Dict, Any, List, Optional

sys.path.insert(0, "E:/tools/ai-bot")


# ============================================================================
# Mock Module for Testing
# ============================================================================

class MockTool:
    """Mock Tool object mimicking mcp.types.Tool"""
    def __init__(self, name: str, description: str, input_schema: Dict[str, Any]):
        self.name = name
        self.description = description
        self.inputSchema = input_schema  # Note: MCP uses inputSchema (camelCase)


class MockMCPModule:
    """Mock MCP server module for testing"""

    @staticmethod
    def get_tool_schemas() -> List[Dict[str, Any]]:
        """Return tool schemas in the expected format"""
        return [
            {
                "name": "test_tool",
                "description": "A test tool",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "param1": {"type": "string", "description": "First parameter"}
                    },
                    "required": ["param1"]
                }
            },
            {
                "name": "another_tool",
                "description": "Another test tool",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "integer", "description": "A number"}
                    }
                }
            }
        ]

    @staticmethod
    def test_tool(param1: str) -> Dict[str, Any]:
        """Test tool implementation"""
        return {"status": "success", "param1": param1}

    @staticmethod
    def another_tool(value: int = 0) -> Dict[str, Any]:
        """Another test tool implementation"""
        return {"status": "success", "value": value}


class MockMCPModuleWithTools:
    """Mock module that provides tool functions via tools dict"""

    @staticmethod
    def get_tool_schemas() -> List[Dict[str, Any]]:
        return [
            {
                "name": "calc_add",
                "description": "Add two numbers",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "number"},
                        "b": {"type": "number"}
                    },
                    "required": ["a", "b"]
                }
            }
        ]

    @staticmethod
    def calc_add(a: float, b: float) -> Dict[str, Any]:
        """Calculate addition"""
        return {"result": a + b}


# ============================================================================
# Test Cases
# ============================================================================

@pytest.mark.p0
class TestToolRegistryBasics:
    """Test basic ToolRegistry functionality"""

    def test_import_tool_registry(self):
        """Test that ToolRegistry can be imported"""
        from agent.tool_registry import ToolRegistry
        assert ToolRegistry is not None

    def test_create_tool_registry(self):
        """Test creating a ToolRegistry instance"""
        from agent.tool_registry import ToolRegistry
        registry = ToolRegistry()
        assert registry is not None

    def test_register_server_returns_true(self):
        """Test that register_server returns True on success"""
        from agent.tool_registry import ToolRegistry
        registry = ToolRegistry()
        result = registry.register_server("test-server", MockMCPModule)
        assert result is True

    def test_get_tool_function(self):
        """Test retrieving a registered tool function"""
        from agent.tool_registry import ToolRegistry
        registry = ToolRegistry()
        registry.register_server("test-server", MockMCPModule)

        # Get tool by full name (server/tool)
        tool_fn = registry.get("test-server/test_tool")
        assert tool_fn is not None
        assert callable(tool_fn)

        # Verify the function works
        result = tool_fn(param1="hello")
        assert result == {"status": "success", "param1": "hello"}

    def test_get_nonexistent_tool_returns_none(self):
        """Test that getting a non-existent tool returns None"""
        from agent.tool_registry import ToolRegistry
        registry = ToolRegistry()

        # Empty registry
        assert registry.get("server/tool") is None

        # Wrong tool name
        registry.register_server("test-server", MockMCPModule)
        assert registry.get("test-server/nonexistent") is None
        assert registry.get("wrong-server/test_tool") is None

    def test_get_schemas_returns_list(self):
        """Test that get_schemas returns tool schema list"""
        from agent.tool_registry import ToolRegistry
        registry = ToolRegistry()
        registry.register_server("test-server", MockMCPModule)

        schemas = registry.get_schemas()
        assert isinstance(schemas, list)
        assert len(schemas) == 2

        # Check schema format
        for schema in schemas:
            assert "name" in schema
            assert "description" in schema
            assert "input_schema" in schema
            # Name should be prefixed with server name
            assert schema["name"].startswith("test-server/")

    def test_register_multiple_servers(self):
        """Test registering tools from multiple servers"""
        from agent.tool_registry import ToolRegistry
        registry = ToolRegistry()

        registry.register_server("server-a", MockMCPModule)
        registry.register_server("server-b", MockMCPModuleWithTools)

        # Check schemas from both servers
        schemas = registry.get_schemas()
        assert len(schemas) == 3  # 2 from server-a + 1 from server-b

        # Check tools from both servers
        tool_a = registry.get("server-a/test_tool")
        assert tool_a is not None

        tool_b = registry.get("server-b/calc_add")
        assert tool_b is not None

        # Verify tool works
        result = tool_b(a=1, b=2)
        assert result == {"result": 3}

    def test_register_server_without_get_tool_schemas(self):
        """Test registering a module without get_tool_schemas returns False"""
        from agent.tool_registry import ToolRegistry
        registry = ToolRegistry()

        class BadModule:
            pass

        result = registry.register_server("bad-server", BadModule)
        assert result is False


@pytest.mark.p0
class TestGlobalRegistry:
    """Test global registry instance management"""

    def test_get_registry_returns_instance(self):
        """Test that get_registry returns a ToolRegistry instance"""
        from agent.tool_registry import get_registry
        registry = get_registry()
        assert registry is not None
        from agent.tool_registry import ToolRegistry
        assert isinstance(registry, ToolRegistry)

    def test_get_registry_returns_same_instance(self):
        """Test that get_registry returns the same instance"""
        from agent.tool_registry import get_registry
        registry1 = get_registry()
        registry2 = get_registry()
        assert registry1 is registry2

    def test_set_registry_replaces_instance(self):
        """Test that set_registry replaces the global instance"""
        from agent.tool_registry import get_registry, set_registry, ToolRegistry

        # Create new instance
        new_registry = ToolRegistry()
        new_registry.register_server("custom-server", MockMCPModule)

        # Set it as global
        set_registry(new_registry)

        # Verify it's now the global instance
        registry = get_registry()
        assert registry is new_registry
        assert registry.get("custom-server/test_tool") is not None


@pytest.mark.p0
class TestToolRegistryEdgeCases:
    """Test edge cases and error handling"""

    def test_register_same_server_twice(self):
        """Test that re-registering a server overwrites previous tools"""
        from agent.tool_registry import ToolRegistry
        registry = ToolRegistry()

        registry.register_server("test-server", MockMCPModule)
        assert len(registry.get_schemas()) == 2

        # Register different module with same server name
        registry.register_server("test-server", MockMCPModuleWithTools)
        schemas = registry.get_schemas()
        assert len(schemas) == 1  # Should have only the new tools
        assert schemas[0]["name"] == "test-server/calc_add"

    def test_tool_name_with_slash_in_tool_name(self):
        """Test tool name containing slash in the tool part"""
        from agent.tool_registry import ToolRegistry
        registry = ToolRegistry()

        class SlashToolModule:
            @staticmethod
            def get_tool_schemas():
                return [{
                    "name": "tool/with/slash",
                    "description": "Tool with slash in name",
                    "input_schema": {"type": "object"}
                }]

            @staticmethod
            def get_tool_function(name: str):
                if name == "tool/with/slash":
                    return lambda: {"ok": True}

        # This is an edge case - tool name with slash
        # The expected behavior: server-name/tool/with/slash
        result = registry.register_server("server", SlashToolModule)
        assert result is True

        # Should be able to get it with full name
        schemas = registry.get_schemas()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "server/tool/with/slash"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "p0"])
