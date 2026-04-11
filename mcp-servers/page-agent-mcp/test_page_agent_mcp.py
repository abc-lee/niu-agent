"""
Test Page Agent MCP Integration

Tests for HTTP client connecting to page-agent-mcp REST API.
"""
import pytest
import json
from unittest.mock import patch, MagicMock


# Import the module to test
import sys
sys.path.insert(0, 'E:\\tools\\ai-bot\\mcp-servers\\page-agent-mcp\\src')
from niu_page_agent import execute_task, get_status, stop_task


class TestGetStatus:
    """Test get_status function"""

    @pytest.mark.asyncio
    async def test_get_status_returns_json_string(self):
        """Test that get_status returns a JSON string"""
        result = get_status()
        # Should be valid JSON
        data = json.loads(result)
        assert isinstance(data, dict)
        assert "connected" in data
        assert "busy" in data

    @pytest.mark.asyncio
    async def test_get_status_when_service_running(self):
        """Test get_status when service is running"""
        result = get_status()
        data = json.loads(result)
        # Service should be running in this test environment
        assert isinstance(data["connected"], bool)
        assert isinstance(data["busy"], bool)


class TestExecuteTask:
    """Test execute_task function"""

    @pytest.mark.asyncio
    async def test_execute_task_returns_string(self):
        """Test that execute_task returns a string"""
        result = execute_task("test task")
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_execute_task_without_hub_connection(self):
        """Test execute_task when Chrome extension is not connected"""
        # Expected: return error message about hub not connected
        result = execute_task("test task")
        assert "Error" in result or "failed" in result.lower()


class TestStopTask:
    """Test stop_task function"""

    @pytest.mark.asyncio
    async def test_stop_task_returns_string(self):
        """Test that stop_task returns a string"""
        result = stop_task()
        assert isinstance(result, str)


class TestToolSchemas:
    """Test tool schema definitions"""

    def test_tool_schemas_exist(self):
        """Test that all required tool schemas exist"""
        from niu_page_agent import TOOL_SCHEMAS

        assert "execute_task" in TOOL_SCHEMAS
        assert "get_status" in TOOL_SCHEMAS
        assert "stop_task" in TOOL_SCHEMAS

    def test_tool_schemas_structure(self):
        """Test that tool schemas have required fields"""
        from niu_page_agent import TOOL_SCHEMAS

        for tool_name, schema in TOOL_SCHEMAS.items():
            assert "description" in schema
            assert "input_schema" in schema
            assert schema["input_schema"]["type"] == "object"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
