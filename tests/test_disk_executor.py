"""Tests for disk_executor.py — Tool execution and argument validation."""

import json
from unittest.mock import MagicMock

import pytest
import yaml

from niu_api.internal.disk_config import DiskConfig
from niu_api.internal.disk_executor import DiskExecutor
from niu_api.internal.disk_parser import ParsedCommand, DiskParser


@pytest.fixture
def config(tmp_path):
    """Create a valid config with detailed tool definitions."""
    config_dir = tmp_path / "disk"
    config_dir.mkdir()
    (config_dir / "disk.yaml").write_text(yaml.dump({"version": 1}))
    (config_dir / "kg-server.yaml").write_text(yaml.dump({
        "server": "kg-server", "directory": "kg", "description": "知识图谱",
        "tools": {
            "explore_node": {
                "summary": "探索实体邻居", "description": "从实体出发探索邻居。",
                "args": [
                    {"name": "entity_id", "position": 1, "type": "string", "required": True, "description": "实体ID"},
                    {"name": "depth", "type": "integer", "default": 2, "flag": "depth", "description": "遍历深度",
                     "constraints": {"minimum": 1, "maximum": 5}},
                    {"name": "min_confidence", "type": "number", "default": 0.0, "flag": "min-confidence",
                     "description": "最小置信度", "constraints": {"minimum": 0.0, "maximum": 1.0}},
                    {"name": "direction", "type": "string", "flag": "direction",
                     "enum": ["both", "outgoing", "incoming"], "default": "both", "description": "方向"},
                ],
                "examples": ["/kg/explore_node Einstein"],
                "mutually_exclusive": [],
            },
            "graph_stats": {
                "summary": "图谱统计", "description": "获取统计信息。",
                "args": [],
            },
            "find_path": {
                "summary": "查找路径", "description": "查找两个实体间路径。",
                "args": [
                    {"name": "from_entity", "position": 1, "type": "string", "required": True, "description": "起始实体"},
                    {"name": "to_entity", "position": 2, "type": "string", "required": True, "description": "目标实体"},
                    {"name": "max_depth", "type": "integer", "default": 5, "flag": "max-depth", "description": "最大深度"},
                ],
            },
            "delete_doc": {
                "summary": "删除文档", "description": "删除文档。",
                "args": [
                    {"name": "uri", "type": "string", "flag": "uri", "description": "文档URI"},
                    {"name": "title", "type": "string", "flag": "title", "description": "文档标题"},
                ],
                "mutually_exclusive": [["uri", "title"]],
            },
        },
    }))
    (config_dir / "memory-server.yaml").write_text(yaml.dump({
        "server": "memory-server", "directory": "memory", "description": "记忆",
        "tools": {
            "remember": {
                "summary": "保存记忆", "description": "保存长期记忆。",
                "args": [
                    {"name": "content", "position": 1, "type": "string", "required": True, "description": "记忆内容"},
                    {"name": "metadata", "type": "object", "flag": "metadata",
                     "cli_format": "json", "description": "元数据"},
                ],
            },
        },
    }))
    return DiskConfig(str(config_dir))


@pytest.fixture
def mock_registry():
    """Mock ToolRegistry."""
    reg = MagicMock()
    reg._schemas = {}
    reg.get = MagicMock(return_value=MagicMock(return_value={"status": "success", "data": "ok"}))
    return reg


@pytest.fixture
def executor(config, mock_registry):
    return DiskExecutor(config, mock_registry)


@pytest.fixture
def parser():
    return DiskParser()


# ---------------------------------------------------------------------------
# Correct execution
# ---------------------------------------------------------------------------

class TestCorrectExecution:
    def test_positional_arg(self, executor, parser, mock_registry):
        parsed = parser.parse("/kg/explore_node Einstein")
        result = executor.execute(parsed)
        # Should call the real MCP function
        mock_registry.get.assert_called_once()
        call_kwargs = mock_registry.get.return_value.call_args[1]
        assert call_kwargs["entity_id"] == "Einstein"

    def test_flag_args(self, executor, parser, mock_registry):
        parsed = parser.parse("/kg/explore_node Einstein --depth 3")
        executor.execute(parsed)
        call_kwargs = mock_registry.get.return_value.call_args[1]
        assert call_kwargs["entity_id"] == "Einstein"
        assert call_kwargs["depth"] == 3

    def test_mixed_positional_and_flags(self, executor, parser, mock_registry):
        parsed = parser.parse("/kg/find_path Einstein Bohr --max-depth 3")
        executor.execute(parsed)
        call_kwargs = mock_registry.get.return_value.call_args[1]
        assert call_kwargs["from_entity"] == "Einstein"
        assert call_kwargs["to_entity"] == "Bohr"
        assert call_kwargs["max_depth"] == 3

    def test_no_args_tool(self, executor, parser, mock_registry):
        parsed = parser.parse("/kg/graph_stats")
        result = executor.execute(parsed)
        mock_registry.get.assert_called_once()

    def test_default_values_applied(self, executor, parser, mock_registry):
        parsed = parser.parse("/kg/explore_node Einstein")
        executor.execute(parsed)
        call_kwargs = mock_registry.get.return_value.call_args[1]
        # Defaults should be sent
        assert call_kwargs["depth"] == 2
        assert call_kwargs["min_confidence"] == 0.0
        assert call_kwargs["direction"] == "both"

    def test_object_arg_json(self, executor, parser, mock_registry):
        parsed = parser.parse('/memory/remember "hello" --metadata \'{"source":"chat"}\'')
        result = executor.execute(parsed)
        # Should parse JSON and pass as dict
        call_kwargs = mock_registry.get.return_value.call_args[1]
        assert call_kwargs["metadata"] == {"source": "chat"}


# ---------------------------------------------------------------------------
# Parameter validation errors
# ---------------------------------------------------------------------------

class TestParameterErrors:
    def test_e5_missing_required(self, executor, parser):
        parsed = parser.parse("/kg/explore_node")
        result = executor.execute(parsed)
        assert isinstance(result, str)  # Error message
        assert "entity_id" in result

    def test_e5_self_contained(self, executor, parser):
        """E5 must include ALL parameters."""
        parsed = parser.parse("/kg/explore_node")
        result = executor.execute(parsed)
        assert "--depth" in result
        assert "--min-confidence" in result
        assert "--direction" in result

    def test_e6_type_mismatch(self, executor, parser):
        parsed = parser.parse("/kg/explore_node Einstein --depth abc")
        result = executor.execute(parsed)
        assert isinstance(result, str)
        assert "depth" in result
        assert "integer" in result.lower()

    def test_e7_unknown_flag_with_suggestion(self, executor, parser):
        parsed = parser.parse("/kg/explore_node Einstein --depht 3")
        result = executor.execute(parsed)
        assert isinstance(result, str)
        assert "depht" in result
        assert "depth" in result

    def test_e8_enum_error(self, executor, parser):
        parsed = parser.parse("/kg/explore_node Einstein --direction sideways")
        result = executor.execute(parsed)
        assert isinstance(result, str)
        assert "sideways" in result
        assert "both" in result

    def test_e9_out_of_range(self, executor, parser):
        parsed = parser.parse("/kg/explore_node Einstein --depth 10")
        result = executor.execute(parsed)
        assert isinstance(result, str)
        assert "10" in result

    def test_e10_too_many_args(self, executor, parser):
        parsed = parser.parse("/kg/explore_node Einstein ExtraArg")
        result = executor.execute(parsed)
        assert isinstance(result, str)
        assert "too many" in result.lower()

    def test_e14_flag_missing_value(self, executor, parser):
        parsed = parser.parse("/kg/explore_node Einstein --depth")
        result = executor.execute(parsed)
        assert isinstance(result, str)
        assert "depth" in result

    def test_e18_mutually_exclusive(self, executor, parser):
        parsed = parser.parse("/kg/delete_doc --uri x --title y")
        result = executor.execute(parsed)
        assert isinstance(result, str)
        assert "mutually exclusive" in result.lower()


# ---------------------------------------------------------------------------
# Repeated errors escalation (E17)
# ---------------------------------------------------------------------------

class TestE17Escalation:
    def test_first_error_no_escalation(self, executor, parser):
        parsed = parser.parse("/kg/explore_node")
        result = executor.execute(parsed)
        assert "repeated" not in result.lower()

    def test_third_error_escalates(self, executor, parser):
        # First two errors
        parsed = parser.parse("/kg/explore_node")
        executor.execute(parsed)
        executor.execute(parsed)
        # Third error
        result = executor.execute(parsed)
        assert "repeated" in result.lower()

    def test_escalation_includes_full_usage(self, executor, parser):
        parsed = parser.parse("/kg/explore_node")
        executor.execute(parsed)
        executor.execute(parsed)
        result = executor.execute(parsed)
        assert "entity_id" in result
        assert "--depth" in result


# ---------------------------------------------------------------------------
# MCP error passthrough (E12)
# ---------------------------------------------------------------------------

class TestMCPErrorPassthrough:
    def test_tool_raises_exception(self, executor, parser, mock_registry):
        mock_registry.get.return_value = MagicMock(side_effect=ValueError("Entity not found"))
        parsed = parser.parse("/kg/explore_node Einstein")
        result = executor.execute(parsed)
        assert isinstance(result, str)
        assert "execution failed" in result.lower()

    def test_tool_returns_error_status(self, executor, parser, mock_registry):
        mock_registry.get.return_value = MagicMock(return_value={"status": "error", "message": "Not found"})
        parsed = parser.parse("/kg/explore_node Einstein")
        result = executor.execute(parsed)
        # Error dict is returned as-is (raw result)
        assert isinstance(result, dict)
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# First error tip
# ---------------------------------------------------------------------------

class TestFirstErrorTip:
    def test_first_error_includes_tip(self, executor, parser):
        parsed = parser.parse("/kg/explore_node")
        result = executor.execute(parsed)
        assert "Tip" in result
        assert "cat /kg/explore_node" in result

    def test_second_error_no_tip(self, executor, parser):
        parsed = parser.parse("/kg/explore_node")
        executor.execute(parsed)
        result = executor.execute(parsed)
        assert "Tip" not in result or "cat" not in result.split("Tip")[0]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_unknown_directory_in_execute(self, executor, parser):
        parsed = parser.parse("/nonexist/tool arg1")
        result = executor.execute(parsed)
        assert isinstance(result, str)
        assert "nonexist" in result.lower() or "No such" in result

    def test_unknown_tool_in_execute(self, executor, parser):
        parsed = parser.parse("/kg/nonexist_tool arg1")
        result = executor.execute(parsed)
        assert isinstance(result, str)
        assert "nonexist_tool" in result.lower() or "No such" in result

    def test_kebab_flag_to_snake_case(self, executor, parser, mock_registry):
        parsed = parser.parse("/kg/explore_node Einstein --min-confidence 0.5")
        executor.execute(parsed)
        call_kwargs = mock_registry.get.return_value.call_args[1]
        assert "min_confidence" in call_kwargs
        assert call_kwargs["min_confidence"] == 0.5
