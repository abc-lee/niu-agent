"""
Page Agent MCP Server - Python Bridge

This module provides a Python wrapper for page-agent-mcp (Node.js).
It enables page-agent-mcp to be loaded via the standard MCP loader mechanism.

Usage:
    from niu_page_agent import get_tool_schemas, execute_task, get_status, stop_task
"""

import os
import sys
import json
import asyncio
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


def _call_page_agent_tool(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Call a page-agent-mcp tool via the MCP bridge"""
    bridge = get_mcp_bridge()
    result = bridge.call_tool("page-agent-mcp", tool_name, args, timeout=120)

    # Handle the result format from page-agent-mcp
    if isinstance(result, dict):
        content = result.get("content", [])
        if isinstance(content, list) and len(content) > 0:
            text = content[0].get("text", "")
            return {"text": text}
    return result


# ============================================================================
# Tool Functions (called by the handler via ToolRegistry)
# ============================================================================

def execute_task(task: str) -> str:
    """
    Execute a task in user's browser.

    Args:
        task: Task description in natural language

    Returns:
        Task execution result
    """
    result = _call_page_agent_tool("execute_task", {"task": task})
    if isinstance(result, dict) and "text" in result:
        return result["text"]
    return json.dumps(result, ensure_ascii=False)


def get_status() -> str:
    """
    Get the status of Page Agent hub.

    Returns:
        JSON string with { connected, busy }
    """
    result = _call_page_agent_tool("get_status", {})
    if isinstance(result, dict) and "text" in result:
        return result["text"]
    return json.dumps(result, ensure_ascii=False)


def stop_task() -> str:
    """
    Stop the currently running task.

    Returns:
        Confirmation message
    """
    result = _call_page_agent_tool("stop_task", {})
    if isinstance(result, dict) and "text" in result:
        return result["text"]
    return json.dumps(result, ensure_ascii=False)
