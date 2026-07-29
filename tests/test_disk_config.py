"""Tests for disk_config.py — YAML configuration loading and validation."""

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
    def test_load_servers_basic(self, config):
        """Sanity check: 2 servers loaded with correct names."""
        assert len(config.servers) == 2
        assert "kg-server" in config.servers
        assert "memory-server" in config.servers

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

    def test_no_disk_yaml_required(self, tmp_path):
        """disk.yaml is no longer used — loaders work fine without it."""
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        # Only server YAML, no disk.yaml at all
        (config_dir / "kg-server.yaml").write_text(yaml.dump({
            "server": "kg-server",
            "directory": "kg",
            "description": "test",
            "tools": {},
        }))
        cfg = DiskConfig(str(config_dir))
        assert "kg-server" in cfg.servers
        assert "kg" in cfg.directory_map

    def test_legacy_disk_yaml_skipped(self, tmp_path):
        """If a user still keeps a disk.yaml around, it is skipped (warning), not an error."""
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        (config_dir / "disk.yaml").write_text("version: 1\nexclude_tools: []\n")
        (config_dir / "kg-server.yaml").write_text(yaml.dump({
            "server": "kg-server",
            "directory": "kg",
            "description": "test",
            "tools": {},
        }))
        cfg = DiskConfig(str(config_dir))
        assert "kg-server" in cfg.servers

    def test_invalid_yaml_skipped_not_raised(self, tmp_path):
        """Per-yaml syntax errors are skipped (warning), no longer block startup."""
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        (config_dir / "broken.yaml").write_text("server: [invalid")
        (config_dir / "kg-server.yaml").write_text(yaml.dump({
            "server": "kg-server",
            "directory": "kg",
            "description": "test",
            "tools": {},
        }))
        # Should not raise — broken.yaml skipped, kg-server.yaml loaded.
        cfg = DiskConfig(str(config_dir))
        assert "kg-server" in cfg.servers


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

class TestValidation:
    def _write_config(self, config_dir, server_yaml_content, disk_yaml=None):
        """Helper: write a single server config. disk_yaml arg is ignored (legacy)."""
        (config_dir / "kg-server.yaml").write_text(yaml.dump(server_yaml_content))
        # disk.yaml is no longer used — ignored for backward compatibility with
        # any callers that still pass disk_yaml=...

    def test_duplicate_directory_names(self, tmp_path):
        config_dir = tmp_path / "disk"
        config_dir.mkdir()
        (config_dir / "kg-server.yaml").write_text(yaml.dump({
            "server": "kg-server", "directory": "data", "description": "test", "tools": {},
        }))
        (config_dir / "memory-server.yaml").write_text(yaml.dump({
            "server": "memory-server", "directory": "data", "description": "test", "tools": {},
        }))
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


# ---------------------------------------------------------------------------
# Multi-directory scan tests (bundle + user overlay)
# ---------------------------------------------------------------------------

class TestMultiDirectoryScan:
    """Tests for ~/.niu/disk/ user overlay support."""

    def _write_bundle_server(self, bundle_dir: Path) -> None:
        """Write a 'server-a' to the bundle dir."""
        (bundle_dir / "server-a.yaml").write_text(yaml.dump({
            "server": "server-a",
            "directory": "adir",
            "description": "bundle description",
            "tools": {
                "tool1": {
                    "summary": "bundle tool1",
                    "description": "bundle tool1 long",
                    "args": [],
                },
            },
        }))

    def test_user_dir_overrides_bundle(self, tmp_path):
        """User dir yaml with same server_name replaces bundle version."""
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        self._write_bundle_server(bundle)

        user = tmp_path / "user"
        user.mkdir()
        (user / "server-a.yaml").write_text(yaml.dump({
            "server": "server-a",  # same server_name
            "directory": "adir",
            "description": "user override description",
            "tools": {
                "tool1": {
                    "summary": "user tool1",
                    "description": "user tool1 long",
                    "args": [],
                },
                "tool2": {
                    "summary": "user tool2 added",
                    "description": "user tool2 long",
                    "args": [],
                },
            },
        }))

        cfg = DiskConfig([str(bundle), str(user)])
        assert "server-a" in cfg.servers
        # User description should win
        assert cfg.servers["server-a"].description == "user override description"
        # User tools should fully replace bundle tools (not merge)
        server = cfg.servers["server-a"]
        assert "tool1" in server.tools
        assert "tool2" in server.tools
        assert server.tools["tool1"].summary == "user tool1"
        # directory_map rebuilt correctly
        assert cfg.directory_map["adir"] == "server-a"

    def test_user_dir_not_exist_bundle_works(self, tmp_path):
        """Missing user dir is skipped silently; bundle still loads."""
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        self._write_bundle_server(bundle)

        # User dir does NOT exist — should not raise, bundle loads alone.
        user = tmp_path / "nonexistent-user"
        cfg = DiskConfig([str(bundle), str(user)])
        assert "server-a" in cfg.servers
        assert cfg.servers["server-a"].description == "bundle description"

    def test_str_compat(self, tmp_path):
        """Passing a str (instead of list) still works — backward compat."""
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        self._write_bundle_server(bundle)

        # Old call style: str only
        cfg = DiskConfig(str(bundle))
        assert "server-a" in cfg.servers
        assert "adir" in cfg.directory_map

    def test_all_dirs_missing_raises(self, tmp_path):
        """If all dirs are missing, raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            DiskConfig([str(tmp_path / "no1"), str(tmp_path / "no2")])

    def test_broken_yaml_in_user_dir_skipped(self, tmp_path):
        """Broken yaml in user dir is warning+skip, doesn't block bundle."""
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        self._write_bundle_server(bundle)

        user = tmp_path / "user"
        user.mkdir()
        (user / "broken.yaml").write_text("server: [invalid")
        (user / "server-b.yaml").write_text(yaml.dump({
            "server": "server-b",
            "directory": "bdir",
            "description": "user-added server b",
            "tools": {},
        }))

        cfg = DiskConfig([str(bundle), str(user)])
        # Both bundle server-a and user-added server-b should load
        assert "server-a" in cfg.servers
        assert "server-b" in cfg.servers
        assert cfg.directory_map["bdir"] == "server-b"

    def test_cross_dir_duplicate_directory_raises(self, tmp_path):
        """Duplicate directory name across bundle+user still raises (strict)."""
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "a.yaml").write_text(yaml.dump({
            "server": "server-a",
            "directory": "samedir",
            "description": "bundle a",
            "tools": {},
        }))

        user = tmp_path / "user"
        user.mkdir()
        # User defines a DIFFERENT server_name but SAME directory as bundle.
        # This is a real cross-file conflict, should still raise.
        (user / "b.yaml").write_text(yaml.dump({
            "server": "server-b",
            "directory": "samedir",
            "description": "user b",
            "tools": {},
        }))

        with pytest.raises(ValidationError, match="Duplicate directory"):
            DiskConfig([str(bundle), str(user)])
