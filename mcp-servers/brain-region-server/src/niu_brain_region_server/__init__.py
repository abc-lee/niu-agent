"""
Brain Region MCP Server — Manual brain region control tools.

Provides three MCP tools:
- brain_region_activate: manually light up brain regions
- brain_region_dim: manually dim brain regions
- brain_region_status: show current brain region states

M5 module: MCP tools for brain region activation control.
"""

from __future__ import annotations

import sys
import os

# Ensure project root is in sys.path so `agent` package can be found
# when running as standalone MCP server (python -m niu_brain_region_server)
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import json
import logging
from typing import Any, Dict, List

from loguru import logger

# ============== Tool Handlers (delegate to brain_tools) ==============

def brain_region_activate(regions: list[str], reason: str = "", **kwargs) -> str:
    """Activate brain regions by label names."""
    from agent.brain_tools import handle_brain_region_activate
    return handle_brain_region_activate(regions=regions, reason=reason)


def brain_region_dim(regions: list[str], **kwargs) -> str:
    """Dim brain regions by label names."""
    from agent.brain_tools import handle_brain_region_dim
    return handle_brain_region_dim(regions=regions)


def brain_region_status(include_dark: bool = False, **kwargs) -> str:
    """Show brain region activation states."""
    from agent.brain_tools import handle_brain_region_status
    return handle_brain_region_status(include_dark=include_dark)


# ============== TOOL_SCHEMAS ==============

TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "brain_region_activate": {
        "name": "brain_region_activate",
        "description": (
            "主动点亮一个或多个脑区，使其知识立即注入上下文。"
            "当你判断接下来的工作需要某个领域的知识时使用。"
            "例如：需要编程知识时点亮编程开发脑区，需要回忆项目细节时点亮项目管理脑区。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "regions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要点亮的脑区名称列表，如 ['编程开发', '项目管理']",
                },
                "reason": {
                    "type": "string",
                    "description": "为什么要点亮这些脑区（可选，用于记忆记录）",
                },
            },
            "required": ["regions"],
        },
    },
    "brain_region_dim": {
        "name": "brain_region_dim",
        "description": (
            "主动关闭一个或多个脑区，停止注入其详细知识。"
            "当你确认某领域知识不再需要时使用，可节省上下文空间。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "regions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要关闭的脑区名称列表",
                },
            },
            "required": ["regions"],
        },
    },
    "brain_region_status": {
        "name": "brain_region_status",
        "description": (
            "查看当前所有脑区的激活状态。显示哪些脑区被点亮及其激活程度。"
            "当你需要了解当前知识上下文覆盖范围时使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_dark": {
                    "type": "boolean",
                    "description": "是否包含未激活的脑区。默认false只显示活跃脑区。",
                    "default": False,
                },
            },
        },
    },
}


def get_tool_schemas() -> List[Dict[str, Any]]:
    """Return all tool schemas for MCP Loader registration."""
    return list(TOOL_SCHEMAS.values())


# ============== MCP Server (for standalone stdio mode) ==============

try:
    from mcp.server import Server
    from mcp.types import Tool, TextContent

    server = Server("brain-region-server")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=schema["name"],
                description=schema["description"],
                inputSchema=schema["input_schema"],
            )
            for schema in get_tool_schemas()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            if name == "brain_region_activate":
                result = brain_region_activate(**arguments)
            elif name == "brain_region_dim":
                result = brain_region_dim(**arguments)
            elif name == "brain_region_status":
                result = brain_region_status(**arguments)
            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

            if isinstance(result, dict):
                text = json.dumps(result, ensure_ascii=False)
            else:
                text = str(result)
            return [TextContent(type="text", text=text)]
        except Exception as e:
            logger.exception(f"Error executing tool {name}: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]

except ImportError:
    server = None


def main():
    """Entry point for standalone MCP server (stdio mode)."""
    if server is None:
        print("mcp package not installed, cannot run as standalone server")
        return
    import asyncio
    from mcp.server.stdio import stdio_server

    async def run():
        async with stdio_server(server) as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(run())
