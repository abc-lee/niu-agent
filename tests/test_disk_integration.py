"""Tests for virtual disk integration."""
import yaml
from pathlib import Path


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