"""
Memory Server - 智能记忆管理 MCP 服务器

提供记忆存储、检索功能，支持 L0/L1/L2 三层存储。
"""

from mcp.server import Server
from mcp.types import Tool, TextContent
from loguru import logger
import json
import asyncio
from typing import Optional, Dict, Any

from .storage import MemoryStorage

# 创建 MCP 服务器
server = Server("memory-server")

# 初始化存储
storage = MemoryStorage()

# ============================================================================
# Tool Schemas
# ============================================================================

TOOL_SCHEMAS = {
    "remember": {
        "name": "remember",
        "description": "保存长期记忆（自动生成 L0/L1/L2 三层）",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "记忆内容"},
                "memory_type": {
                    "type": "string",
                    "description": "记忆类型（environment/preferences/skills/experiences/facts）",
                    "enum": ["environment", "preferences", "skills", "experiences", "facts"],
                },
                "title": {
                    "type": "string",
                    "description": "记忆标题（≤20字符），可选，不提供则自动生成",
                },
                "importance": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "重要性评分（0-1），可选，不提供则根据类型自动设置",
                },
                "metadata": {
                    "type": "object",
                    "description": "可选的元数据",
                },
            },
            "required": ["content", "memory_type"],
        },
    },
    "recall": {
        "name": "recall",
        "description": "检索相关记忆（语义搜索）",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询"},
                "limit": {
                    "type": "integer",
                    "description": "返回数量限制",
                    "default": 5,
                },
                "memory_type": {
                    "type": "string",
                    "description": "可选的记忆类型过滤",
                },
                "level": {
                    "type": "string",
                    "description": "搜索层级（l0/l1/l2）",
                    "default": "l1",
                },
            },
            "required": ["query"],
        },
    },
    "update_memory": {
        "name": "update_memory",
        "description": "更新已有记忆",
        "input_schema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆 ID"},
                "content": {"type": "string", "description": "新内容"},
                "metadata": {
                    "type": "object",
                    "description": "可选的元数据更新",
                },
            },
            "required": ["memory_id", "content"],
        },
    },
    "get_memory_stats": {
        "name": "get_memory_stats",
        "description": "获取记忆统计信息",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "cleanup_memories": {
        "name": "cleanup_memories",
        "description": "清理过期记忆",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "清理多少天前的记忆",
                    "default": 30,
                },
            },
        },
    },
    "link_memories": {
        "name": "link_memories",
        "description": "关联两条记忆",
        "input_schema": {
            "type": "object",
            "properties": {
                "memory_id_1": {"type": "string", "description": "记忆 ID 1"},
                "memory_id_2": {"type": "string", "description": "记忆 ID 2"},
                "relation": {"type": "string", "description": "关系描述"},
            },
            "required": ["memory_id_1", "memory_id_2", "relation"],
        },
    },
    "user_memory_remember": {
        "name": "user_memory_remember",
        "description": "添加用户长期记忆（最多5条，每条≤200 token）。记忆将永久驻留在系统提示词中。若已满(5/5)，必须先调用 user_memory_forget 删除旧记忆。",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "记忆内容（≤200 token，约300中文字符）",
                },
            },
            "required": ["content"],
        },
    },
    "user_memory_forget": {
        "name": "user_memory_forget",
        "description": "删除用户长期记忆。按序号(index)或关键词(keyword)匹配删除。",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "integer",
                    "description": "记忆序号（1-5），优先于 keyword",
                },
                "keyword": {
                    "type": "string",
                    "description": "不区分大小写的子串匹配",
                },
            },
        },
    },
    "user_memory_list": {
        "name": "user_memory_list",
        "description": "查看当前所有用户长期记忆",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
}


def get_tool_schemas() -> list[dict]:
    """返回所有工具的 schema 列表（用于 MCP Loader 注册）"""
    return list(TOOL_SCHEMAS.values())


# ============================================================================
# Tool definitions
# ============================================================================


def get_tool_definitions() -> list[Tool]:
    """返回所有工具定义"""
    return [
        Tool(
            name="remember",
            description="保存长期记忆（自动生成 L0/L1/L2 三层）",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "记忆内容"},
                    "memory_type": {
                        "type": "string",
                        "description": "记忆类型（environment/preferences/skills/experiences/facts）",
                        "enum": ["environment", "preferences", "skills", "experiences", "facts"],
                    },
                    "title": {
                        "type": "string",
                        "description": "记忆标题（≤20字符），可选，不提供则自动生成",
                    },
                    "importance": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "重要性评分（0-1），可选，不提供则根据类型自动设置",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "可选的元数据",
                    },
                },
                "required": ["content", "memory_type"],
            },
        ),
        Tool(
            name="recall",
            description="检索相关记忆（语义搜索）",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索查询"},
                    "limit": {
                        "type": "integer",
                        "description": "返回数量限制",
                        "default": 5,
                    },
                    "memory_type": {
                        "type": "string",
                        "description": "可选的记忆类型过滤",
                    },
                    "level": {
                        "type": "string",
                        "description": "搜索层级（l0/l1/l2）",
                        "default": "l1",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="update_memory",
            description="更新已有记忆",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆 ID"},
                    "content": {"type": "string", "description": "新内容"},
                    "metadata": {
                        "type": "object",
                        "description": "可选的元数据更新",
                    },
                },
                "required": ["memory_id", "content"],
            },
        ),
        Tool(
            name="get_memory_stats",
            description="获取记忆统计信息",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="cleanup_memories",
            description="清理过期记忆",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "清理多少天前的记忆",
                        "default": 30,
                    },
                },
            },
        ),
        Tool(
            name="link_memories",
            description="关联两条记忆",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id_1": {"type": "string", "description": "记忆 ID 1"},
                    "memory_id_2": {"type": "string", "description": "记忆 ID 2"},
                    "relation": {"type": "string", "description": "关系描述"},
                },
                "required": ["memory_id_1", "memory_id_2", "relation"],
            },
        ),
        Tool(
            name="user_memory_remember",
            description="添加用户长期记忆（最多5条，每条≤200 token）。记忆将永久驻留在系统提示词中。若已满(5/5)，必须先调用 user_memory_forget 删除旧记忆。",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "记忆内容（≤200 token，约300中文字符）"},
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="user_memory_forget",
            description="删除用户长期记忆。按序号(index)或关键词(keyword)匹配删除。",
            inputSchema={
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "记忆序号（1-5），优先于 keyword"},
                    "keyword": {"type": "string", "description": "不区分大小写的子串匹配"},
                },
            },
        ),
        Tool(
            name="user_memory_list",
            description="查看当前所有用户长期记忆",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


# ============================================================================
# Tool implementations
# ============================================================================


async def remember_handler(
    content: str,
    memory_type: str,
    metadata: Optional[Dict[str, Any]] = None,
    title: Optional[str] = None,
    importance: Optional[float] = None,
) -> dict:
    """保存记忆（L0/L1/L2 三层）"""
    logger.info(f"保存记忆: type={memory_type}, title={title}, importance={importance}")

    memory_id = storage.store_memory(content, memory_type, metadata, title, importance)

    return {
        "status": "success",
        "memory_id": memory_id,
        "message": f"✅ 已保存记忆（L0/L1/L2）: {memory_id}",
    }


async def recall_handler(
    query: str, limit: int = 5, memory_type: Optional[str] = None, level: str = "l1"
) -> list[dict]:
    """检索相关记忆"""
    logger.info(f"搜索记忆: query={query[:50]}..., limit={limit}, level={level}")

    # 构建过滤条件
    filter_dict = {}
    if memory_type:
        filter_dict["memory_type"] = memory_type

    # 搜索
    results = storage.search_memories(
        query=query, limit=limit, filter_dict=filter_dict if filter_dict else None, level=level
    )

    logger.info(f"搜索完成: 找到 {len(results)} 条相关记忆")
    return results


async def update_memory_handler(
    memory_id: str, content: str, metadata: Optional[Dict[str, Any]] = None
) -> dict:
    """更新记忆"""
    logger.info(f"更新记忆: {memory_id}")

    try:
        new_id = storage.update_memory(memory_id, content, metadata)
        return {
            "status": "success",
            "memory_id": new_id,
            "message": f"✅ 已更新记忆: {new_id}",
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"❌ 更新失败: {str(e)}",
        }


async def get_memory_stats_handler() -> dict:
    """获取记忆统计"""
    stats = storage.get_memory_stats()
    return {
        "status": "success",
        "stats": stats,
    }


async def cleanup_memories_handler(days: int = 30) -> dict:
    """清理过期记忆"""
    logger.info(f"清理记忆: {days} 天前")

    deleted_count = storage.cleanup_memories(days)

    return {
        "status": "success",
        "deleted_count": deleted_count,
        "message": f"✅ 已清理 {deleted_count} 条过期记忆",
    }


async def link_memories_handler(
    memory_id_1: str, memory_id_2: str, relation: str
) -> dict:
    """关联两条记忆"""
    logger.info(f"关联记忆: {memory_id_1} <-> {memory_id_2}")

    success = storage.link_memories(memory_id_1, memory_id_2, relation)

    if success:
        return {
            "status": "success",
            "message": f"✅ 已关联记忆: {memory_id_1} <-> {memory_id_2} ({relation})",
        }
    else:
        return {
            "status": "error",
            "message": "❌ 关联失败",
        }


# ============================================================================
# User memory tools (memory.json permanent array)
# ============================================================================

MEMORY_JSON_PATH = None  # Set at first call

MAX_PERMANENT_ITEMS = 5
MAX_TOKEN_PER_ITEM = 200  # ~300 Chinese chars


def _get_memory_json_path():
    """Get path to ~/.niu/memory.json"""
    global MEMORY_JSON_PATH
    if MEMORY_JSON_PATH is None:
        from pathlib import Path
        MEMORY_JSON_PATH = Path.home() / ".niu" / "memory.json"
    return MEMORY_JSON_PATH


def _read_memory_json() -> dict:
    """Read memory.json, return dict with at least {permanent: []}"""
    path = _get_memory_json_path()
    if not path.exists():
        return {"permanent": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "permanent" not in data:
            data["permanent"] = []
        # Truncate if over limit (keep first 5, drop from end)
        if len(data["permanent"]) > MAX_PERMANENT_ITEMS:
            data["permanent"] = data["permanent"][:MAX_PERMANENT_ITEMS]
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
    except Exception:
        return {"permanent": []}


def _write_memory_json(data: dict):
    """Write memory.json"""
    path = _get_memory_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def user_memory_remember_handler(content: str) -> dict:
    """添加用户长期记忆到 memory.json permanent 数组"""
    data = _read_memory_json()
    permanent = data["permanent"]

    if len(permanent) >= MAX_PERMANENT_ITEMS:
        return {
            "status": "error",
            "message": f"记忆已满({len(permanent)}/{MAX_PERMANENT_ITEMS})，请先调用 user_memory_forget 删除旧记忆。",
            "current_memories": permanent,
        }

    # Rough token estimate: 1 token ≈ 1.5 Chinese chars
    estimated_tokens = len(content) / 1.5
    if estimated_tokens > MAX_TOKEN_PER_ITEM:
        return {
            "status": "error",
            "message": f"记忆内容过长（约{int(estimated_tokens)} token，上限{MAX_TOKEN_PER_ITEM}），请精简后重试。",
        }

    permanent.append(content)
    _write_memory_json(data)

    return {
        "status": "success",
        "message": f"✅ 已添加记忆({len(permanent)}/{MAX_PERMANENT_ITEMS})",
        "current_memories": permanent,
    }


async def user_memory_forget_handler(index: int = None, keyword: str = None) -> dict:
    """删除用户长期记忆"""
    data = _read_memory_json()
    permanent = data["permanent"]

    if not permanent:
        return {"status": "error", "message": "没有可删除的记忆"}

    if index is not None:
        # Index is 1-based
        if index < 1 or index > len(permanent):
            return {"status": "error", "message": f"序号超出范围(1-{len(permanent)})"}
        removed = permanent.pop(index - 1)
        _write_memory_json(data)
        return {
            "status": "success",
            "message": f"✅ 已删除第{index}条记忆: {removed}",
            "current_memories": permanent,
        }

    if keyword:
        keyword_lower = keyword.lower()
        for i, item in enumerate(permanent):
            if keyword_lower in item.lower():
                removed = permanent.pop(i)
                _write_memory_json(data)
                return {
                    "status": "success",
                    "message": f"✅ 已删除匹配'{keyword}'的记忆: {removed}",
                    "current_memories": permanent,
                }
        return {"status": "error", "message": f"未找到包含'{keyword}'的记忆", "current_memories": permanent}

    return {"status": "error", "message": "请提供 index 或 keyword 参数"}


async def user_memory_list_handler() -> dict:
    """查看当前所有用户长期记忆"""
    data = _read_memory_json()
    permanent = data["permanent"]

    return {
        "status": "success",
        "count": len(permanent),
        "max": MAX_PERMANENT_ITEMS,
        "memories": permanent,
    }


# ============================================================================
# MCP handlers
# ============================================================================


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return get_tool_definitions()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    try:
        if name == "remember":
            result = await remember_handler(
                content=arguments["content"],
                memory_type=arguments["memory_type"],
                metadata=arguments.get("metadata"),
                title=arguments.get("title"),
                importance=arguments.get("importance"),
            )
        elif name == "recall":
            result = await recall_handler(
                query=arguments["query"],
                limit=arguments.get("limit", 5),
                memory_type=arguments.get("memory_type"),
                level=arguments.get("level", "l1"),
            )
        elif name == "update_memory":
            result = await update_memory_handler(
                memory_id=arguments["memory_id"],
                content=arguments["content"],
                metadata=arguments.get("metadata"),
            )
        elif name == "get_memory_stats":
            result = await get_memory_stats_handler()
        elif name == "cleanup_memories":
            result = await cleanup_memories_handler(
                days=arguments.get("days", 30),
            )
        elif name == "link_memories":
            result = await link_memories_handler(
                memory_id_1=arguments["memory_id_1"],
                memory_id_2=arguments["memory_id_2"],
                relation=arguments["relation"],
            )
        elif name == "user_memory_remember":
            result = await user_memory_remember_handler(
                content=arguments["content"],
            )
        elif name == "user_memory_forget":
            result = await user_memory_forget_handler(
                index=arguments.get("index"),
                keyword=arguments.get("keyword"),
            )
        elif name == "user_memory_list":
            result = await user_memory_list_handler()
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    except Exception as e:
        logger.exception(f"Error executing tool {name}: {e}")
        return [TextContent(type="text", text=f"Error: {e}")]


# ============================================================================
# Main
# ============================================================================


def main():
    """MCP 服务器入口"""
    import mcp.server.stdio

    logger.info("Memory Server 启动中...")

    async def run():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    asyncio.run(run())


if __name__ == "__main__":
    main()
