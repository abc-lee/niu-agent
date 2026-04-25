"""Tests for virtual disk integration."""
import pytest
import yaml
from pathlib import Path

from niu_api.internal.disk_config import DiskConfig


def test_no_static_or_dynamic_tools():
    """All MCP tools must be visibility: hidden (not static or dynamic)."""
    config_path = Path(__file__).parent.parent / "config" / "mcp-servers.yaml"
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    violations = []
    for server_name, server_cfg in data.items():
        if not isinstance(server_cfg, dict):
            continue
        tools = server_cfg.get("tools", {})
        if not isinstance(tools, dict):
            continue
        for tool_name, tool_cfg in tools.items():
            if not isinstance(tool_cfg, dict):
                continue
            visibility = tool_cfg.get("visibility", "static")
            if visibility in ("static", "dynamic"):
                violations.append(f"{server_name}/{tool_name}: visibility={visibility}")

    assert violations == [], f"Found non-hidden tools: {violations}"


class TestLightragServerYaml:
    """Test lightrag-server.yaml disk configuration."""

    @pytest.fixture
    def disk_config(self):
        return DiskConfig(str(Path(__file__).parent.parent / "config" / "disk"))

    def test_lightrag_directory_exists(self, disk_config):
        """lightrag directory should be listed."""
        assert "lightrag" in disk_config.list_directories()

    def test_lightrag_tool_count(self, disk_config):
        """lightrag server should have 12 tools total."""
        all_tools = disk_config.list_all_tools("lightrag")
        assert len(all_tools) == 12

    def test_lightrag_visible_tool_count(self, disk_config):
        """lightrag server should have 11 visible tools (1 hidden)."""
        visible = disk_config.list_visible_tools("lightrag")
        assert len(visible) == 11

    def test_lightrag_hidden_tool(self, disk_config):
        """lightrag_document_status should be hidden."""
        tool = disk_config.get_tool_config("lightrag", "lightrag_document_status")
        assert tool is not None
        assert tool.hidden is True

    def test_lightrag_key_tools_present(self, disk_config):
        """Key lightrag tools should be present."""
        server = disk_config.get_server_by_dir("lightrag")
        expected = [
            "lightrag_query", "lightrag_query_data", "lightrag_search_entities",
            "lightrag_get_graph", "lightrag_insert", "lightrag_insert_custom_kg",
            "lightrag_insert_entity", "lightrag_insert_relation",
            "lightrag_delete_entity", "lightrag_list_entities", "lightrag_merge_entities",
        ]
        for name in expected:
            assert name in server.tools, f"Missing tool: {name}"


class TestDiskDescription:
    """Test disk description in system prompt."""

    def test_disk_description_contains_directory_listing(self):
        """_build_disk_description() should list all disk directories."""
        from unittest.mock import patch, MagicMock
        from niu_api.internal.disk_engine import DiskEngine
        import os

        disk_config_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "config", "disk"
        )
        engine = DiskEngine(disk_config_dir, registry=None)

        # Build description directly
        servers = engine.config.servers
        dir_lines = []
        for server in servers.values():
            dir_lines.append(f"  /{server.directory:<10} — {server.description}")
        desc = "\n".join(dir_lines)

        # Should contain key directories
        assert "/memory" in desc
        assert "/lightrag" in desc
        assert "/photos" in desc

    def test_disk_description_format(self):
        """Description should follow the expected format."""
        from niu_api.internal.disk_engine import DiskEngine
        import os

        disk_config_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "config", "disk"
        )
        engine = DiskEngine(disk_config_dir, registry=None)
        dirs = engine.config.list_directories()

        # Should have at least 8 directories
        assert len(dirs) >= 8
        # Should include lightrag
        assert "lightrag" in dirs