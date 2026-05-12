"""Tests for disk_config.py — YAML configuration loading and validation."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from niu_api.internal.disk_config import DiskConfig, ValidationError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_config_dir(tmp_path):
    """Create a temporary config directory with valid YAML files."""
    config_dir = tmp_path / "disk"
    config_dir.mkdir()

    # Global config
    (config_dir / "disk.yaml").write_text(yaml.dump({
        "version": 1,
        "exclude_tools": ["nanobot.system/code_run", "nanobot.system/file_read"],
        "show_hidden": False,
        "disk_mode": True,
    }))

    # kg-server config
    (config_dir / "kg-server.yaml").write_text(yaml.dump({
        "server": "kg-server",
        "directory": "kg",
        "description": "知识图谱",
        "tools": {
            "explore_node": {
                "summary": "探索实体邻居",
                "description": "从实体出发探索N层邻居",
                "category": "explore",
                "args": [
                    {"name": "entity_id", "position": 1, "type": "string", "required": True, "description": "实体ID"},
                    {"name": "depth", "type": "integer", "default": 2, "description": "遍历深度", "constraints": {"minimum": 1, "maximum": 5}},
                    {"name": "min_confidence", "flag": "min-confidence", "type": "number", "default": 0.0, "description": "最小置信度"},
                    {"name": "direction", "type": "string", "enum": ["both", "outgoing", "incoming"], "default": "both", "description": "方向"},
                ],
                "examples": ["/kg/explore_node Einstein"],
            },
            "graph_stats": {
                "summary": "图谱统计",
                "description": "获取统计信息。",
                "args": [],
            },
        },
    }))

    # memory-server config
    (config_dir / "memory-server.yaml").write_text(yaml.dump({
        "server": "memory-server",
        "directory": "memory",
        "description": "记忆系统",
        "tools": {
            "remember": {
                "summary": "保存长期记忆",
                "description": "保存记忆。",
                "args": [
                    {"name": "content", "position": 1, "type": "string", "required": True, "description": "记忆内容"},
                    {"name": "memory_type", "flag": "type", "type": "string", "required": True, "enum": ["environment", "preferences", "skills", "experiences", "facts"], "description": "记忆类型"},
                    {"name": "metadata", "type": "object", "cli_format": "json", "description": "元数据"},
                ],
            },
        },
    }))

    return config_dir


@pytest.fixture
def config(tmp_config_dir):
    """Load a valid DiskConfig."""
    return DiskConfig(str(tmp_config_dir))


# ---------------------------------------------------------------------------
# Loading tests
# ---------------------------------------------------------------------------

class TestLoading:
    def test_load_valid_config(self, config):
        assert config.version == 1
        assert config.exclude_tools == ["nanobot.system/code_run", "nanobot.system/file_read"]
        assert config.disk_mode is True

    def test_load_servers(self, config):
        assert len(config.servers) == 2
        assert "kg-server" in config.servers
        assert "memory-server" in config.servers

    def test_server_directory_mapping(self, config):
        assert config.directory_map["kg"] == "kg-server"
        assert config.directory_map["memory"] == "memory-server"

    def test_server_tools_loaded(self, config):
        kg = config.servers["kg-server"]
        assert "explore_node" in kg.tools
        assert "graph_stats" in kg.tools

    def test_tool_args_loaded(self, config):
        args = config.servers["kg-server"].tools["explore_node"].args
        assert len(args) == 4
        assert args[0].name == "entity_id"
        assert args[0].position == 1
        assert args[0].required is True

    def test_flag_name_default(self, config):
        """Flag name defaults to arg name if not specified."""
        args = config.servers["kg-server"].tools["explore_node"].args
        depth = args[1]
        assert depth.name == "depth"
        assert depth.flag == "depth"  # default = name

    def test_flag_name_custom(self, config):
        """Custom flag name (kebab-case)."""
        args = config.servers["kg-server"].tools["explore_node"].args
        min_conf = args[2]
        assert min_conf.flag == "min-confidence"

    def test_missing_config_dir(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            DiskConfig(str(tmp_path / "nonexist"))

    def test_missing_disk_yaml(self, tmp_path):
        """disk.yaml is optional — defaults apply."""
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        # Only server YAML, no disk.yaml
        (config_dir / "kg-server.yaml").write_text(yaml.dump({
            "server": "kg-server",
            "directory": "kg",
            "description": "test",
            "tools": {},
        }))
        cfg = DiskConfig(str(config_dir))
        assert cfg.version == 0  # default
        assert cfg.disk_mode is True  # default

    def test_invalid_yaml_syntax(self, tmp_path):
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        (config_dir / "disk.yaml").write_text("version: [invalid")
        with pytest.raises(ValidationError):
            DiskConfig(str(config_dir))


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

class TestValidation:
    def _write_config(self, config_dir, server_yaml_content, disk_yaml=None):
        """Helper: write a single server config + optional global config."""
        (config_dir / "kg-server.yaml").write_text(yaml.dump(server_yaml_content))
        if disk_yaml:
            (config_dir / "disk.yaml").write_text(yaml.dump(disk_yaml))
        else:
            (config_dir / "disk.yaml").write_text(yaml.dump({"version": 1}))

    def test_duplicate_directory_names(self, tmp_path):
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        (config_dir / "kg-server.yaml").write_text(yaml.dump({
            "server": "kg-server", "directory": "data", "description": "test", "tools": {},
        }))
        (config_dir / "memory-server.yaml").write_text(yaml.dump({
            "server": "memory-server", "directory": "data", "description": "test", "tools": {},
        }))
        (config_dir / "disk.yaml").write_text(yaml.dump({"version": 1}))
        with pytest.raises(ValidationError, match="Duplicate directory"):
            DiskConfig(str(config_dir))

    def test_tool_name_conflicts_with_builtin(self, tmp_path):
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        self._write_config(config_dir, {
            "server": "kg-server", "directory": "kg", "description": "test",
            "tools": {"ls": {"summary": "oops", "description": "bad", "args": []}},
        })
        with pytest.raises(ValidationError, match="reserved"):
            DiskConfig(str(config_dir))

    def test_directory_name_conflicts_with_builtin(self, tmp_path):
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        self._write_config(config_dir, {
            "server": "kg-server", "directory": "ls", "description": "test", "tools": {},
        })
        with pytest.raises(ValidationError, match="reserved"):
            DiskConfig(str(config_dir))

    def test_position_gap(self, tmp_path):
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        self._write_config(config_dir, {
            "server": "kg-server", "directory": "kg", "description": "test",
            "tools": {"tool": {
                "summary": "test", "description": "test",
                "args": [
                    {"name": "a", "position": 1, "type": "string", "required": True, "description": "a"},
                    {"name": "c", "position": 3, "type": "string", "required": True, "description": "c"},
                ],
            }},
        })
        with pytest.raises(ValidationError, match="position"):
            DiskConfig(str(config_dir))

    def test_duplicate_flag_names(self, tmp_path):
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        self._write_config(config_dir, {
            "server": "kg-server", "directory": "kg", "description": "test",
            "tools": {"tool": {
                "summary": "test", "description": "test",
                "args": [
                    {"name": "a", "flag": "same", "type": "string", "description": "a"},
                    {"name": "b", "flag": "same", "type": "string", "description": "b"},
                ],
            }},
        })
        with pytest.raises(ValidationError, match="duplicate flag"):
            DiskConfig(str(config_dir))

    def test_object_without_cli_format(self, tmp_path):
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        self._write_config(config_dir, {
            "server": "kg-server", "directory": "kg", "description": "test",
            "tools": {"tool": {
                "summary": "test", "description": "test",
                "args": [
                    {"name": "filter", "type": "object", "description": "filter"},
                ],
            }},
        })
        with pytest.raises(ValidationError, match="cli_format"):
            DiskConfig(str(config_dir))

    def test_array_without_cli_format(self, tmp_path):
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        self._write_config(config_dir, {
            "server": "kg-server", "directory": "kg", "description": "test",
            "tools": {"tool": {
                "summary": "test", "description": "test",
                "args": [
                    {"name": "tags", "type": "array", "description": "tags"},
                ],
            }},
        })
        with pytest.raises(ValidationError, match="cli_format"):
            DiskConfig(str(config_dir))

    def test_optional_position_before_required(self, tmp_path):
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        self._write_config(config_dir, {
            "server": "kg-server", "directory": "kg", "description": "test",
            "tools": {"tool": {
                "summary": "test", "description": "test",
                "args": [
                    {"name": "a", "position": 1, "type": "string", "required": False, "description": "a"},
                    {"name": "b", "position": 2, "type": "string", "required": True, "description": "b"},
                ],
            }},
        })
        with pytest.raises(ValidationError, match="required.*position"):
            DiskConfig(str(config_dir))

    def test_mutually_exclusive_with_nonexistent_param(self, tmp_path):
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        self._write_config(config_dir, {
            "server": "kg-server", "directory": "kg", "description": "test",
            "tools": {"tool": {
                "summary": "test", "description": "test",
                "args": [{"name": "a", "type": "string", "description": "a"}],
                "mutually_exclusive": [["a", "nonexist"]],
            }},
        })
        with pytest.raises(ValidationError, match="mutually_exclusive"):
            DiskConfig(str(config_dir))


# ---------------------------------------------------------------------------
# Lookup tests
# ---------------------------------------------------------------------------

class TestLookup:
    def test_get_server_by_dir(self, config):
        server = config.get_server_by_dir("kg")
        assert server.server_name == "kg-server"

    def test_get_server_by_dir_not_found(self, config):
        assert config.get_server_by_dir("nonexist") is None

    def test_get_tool_config(self, config):
        tool = config.get_tool_config("kg", "explore_node")
        assert tool is not None
        assert tool.summary == "探索实体邻居"

    def test_get_tool_config_not_found(self, config):
        assert config.get_tool_config("kg", "nonexist") is None

    def test_list_visible_tools(self, config):
        tools = config.list_visible_tools("kg")
        names = [t.name for t in tools]
        assert "explore_node" in names
        assert "graph_stats" in names

    def test_list_visible_tools_excludes_hidden(self, tmp_config_dir):
        # Add a hidden tool to kg-server
        kg_yaml = tmp_config_dir / "kg-server.yaml"
        data = yaml.safe_load(kg_yaml.read_text())
        data["tools"]["hidden_tool"] = {
            "summary": "hidden", "description": "hidden", "hidden": True, "args": [],
        }
        kg_yaml.write_text(yaml.dump(data))
        config = DiskConfig(str(tmp_config_dir))
        tools = config.list_visible_tools("kg")
        names = [t.name for t in tools]
        assert "hidden_tool" not in names

    def test_list_all_tools_includes_hidden(self, config):
        # graph_stats is not hidden
        tools = config.list_all_tools("kg")
        assert len(tools) >= 2

    def test_list_directories(self, config):
        dirs = config.list_directories()
        assert "kg" in dirs
        assert "memory" in dirs
