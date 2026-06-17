"""
Niu Session Manager MCP Server

Provides session message management tools for context compression.

Architecture:
- Module-level functions (get_messages, add_message, update_message, delete_messages)
  use MessageStore directly (同进程调用, no HTTP API dependency).
- MCP stdio handlers preserved for backward compatibility.
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

# Main API URL (fallback for stdio mode)
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
        "description": "Add a message to the session. Used by context-manager to insert consolidated summaries after merging multiple messages.",
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
        "description": "Update content of an existing message by ID. Used by context-manager to rewrite summaries in-place during compression.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID",
                },
                "message_id": {
                    "type": "string",
                    "description": "ID of the message to update (from get_messages response)",
                },
                "content": {
                    "type": "string",
                    "description": "New content for the message",
                },
            },
            "required": ["session_id", "message_id", "content"],
        },
    },
    "delete_messages": {
        "name": "delete_messages",
        "description": "Delete messages from a session by IDs. Returns deleted count and freed tokens.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID",
                },
                "message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of message IDs to delete (from get_messages response)",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for deletion (optional)",
                },
            },
            "required": ["session_id", "message_ids"],
        },
    },
}


def get_tool_schemas() -> list[dict]:
    """返回所有工具的 schema 列表（用于 MCP Loader 注册）"""
    return list(TOOL_SCHEMAS.values())


def _get_store():
    """Get or create MessageStore instance (sync wrapper)."""
    from agent.session import get_message_store
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already in async context — create a new event loop in a thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, get_message_store()).result()
    else:
        return asyncio.run(get_message_store())


def _run_async(coro):
    """Run an async coroutine synchronously."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


# ============================================================================
# Module-level functions for ToolRegistry direct function lookup
# (同进程调用, no HTTP API dependency — snowball compression depends on these)
# ============================================================================

def get_messages(session_id: str, **kwargs) -> dict:
    """Get message list with token counts via MessageStore (direct call)."""
    try:
        store = _get_store()
        messages = _run_async(store.get_messages())

        formatted = []
        total_tokens = 0
        for i, msg in enumerate(messages, 1):
            content = getattr(msg, "content", "") or ""
            try:
                from agent.token_calculator import TokenCalculator
                tokens = TokenCalculator.get().count_message_single(getattr(msg, "role", "user"), content)
            except Exception:
                tokens = max(1, len(content) // 2) + 4
            total_tokens += tokens

            formatted.append({
                "id": getattr(msg, "id", ""),
                "idx": i,
                "tokens": tokens,
                "role": getattr(msg, "role", "unknown"),
                "content": content,
            })

        return {
            "total_messages": len(messages),
            "total_tokens": total_tokens,
            "messages": formatted,
        }
    except Exception as e:
        logger.error(f"get_messages direct call failed: {e}")
        return {"error": str(e), "total_messages": 0, "total_tokens": 0, "messages": []}


def add_message(session_id: str, role: str, content: str, **kwargs) -> dict:
    """Add a message via MessageStore (direct call)."""
    try:
        store = _get_store()
        msg_id = _run_async(store.add_message(role=role, content=content))
        return {"status": "ok", "message_id": msg_id}
    except Exception as e:
        logger.error(f"add_message direct call failed: {e}")
        return {"status": "error", "error": str(e)}


def update_message(session_id: str, message_id: str, content: str, **kwargs) -> dict:
    """Update message content via MessageStore (direct call)."""
    try:
        store = _get_store()
        updated = _run_async(store.update_message(message_id=message_id, content=content))
        if updated:
            return {"status": "ok", "message_id": message_id}
        else:
            return {"status": "error", "error": f"Message {message_id} not found"}
    except Exception as e:
        logger.error(f"update_message direct call failed: {e}")
        return {"status": "error", "error": str(e)}


def delete_messages(session_id: str, message_ids: list, reason: str = "Context compression", **kwargs) -> dict:
    """Delete messages by IDs via MessageStore (direct call)."""
    try:
        store = _get_store()
        result = _run_async(store.delete_messages_by_ids(message_ids))
        return {
            "status": "ok",
            "deleted_count": result.get("deleted_count", 0),
            "freed_tokens": result.get("freed_tokens", 0),
        }
    except Exception as e:
        logger.error(f"delete_messages direct call failed: {e}")
        return {"status": "error", "error": str(e), "deleted_count": 0, "freed_tokens": 0}


# ============================================================================
# HTTP API fallback (for stdio MCP mode when API server is running)
# ============================================================================

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
            description="Update content of an existing message by ID. Used by context-manager to rewrite L0 summaries in-place during compression.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "ID of the message to update (from get_messages response)",
                    },
                    "content": {
                        "type": "string",
                        "description": "New content for the message",
                    },
                },
                "required": ["session_id", "message_id", "content"],
            },
        ),
        Tool(
            name="delete_messages",
            description="Delete messages from a session by IDs. Returns deleted count and freed tokens.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of message IDs to delete (from get_messages response)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for deletion (optional)",
                    },
                },
                "required": ["session_id", "message_ids"],
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

        for i, msg in enumerate(messages, 1):
            content = msg.get("content", "")
            try:
                from agent.token_calculator import TokenCalculator
                tokens = TokenCalculator.get().count_message_single(msg.get("role", "user"), content)
            except Exception:
                tokens = max(1, len(content) // 2) + 4
            total_tokens += tokens

            formatted.append(
                {
                    "id": msg.get("id", ""),
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

        result = call_api(
            "POST",
            "/api/context/messages/add",
            {
                "session_id": session_id,
                "role": role,
                "content": content,
            },
        )

        if not result:
            return [TextContent(type="text", text="Error: Failed to add message")]

        return [
            TextContent(
                type="text", text=json.dumps(result, ensure_ascii=False, indent=2)
            )
        ]

    elif name == "update_message":
        session_id = arguments.get("session_id")
        message_id = arguments.get("message_id")
        content = arguments.get("content")

        if not session_id:
            return [TextContent(type="text", text="Error: session_id is required")]
        if not message_id:
            return [TextContent(type="text", text="Error: message_id is required")]
        if not content:
            return [TextContent(type="text", text="Error: content is required")]

        # Call main API to update message by ID
        result = call_api(
            "POST",
            "/api/context/messages/update",
            {
                "session_id": session_id,
                "message_id": message_id,
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
        message_ids = arguments.get("message_ids", [])
        reason = arguments.get("reason", "Context compression")

        if not session_id:
            return [TextContent(type="text", text="Error: session_id is required")]
        if not message_ids:
            return [TextContent(type="text", text="Error: message_ids is required")]

        # Call main API to delete messages by IDs
        result = call_api(
            "POST",
            "/api/context/messages/delete",
            {
                "session_id": session_id,
                "message_ids": message_ids,
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
