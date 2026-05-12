"""Tests for disk_engine.py — Main engine integration."""

from unittest.mock import MagicMock

import pytest
import yaml

from niu_api.internal.disk_engine import DiskEngine, DiskResult


@pytest.fixture
def config_dir(tmp_path):
    """Create a full config directory for integration testing."""
    d = tmp_path / "disk"
    d.mkdir()

    (d / "disk.yaml").write_text(yaml.dump({
        "version": 1,
        "exclude_tools": ["nanobot.system/code_run"],
        "disk_mode": True,
    }))

    (d / "kg-server.yaml").write_text(yaml.dump({
        "server": "kg-server", "directory": "kg", "description": "知识图谱",
        "tools": {
            "explore_node": {
                "summary": "探索实体邻居", "description": "从实体出发探索邻居。",
                "category": "explore",
                "args": [
                    {"name": "entity_id", "position": 1, "type": "string", "required": True, "description": "实体ID"},
                    {"name": "depth", "type": "integer", "default": 2, "flag": "depth", "description": "遍历深度",
                     "constraints": {"minimum": 1, "maximum": 5}},
                ],
                "examples": ["/kg/explore_node Einstein"],
            },
            "graph_stats": {
                "summary": "图谱统计", "description": "统计信息。",
                "args": [],
            },
        },
    }))

    (d / "memory-server.yaml").write_text(yaml.dump({
        "server": "memory-server", "directory": "memory", "description": "记忆系统",
        "tools": {
            "remember": {
                "summary": "保存记忆", "description": "保存长期记忆。",
                "args": [
                    {"name": "content", "position": 1, "type": "string", "required": True, "description": "内容"},
                    {"name": "memory_type", "flag": "type", "type": "string", "required": True,
                     "enum": ["environment", "preferences", "skills"], "description": "类型"},
                ],
            },
        },
    }))

    return d


@pytest.fixture
def mock_registry():
    reg = MagicMock()
    reg._schemas = {}
    reg.get = MagicMock(return_value=MagicMock(return_value={"status": "success", "entities": []}))
    return reg


@pytest.fixture
def engine(config_dir, mock_registry):
    return DiskEngine(str(config_dir), mock_registry)


# ---------------------------------------------------------------------------
# Full chain: ls → cat → execute
# ---------------------------------------------------------------------------

class TestFullChain:
    def test_ls_root(self, engine):
        result = engine.execute("ls /")
        assert result.action == "LIST"
        assert "kg/" in result.text
        assert "memory/" in result.text

    def test_ls_subdir(self, engine):
        result = engine.execute("ls /kg")
        assert result.action == "LIST"
        assert "explore_node" in result.text

    def test_cat_tool(self, engine):
        result = engine.execute("cat /kg/explore_node")
        assert result.action == "READ"
        assert "entity_id" in result.text
        assert "USAGE" in result.text

    def test_execute_tool(self, engine, mock_registry):
        result = engine.execute("/kg/explore_node Einstein")
        assert result.action == "EXECUTE"
        assert result.raw_result == {"status": "success", "entities": []}
        assert result.tool_path == "/kg/explore_node"
        mock_registry.get.assert_called_once()


# ---------------------------------------------------------------------------
# Error → self-repair chains
# ---------------------------------------------------------------------------

class TestErrorSelfRepair:
    def test_e5_then_fix(self, engine, mock_registry):
        # Missing required arg
        r1 = engine.execute("/kg/explore_node")
        assert r1.action == "ERROR"
        assert "entity_id" in r1.text

        # Fix: provide the arg
        r2 = engine.execute("/kg/explore_node Einstein")
        assert r2.action == "EXECUTE"
        assert r2.raw_result is not None

    def test_e7_typo_then_fix(self, engine, mock_registry):
        # Typo in flag name
        r1 = engine.execute("/kg/explore_node Einstein --depht 3")
        assert r1.action == "ERROR"
        assert "depht" in r1.text

        # Fix: correct flag name
        r2 = engine.execute("/kg/explore_node Einstein --depth 3")
        assert r2.action == "EXECUTE"

    def test_e17_escalation(self, engine):
        # Three consecutive errors trigger escalation
        engine.execute("/kg/explore_node")
        engine.execute("/kg/explore_node")
        r3 = engine.execute("/kg/explore_node")
        assert r3.action == "ERROR"
        assert "repeated" in r3.text.lower()


# ---------------------------------------------------------------------------
# DiskResult type correctness
# ---------------------------------------------------------------------------

class TestDiskResultTypes:
    def test_navigation_returns_text(self, engine):
        result = engine.execute("ls /")
        assert result.action in ("LIST", "READ", "HELP")
        assert isinstance(result.text, str)
        assert result.raw_result is None

    def test_execute_returns_raw(self, engine, mock_registry):
        result = engine.execute("/kg/graph_stats")
        assert result.action == "EXECUTE"
        assert result.raw_result is not None
        assert isinstance(result.raw_result, dict)

    def test_error_returns_text(self, engine):
        result = engine.execute("/kg/explore_node")
        assert result.action == "ERROR"
        assert isinstance(result.text, str)
        assert result.raw_result is None


# ---------------------------------------------------------------------------
# Schema generation
# ---------------------------------------------------------------------------

class TestSchemaGeneration:
    def test_disk_schema_format(self, engine):
        schema = engine.get_schema()
        assert schema["type"] == "function"
        func = schema["function"]
        assert func["name"] == "disk"
        assert "command" in func["parameters"]["properties"]
        assert func["parameters"]["required"] == ["command"]

    def test_disk_schema_description_contains_dirs(self, engine):
        schema = engine.get_schema()
        desc = schema["function"]["description"]
        assert "kg" in desc
        assert "memory" in desc


# ---------------------------------------------------------------------------
# Shell syntax rejection
# ---------------------------------------------------------------------------

class TestShellSyntaxRejection:
    def test_pipe_rejected(self, engine):
        result = engine.execute("/kg/explore_node Einstein | grep friend")
        assert result.action == "ERROR"
        assert "pipe" in result.text.lower()

    def test_chaining_rejected(self, engine):
        result = engine.execute("/kg/explore_node Einstein && /memory/remember x")
        assert result.action == "ERROR"
        assert "&&" in result.text
