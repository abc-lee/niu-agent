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
        "description": "Add a message to the session.",
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
        "description": "Update content of an existing message by ID.",
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
    "read_history_block": {
        "name": "read_history_block",
        "description": (
            "按块号取回已归档早期对话的逐字原文。历史索引中 [块#N] 的 N 即 block_id。"
            "返回该块时间范围内的全部原始消息（时间+角色+内容，tool 输出含 tool_call_id 归属）；"
            "超大块自动精简（头尾保留+已精简标注，单条超长动态截断）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "block_id": {
                    "type": "integer",
                    "description": "归档块号（来自历史索引行的 块#N）",
                },
            },
            "required": ["block_id"],
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
                # Ensure agent package is importable in stdio mode (project root may not be in sys.path)
                import sys as _sys
                from pathlib import Path as _Path
                _project_root = str(_Path(__file__).resolve().parent.parent.parent.parent.parent)
                if _project_root not in _sys.path:
                    _sys.path.insert(0, _project_root)
                from agent.token_calculator import TokenCalculator
                tokens = TokenCalculator.get().count_message_single(getattr(msg, "role", "user"), content, tool_calls=getattr(msg, "tool_calls", None))
            except Exception:
                # Fallback: CJK ~1.5 tokens, ASCII ~0.25 tokens (conservative, same as memory-server)
                cjk = sum(1 for c in content if '一' <= c <= '鿿')
                tokens = max(1, int(cjk * 1.5 + (len(content) - cjk) * 0.25))
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


def read_history_block(block_id: int, **kwargs) -> dict:
    """按块号取回归档历史块的逐字原文（直接读 context_blocks.db + messages.db）。

    截断策略对齐 niu read_file 行为规格：
    - 单条消息动态截断：每条上限 min(10000, max(100, 500000//条数)) 字符，
      截断追加 ' ... [TRUNCATED]'
    - 总量上限：>500 条时保留头尾各 250 条并标注省略
    """
    try:
        blocks_db, messages_db = _resolve_db_paths(kwargs)
        blocks = _load_blocks(blocks_db)
        if not blocks:
            return {"status": "error", "error": "暂无归档历史：尚无已归档的对话块"}

        try:
            bid = int(block_id)
        except (TypeError, ValueError):
            return {"status": "error", "error": f"block_id 必须是整数，收到: {block_id!r}"}

        block = next((b for b in blocks if b.id == bid), None)
        if block is None:
            lo, hi = min(b.id for b in blocks), max(b.id for b in blocks)
            return {
                "status": "error",
                "error": (
                    f"块 #{bid} 不存在。当前有效块号范围：{lo}~{hi}"
                    f"（共 {len(blocks)} 块），请从历史索引行复制块号"
                ),
            }

        rows = _query_messages_by_rowid(messages_db, block.start_rowid, block.end_rowid)
        text, stats = _format_block_text(block, rows)
        return {
            "status": "ok",
            "block": {
                "id": block.id,
                "time_start": block.time_start,
                "time_end": block.time_end,
                "count": block.count,
                "entities": list(block.entities),
                "first_user": block.first_user,
            },
            "total_messages": len(rows),
            **stats,
            "text": text,
        }
    except Exception as e:
        logger.error(f"read_history_block failed: {e}")
        return {"status": "error", "error": str(e)}


def _resolve_db_paths(kwargs: dict) -> tuple[str, str]:
    """解析块存储与消息库路径（测试可注入，默认 ~/.niu/）。"""
    home = os.path.expanduser("~")
    blocks_db = kwargs.get("blocks_db_path") or os.path.join(home, ".niu", "context_blocks.db")
    messages_db = kwargs.get("messages_db_path") or os.path.join(home, ".niu", "messages.db")
    return str(blocks_db), str(messages_db)


def _load_blocks(blocks_db: str) -> list:
    """读全部指针块（复用 agent.context_assembler.blocks 单一实现）。"""
    import sys as _sys
    from pathlib import Path as _Path

    _project_root = str(_Path(__file__).resolve().parent.parent.parent.parent.parent)
    if _project_root not in _sys.path:
        _sys.path.insert(0, _project_root)
    from pathlib import Path as _Path2

    from agent.context_assembler.blocks import load_all

    return load_all(_Path2(blocks_db))


def _query_messages_by_rowid(messages_db: str, start_rowid: int, end_rowid: int) -> list[dict]:
    """按 rowid 闭区间拉原始消息（rowid 即写入顺序）。"""
    import sqlite3

    conn = sqlite3.connect(messages_db)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            """SELECT rowid, role, content, tool_call_id, created_at FROM messages
               WHERE rowid >= ? AND rowid <= ? ORDER BY rowid ASC""",
            (start_rowid, end_rowid),
        )
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


# 截断常量（对齐 agent/handler.py read_file 行为规格）
_BLOCK_MAX_MESSAGES = 500          # 硬上限，超出头尾保留（read_file 的 500 行上限）
_BLOCK_CHAR_BUDGET = 500_000       # 总字符预算，按条数均分出单条上限


def _format_block_text(block, rows: list[dict]) -> tuple[str, dict]:
    """格式化块原文；返回 (text, 统计 dict)。"""
    omitted = 0
    rendered = rows
    head_tail_truncated = False
    if len(rows) > _BLOCK_MAX_MESSAGES:
        half = _BLOCK_MAX_MESSAGES // 2
        omitted = len(rows) - _BLOCK_MAX_MESSAGES
        rendered = rows[:half] + rows[-half:]
        head_tail_truncated = True

    per_msg_cap = min(10000, max(100, _BLOCK_CHAR_BUDGET // max(1, len(rendered))))
    lines = [
        f"[块#{block.id}] {block.time_start} ~ {block.time_end}"
        f" · {block.count}条 · 首问:\"{block.first_user}\""
    ]
    if block.entities:
        lines.insert(1, "实体标签:" + "/".join(block.entities))
    char_cut = 0
    for r in rendered:
        role = r.get("role") or "unknown"
        content = r.get("content") or ""
        tcid = r.get("tool_call_id") if role == "tool" else None
        suffix = f"·{tcid}" if tcid else ""
        prefix = f"{r.get('created_at') or ''} [{role}{suffix}] "
        if len(content) > per_msg_cap:
            content = content[:per_msg_cap] + " ... [TRUNCATED]"
            char_cut += 1
        lines.append(prefix + content)

    if head_tail_truncated:
        lines.insert(len(lines) - (_BLOCK_MAX_MESSAGES // 2),
                     f"... [已精简：中间省略 {omitted} 条消息] ...")

    stats = {
        "head_tail_truncated": head_tail_truncated,
        "omitted_messages": omitted,
        "rendered_messages": len(rendered),
        "char_truncated_messages": char_cut,
        "per_message_char_limit": per_msg_cap,
    }
    return "\n".join(lines), stats


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
            description="Add a message to the session.",
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
            description="Update content of an existing message by ID.",
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
        Tool(
            name="read_history_block",
            description=(
                "按块号取回已归档早期对话的逐字原文。历史索引中 [块#N] 的 N 即 block_id。"
                "返回该块时间范围内的全部原始消息（时间+角色+内容，tool 输出含 tool_call_id 归属）；"
                "超大块自动精简（头尾保留+已精简标注，单条超长动态截断）。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "block_id": {
                        "type": "integer",
                        "description": "归档块号（来自历史索引行的 块#N）",
                    },
                },
                "required": ["block_id"],
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
                # Ensure agent package is importable in stdio mode (project root may not be in sys.path)
                import sys as _sys
                from pathlib import Path as _Path
                _project_root = str(_Path(__file__).resolve().parent.parent.parent.parent.parent)
                if _project_root not in _sys.path:
                    _sys.path.insert(0, _project_root)
                from agent.token_calculator import TokenCalculator
                tokens = TokenCalculator.get().count_message_single(msg.get("role", "user"), content, tool_calls=msg.get("tool_calls"))
            except Exception:
                # Fallback: CJK ~1.5 tokens, ASCII ~0.25 tokens (conservative, same as memory-server)
                cjk = sum(1 for c in content if '一' <= c <= '鿿')
                tokens = max(1, int(cjk * 1.5 + (len(content) - cjk) * 0.25))
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

    elif name == "read_history_block":
        block_id = arguments.get("block_id")
        if block_id is None:
            return [TextContent(type="text", text="Error: block_id is required")]

        # 直读本机 DB（context_blocks.db + messages.db），stdio/同进程两模式通用
        result = read_history_block(block_id)
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
