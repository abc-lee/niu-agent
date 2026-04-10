"""
Page Agent MCP Server - Python Bridge

This module provides a Python wrapper for page-agent-mcp.
page-agent-mcp runs as a standalone HTTP+WebSocket server on port 38401.

Architecture:
- page-agent-mcp must be started separately (not via subprocess)
- The hub-bridge.js creates an HTTP server on port 38401
- Chrome extension connects to the hub via WebSocket
- MCP stdio clients should connect to the existing hub, not create new ones

Usage:
    Start page-agent-mcp separately:
        node mcp-servers/page-agent-mcp/src/index.js

    Then the MCP tools will work when called via the ToolRegistry.
"""

import os
import sys
import json
import urllib.request
import urllib.error
from typing import Any, Dict, List

# Ensure parent directories are in path for imports
_current_dir = os.path.dirname(os.path.abspath(__file__))
_mcp_servers_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
project_root = os.path.dirname(os.path.dirname(_mcp_servers_dir))

# Add project root to path
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from agent.mcp_sync_bridge import get_mcp_bridge


# ============================================================================
# Configuration
# ============================================================================

HUB_BRIDGE_PORT = 38401
HUB_BRIDGE_URL = f"http://localhost:{HUB_BRIDGE_PORT}"


# ============================================================================
# Tool Schemas (must match page-agent-mcp/src/index.js)
# ============================================================================

TOOL_SCHEMAS = {
    "execute_task": {
        "description": "Execute a task in user's browser. The task description should be specific and include the expected result.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Task description in natural language. Give specific instructions for the task. Steps are preferable. Include the information you want to get after the task is done."
                }
            },
            "required": ["task"]
        }
    },
    "get_status": {
        "description": "Check the current status of the Page Agent hub. Returns { connected, busy }.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    "stop_task": {
        "description": "Stop the currently running browser automation task.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    }
}


def get_tool_schemas() -> List[Dict[str, Any]]:
    """Return tool schemas for page-agent-mcp"""
    schemas = []
    for tool_name, tool_def in TOOL_SCHEMAS.items():
        schemas.append({
            "name": tool_name,
            "description": tool_def["description"],
            "input_schema": tool_def["input_schema"]
        })
    return schemas


# ============================================================================
# HTTP-based Tool Functions (for direct hub-bridge access)
# ============================================================================

def _http_get(path: str) -> Dict[str, Any]:
    """Make HTTP GET request to hub-bridge"""
    try:
        url = f"{HUB_BRIDGE_URL}{path}"
        with urllib.request.urlopen(url, timeout=5) as response:
            return {"status": "success", "data": response.read().decode("utf-8")}
    except urllib.error.URLError as e:
        return {"status": "error", "msg": f"Connection failed: {e}"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def execute_task(task: str) -> str:
    """
    Execute a task in user's browser via MCP bridge.

    This calls page-agent-mcp via the MCP stdio protocol.
    page-agent-mcp must be running separately.
    """
    bridge = get_mcp_bridge()
    result = bridge.call_tool("page-agent-mcp", "execute_task", {"task": task}, timeout=120)

    # Handle the result format from page-agent-mcp
    if isinstance(result, dict):
        content = result.get("content", [])
        if isinstance(content, list) and len(content) > 0:
            text = content[0].get("text", "")
            return text
    return json.dumps(result, ensure_ascii=False)


def get_status() -> str:
    """
    Get the status of Page Agent hub via MCP bridge.

    Returns whether the Chrome extension is connected.
    """
    bridge = get_mcp_bridge()
    result = bridge.call_tool("page-agent-mcp", "get_status", {}, timeout=10)

    if isinstance(result, dict):
        content = result.get("content", [])
        if isinstance(content, list) and len(content) > 0:
            return content[0].get("text", "{}")
    return json.dumps(result, ensure_ascii=False)


def stop_task() -> str:
    """
    Stop the currently running task via MCP bridge.
    """
    bridge = get_mcp_bridge()
    result = bridge.call_tool("page-agent-mcp", "stop_task", {}, timeout=10)

    if isinstance(result, dict):
        content = result.get("content", [])
        if isinstance(content, list) and len(content) > 0:
            return content[0].get("text", "")
    return json.dumps(result, ensure_ascii=False)
