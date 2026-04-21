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
    "add_message": {
        "name": "add_message",
        "description": "Add a message to the session. Used by context-manager to insert consolidated L0 summaries after merging multiple messages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID",
                },
                "role": {
                    "type": "string",
                    "description": "Message role: 'user', 'assistant', or 'system'",
                },
                "content": {
                    "type": "string",
                    "description": "Message content",
                },
            },
            "required": ["session_id", "role", "content"],
        },
    },
    "update_message": {
        "name": "update_message",
        "description": "Update content of an existing message by index. Used by context-manager to rewrite L0 summaries in-place during compression.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID",
                },
                "message_index": {
                    "type": "integer",
                    "description": "Index of the message to update (0-based, from get_messages)",
                },
                "content": {
                    "type": "string",
                    "description": "New content for the message",
                },
            },
            "required": ["session_id", "message_index", "content"],
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


def _add_message_direct(session_id: str, role: str, content: str) -> dict:
    """Add message directly via MessageStore (in-process, no HTTP)."""
    import asyncio as _asyncio
    from agent.session_adapter import get_message_store

    async def _do():
        store = await get_message_store(session_id)
        msg_id = await store.add_message(role=role, content=content)
        return {"status": "ok", "message_id": msg_id}

    try:
        loop = _asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(_asyncio.run, _do())
                return future.result(timeout=10)
        else:
            return loop.run_until_complete(_do())
    except Exception as e:
        logger.error(f"add_message failed: {e}")
        return {"status": "error", "message": str(e)}


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
            name="add_message",
            description="Add a message to the session. Used by context-manager to insert consolidated L0 summaries after merging multiple messages.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "role": {
                        "type": "string",
                        "description": "Message role: 'user', 'assistant', or 'system'",
                    },
                    "content": {
                        "type": "string",
                        "description": "Message content",
                    },
                },
                "required": ["session_id", "role", "content"],
            },
        ),
        Tool(
            name="update_message",
            description="Update content of an existing message by index. Used by context-manager to rewrite L0 summaries in-place during compression.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "message_index": {
                        "type": "integer",
                        "description": "Index of the message to update (0-based, from get_messages)",
                    },
                    "content": {
                        "type": "string",
                        "description": "New content for the message",
                    },
                },
                "required": ["session_id", "message_index", "content"],
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

    elif name == "add_message":
        session_id = arguments.get("session_id")
        role = arguments.get("role")
        content = arguments.get("content")

        if not session_id:
            return [TextContent(type="text", text="Error: session_id is required")]
        if not role:
            return [TextContent(type="text", text="Error: role is required")]
        if not content:
            return [TextContent(type="text", text="Error: content is required")]

        result = _add_message_direct(session_id, role, content)
        return [
            TextContent(
                type="text", text=json.dumps(result, ensure_ascii=False, indent=2)
            )
        ]

    elif name == "update_message":
        session_id = arguments.get("session_id")
        message_index = arguments.get("message_index")
        content = arguments.get("content")

        if not session_id:
            return [TextContent(type="text", text="Error: session_id is required")]
        if message_index is None:
            return [TextContent(type="text", text="Error: message_index is required")]
        if not content:
            return [TextContent(type="text", text="Error: content is required")]

        # Call main API to update message
        result = call_api(
            "POST",
            "/api/context/messages/update",
            {
                "session_id": session_id,
                "message_index": message_index,
                "content": content,
            },
        )

        if not result:
            return [TextContent(type="text", text="Error: Failed to update message")]

        return [
            TextContent(
                type="text", text=json.dumps(result, ensure_ascii=False, indent=2)
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
