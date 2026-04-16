"""
Niu Session Manager MCP Server

Provides session message management tools for context compression.
"""

import asyncio
import json
import os
import urllib.request
import urllib.error
from typing import Any

from loguru import logger
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Initialize MCP server
server = Server("niu-session-manager")

# Main API URL
API_URL = os.environ.get("NIU_API_URL", "http://127.0.0.1:9876")

# ============== Tool Schemas ==============

TOOL_SCHEMAS = {
    "get_messages": {
        "name": "get_messages",
        "description": "Get message list for a session with token counts. Returns messages with idx, tokens, role, and content preview.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID to get messages for",
                },
            },
            "required": ["session_id"],
        },
    },
    "delete_messages": {
        "name": "delete_messages",
        "description": "Delete messages from a session by indices. Returns deleted count and freed tokens.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID",
                },
                "message_indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of message indices to delete (0-based)",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for deletion (optional)",
                },
            },
            "required": ["session_id", "message_indices"],
        },
    },
}


def get_tool_schemas() -> list[dict]:
    """返回所有工具的 schema 列表（用于 MCP Loader 注册）"""
    return list(TOOL_SCHEMAS.values())


def call_api(method: str, endpoint: str, data: dict | None = None) -> dict | None:
    """Call the main API."""
    try:
        url = f"{API_URL}{endpoint}"
        body = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        req.method = method
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as e:
        logger.error(f"API unavailable: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to call API: {e}")
        return None


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="get_messages",
            description="Get message list for a session with token counts. Returns messages with idx, tokens, role, and content preview.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID to get messages for",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="delete_messages",
            description="Delete messages from a session by indices. Returns deleted count and freed tokens.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "message_indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of message indices to delete (0-based)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for deletion (optional)",
                    },
                },
                "required": ["session_id", "message_indices"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls."""

    if name == "get_messages":
        session_id = arguments.get("session_id")
        if not session_id:
            return [TextContent(type="text", text="Error: session_id is required")]

        # Call main API to get messages (full content for context manager)
        result = call_api(
            "GET", f"/api/context/messages?session_id={session_id}&full=true&limit=100"
        )
        if not result:
            return [TextContent(type="text", text="Error: Failed to get messages")]

        # Format messages with token counts
        messages = result.get("messages", [])
        formatted = []
        total_tokens = 0

        for i, msg in enumerate(messages):
            content = msg.get("content", "")
            try:
                from litellm import token_counter
                tokens = token_counter(model="gpt-4o", messages=[{"role": msg.get("role", "user"), "content": content}])
            except Exception:
                tokens = max(1, len(content) // 2) + 4
            total_tokens += tokens

            formatted.append(
                {
                    "idx": i,
                    "tokens": tokens,
                    "role": msg.get("role", "unknown"),
                    "content": content,  # Full content, no truncation
                }
            )

        output = {
            "total_messages": len(messages),
            "total_tokens": total_tokens,
            "messages": formatted,
        }

        return [
            TextContent(
                type="text", text=json.dumps(output, ensure_ascii=False, indent=2)
            )
        ]

    elif name == "delete_messages":
        session_id = arguments.get("session_id")
        message_indices = arguments.get("message_indices", [])
        reason = arguments.get("reason", "Context compression")

        if not session_id:
            return [TextContent(type="text", text="Error: session_id is required")]
        if not message_indices:
            return [TextContent(type="text", text="Error: message_indices is required")]

        # Call main API to delete messages
        result = call_api(
            "POST",
            "/api/context/messages/delete",
            {
                "session_id": session_id,
                "message_indices": message_indices,
                "reason": reason,
            },
        )

        if not result:
            return [TextContent(type="text", text="Error: Failed to delete messages")]

        return [
            TextContent(
                type="text", text=json.dumps(result, ensure_ascii=False, indent=2)
            )
        ]

    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def run_server():
    """Run the MCP server."""
    logger.info("Session manager starting")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def main():
    """Main entry point."""
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
