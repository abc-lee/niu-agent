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
            description="Get message list for a session with KB sizes. Returns messages with idx, kb, role, and content preview.",
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
            description="Delete messages from a session by indices. Returns deleted count and freed KB.",
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

        # Format messages with KB sizes
        messages = result.get("messages", [])
        formatted = []
        total_kb = 0

        for i, msg in enumerate(messages):
            content = msg.get("content", "")
            kb = max(1, len(content) // 1024)  # At least 1KB
            total_kb += kb

            formatted.append(
                {
                    "idx": i,
                    "kb": kb,
                    "role": msg.get("role", "unknown"),
                    "content": content,  # Full content, no truncation
                }
            )

        output = {
            "total_messages": len(messages),
            "total_kb": total_kb,
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
