"""Tests for disk_navigator.py — Navigation system (ls/cat/help)."""

import pytest
import yaml

from niu_api.internal.disk_config import DiskConfig
from niu_api.internal.disk_navigator import DiskNavigator
from niu_api.internal.disk_parser import ParsedCommand


@pytest.fixture
def config(tmp_path):
    """Create a valid config with 2 servers."""
    config_dir = tmp_path / "disk"
    config_dir.mkdir()

    (config_dir / "kg-server.yaml").write_text(yaml.dump({
        "server": "kg-server", "directory": "kg", "description": "知识图谱",
        "tools": {
            "explore_node": {
                "summary": "探索实体邻居", "description": "从实体出发探索邻居。",
                "category": "explore",
                "args": [
                    {"name": "entity_id", "position": 1, "type": "string", "required": True, "description": "实体ID"},
                    {"name": "depth", "type": "integer", "default": 2, "description": "遍历深度"},
                ],
                "examples": ["/kg/explore_node Einstein"],
            },
            "query_graph": {
                "summary": "执行Cypher查询", "description": "执行Cypher只读查询。",
                "category": "query",
                "args": [{"name": "cypher", "position": 1, "type": "string", "required": True, "description": "查询语句"}],
            },
            "delete_entity": {
                "summary": "删除实体", "description": "删除实体节点。", "hidden": True,
                "args": [{"name": "name", "position": 1, "type": "string", "required": True, "description": "实体名"}],
            },
        },
    }))

    (config_dir / "memory-server.yaml").write_text(yaml.dump({
        "server": "memory-server", "directory": "memory", "description": "记忆系统",
        "tools": {
            "remember": {
                "summary": "保存长期记忆", "description": "保存记忆。",
                "args": [
                    {"name": "content", "position": 1, "type": "string", "required": True, "description": "记忆内容"},
                    {"name": "memory_type", "flag": "type", "type": "string", "required": True,
                     "enum": ["environment", "preferences", "skills"], "description": "类型"},
                ],
            },
        },
    }))

    return DiskConfig(str(config_dir))


@pytest.fixture
def navigator(config):
    return DiskNavigator(config)


# ---------------------------------------------------------------------------
# ls /
# ---------------------------------------------------------------------------

class TestLsRoot:
    def test_ls_root_shows_all_dirs(self, navigator, config):
        result = navigator.list_dir("/")
        assert "kg/" in result
        assert "memory/" in result
        assert "知识图谱" in result

    def test_ls_root_includes_usage_hint(self, navigator):
        result = navigator.list_dir("/")
        assert "cat" in result
        assert "execute" in result.lower()


# ---------------------------------------------------------------------------
# ls /dir
# ---------------------------------------------------------------------------

class TestLsDir:
    def test_ls_dir_with_categories(self, navigator):
        result = navigator.list_dir("/kg")
        assert "explore_node" in result
        assert "query_graph" in result
        # Should show categories
        assert "explore" in result

    def test_ls_dir_no_categories_for_few_tools(self, navigator):
        result = navigator.list_dir("/memory")
        assert "remember" in result

    def test_ls_dir_not_found(self, navigator):
        result = navigator.list_dir("/nonexist")
        assert "No such file" in result or "not found" in result.lower()

    def test_ls_dir_hidden_tools_excluded(self, navigator):
        result = navigator.list_dir("/kg")
        assert "delete_entity" not in result

    def test_ls_all_shows_hidden(self, navigator):
        result = navigator.list_dir("/kg", show_all=True)
        assert "delete_entity" in result
        assert "hidden" in result.lower()


# ---------------------------------------------------------------------------
# cat /dir/tool
# ---------------------------------------------------------------------------

class TestCatTool:
    def test_cat_shows_full_readme(self, navigator):
        result = navigator.read_tool("/kg/explore_node")
        assert "/kg/explore_node" in result
        assert "entity_id" in result
        assert "--depth" in result
        assert "USAGE" in result

    def test_cat_includes_examples(self, navigator):
        result = navigator.read_tool("/kg/explore_node")
        assert "Einstein" in result

    def test_cat_tool_not_found(self, navigator):
        result = navigator.read_tool("/kg/nonexist")
        assert "No such file" in result or "not found" in result.lower()

    def test_cat_directory(self, navigator):
        result = navigator.read_tool("/kg")
        assert "directory" in result.lower()

    def test_cat_dir_not_found(self, navigator):
        result = navigator.read_tool("/nonexist")
        assert "No such file" in result or "not found" in result.lower()


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------

class TestHelp:
    def test_help_shows_commands(self, navigator):
        result = navigator.help()
        assert "ls" in result
        assert "cat" in result
        assert "help" in result


# ---------------------------------------------------------------------------
# Navigation errors
# ---------------------------------------------------------------------------

class TestNavigationErrors:
    def test_e1_unknown_command(self, navigator):
        parsed = ParsedCommand(action="UNKNOWN", raw="rm /some/path")
        result = navigator.handle(parsed)
        assert "rm" in result
        assert "ls" in result

    def test_e11_empty_command(self, navigator):
        parsed = ParsedCommand(action="EMPTY")
        result = navigator.handle(parsed)
        assert "ls" in result
        assert "cat" in result

    def test_e13_cd_not_supported(self, navigator):
        parsed = ParsedCommand(action="UNKNOWN", raw="cd /kg")
        result = navigator.handle(parsed)
        assert "cd" in result

    def test_e15_execute_directory(self, navigator):
        parsed = ParsedCommand(action="EXECUTE", dir_name="kg", tool_name=None, path="/kg")
        result = navigator.handle(parsed)
        assert "directory" in result.lower()
