"""
Scheduler Server - MCP 接口层

提供 MCP 工具接口，内部直接操作 SQLite 数据库。

MCP 适配器作为 stdio 子进程运行，通过共享数据库文件与主进程通信。
"""

import json
import logging
import asyncio
import os
from pathlib import Path
from mcp.server import Server
from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)

# ============== Tool Schemas ==============

TOOL_SCHEMAS = {
    "schedule_task": {
        "name": "schedule_task",
        "description": """创建定时任务，支持单次和循环任务。

参数：
- content (必填): 任务内容，如 "开会"、"吃药"
- scheduled_at (必填): 首次触发时间，ISO格式，如 "2026-04-06T08:00:00"
- event_type (可选): 事件类型，meeting/task/reminder/recurring，默认 reminder
- is_recurring (可选): 是否循环任务，默认 false
- cron_expr (可选): cron 表达式（循环任务必填），如 "0 8 * * *"（每天8点）

示例：
1. 单次提醒：content="开会", scheduled_at="2026-04-07T15:00:00", event_type="meeting"
2. 每天提醒：content="吃药", scheduled_at="2026-04-06T08:00:00", is_recurring=true, cron_expr="0 8 * * *"

重要：相对时间（明天、下周）必须由 Agent 转换为具体的日期时间。""",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "任务内容"},
                "scheduled_at": {"type": "string", "description": "首次触发时间（ISO格式）"},
                "event_type": {"type": "string", "enum": ["meeting", "task", "reminder", "recurring"]},
                "is_recurring": {"type": "boolean", "description": "是否循环任务"},
                "cron_expr": {"type": "string", "description": "cron 表达式"},
                "name": {"type": "string", "description": "任务名称（可选，系统自动注入的任务用 name 标识）"},
                "chat_id": {"type": "string", "description": "群聊 chat_id（兼容保留——定时提醒不推 IM（只写 DB），主 Agent 回复投递目标跟随当前 IM 会话，不按此字段定向）"},
                "task_kind": {
                    "type": "string",
                    "enum": ["reminder", "background_script", "subagent"],
                    "default": "reminder",
                    "description": "任务类型：reminder=提醒式；background_script=后台静默脚本；subagent=子 Agent 静默执行"
                },
                "script_file": {
                    "type": "string",
                    "description": "脚本文件名（仅 background_script 用，如 clean_tmp.py）。脚本须存于 {workspace}/scripts/ 下"
                },
                "agent_name": {
                    "type": "string",
                    "description": "子 Agent 名称（仅 subagent 必填；config/agents/ 或 ~/.niu/agents/ 下须存在同名 md，如 journal-daily-agent）"
                }
            },
            "required": ["content", "scheduled_at"]
        },
    },
    "list_scheduled_tasks": {
        "name": "list_scheduled_tasks",
        "description": """查询定时任务列表。

参数：
- status (可选): 筛选状态，pending/triggered/cancelled

返回：任务列表，包含 id、content、scheduled_at、is_recurring、cron_expr、status、name""",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["pending", "triggered", "cancelled"]}
            }
        },
    },
    "cancel_task": {
        "name": "cancel_task",
        "description": """取消定时任务。

参数：
- task_id: 任务ID

返回：取消结果""",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务ID"}
            },
            "required": ["task_id"]
        },
    },
    "update_task": {
        "name": "update_task",
        "description": """更新定时任务。

参数：
- task_id: 任务ID
- content: 新的任务内容（可选）
- scheduled_at: 新的触发时间（可选）
- cron_expr: 新的 cron 表达式（可选）

返回：更新结果""",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务ID"},
                "content": {"type": "string", "description": "新的任务内容"},
                "scheduled_at": {"type": "string", "description": "新的触发时间"},
                "cron_expr": {"type": "string", "description": "新的 cron 表达式"}
            },
            "required": ["task_id"]
        },
    },
}


def get_tool_schemas() -> list[dict]:
    """返回所有工具的 schema 列表（用于 MCP Loader 注册）"""
    return list(TOOL_SCHEMAS.values())


def _get_db_path() -> str:
    """获取数据库路径"""
    # 优先使用环境变量
    db_path = os.environ.get("SCHEDULER_DB_PATH")
    if db_path and Path(db_path).parent.exists():
        return db_path

    # 从 ~/.niu/memory.json 读取工作目录
    memory_path = Path.home() / ".niu" / "memory.json"
    if memory_path.exists():
        try:
            with open(memory_path, "r", encoding="utf-8") as f:
                memory = json.load(f)
                workspace = memory.get("workspace", {}).get("path")
                if workspace and Path(workspace).exists():
                    return str(Path(workspace) / "scheduled_tasks.db")
        except Exception:
            pass

    # 默认路径
    return str(Path.home() / ".niu" / "scheduled_tasks.db")


# Import TaskStore
try:
    from niu_api.internal.scheduler.task_store import TaskStore
except ImportError:
    try:
        from niu_scheduler_server.store import TaskStore
    except ImportError:
        logger.error("Cannot import TaskStore. Ensure PYTHONPATH includes niu_api.")
        TaskStore = None


def _get_store():
    """Get TaskStore instance."""
    if TaskStore is None:
        raise RuntimeError("TaskStore not available. Ensure niu_api is in PYTHONPATH.")
    return TaskStore(_get_db_path())


def _check_subagent_agent_name(agent_name: str | None) -> str | None:
    """校验 subagent 任务的 agent_name（返回错误消息；通过返回 None）。

    存在性检查复用 agent.subagent._resolve_agent_md_path 的双目录语义
    （config/agents/ + ~/.niu/agents/）；独立 stdio 模式下 agent 包不可用时，
    回退本地等价双目录检查（与 _resolve_agent_md_path 保持同步）。
    """
    if not agent_name:
        return "subagent 任务必须提供 agent_name"
    not_found = f"子 Agent '{agent_name}' 不存在（config/agents/{agent_name}.md 与 ~/.niu/agents/{agent_name}.md 均未找到）"
    try:
        from agent.subagent import _resolve_agent_md_path
    except Exception as e:
        # 独立 stdio 模式 / agent 包不可用：本地等价双目录检查（kebab-case + 双目录）
        logger.warning(f"[Scheduler] agent 包不可用，agent_name 校验回退本地双目录检查: {e}")
        import re as _re

        if not _re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", agent_name):
            return f"agent_name '{agent_name}' 非法（须为 kebab-case）"
        candidates = [Path.home() / ".niu" / "agents" / f"{agent_name}.md"]
        try:
            import niu_api
            candidates.insert(0, Path(niu_api.__file__).resolve().parent.parent / "config" / "agents" / f"{agent_name}.md")
        except ImportError:
            pass
        return None if any(p.exists() for p in candidates) else not_found
    return None if _resolve_agent_md_path(agent_name) else not_found


# ============== Tool Functions (for in-process ToolRegistry) ==============

def schedule_task(
    content: str,
    scheduled_at: str,
    event_type: str = "reminder",
    is_recurring: bool = False,
    cron_expr: str = None,
    name: str = None,
    chat_id: str = None,
    task_kind: str = "reminder",
    script_file: str = None,
    agent_name: str = None,
) -> dict:
    """创建定时任务，支持单次和循环任务。"""
    if task_kind == "background_script" and not script_file:
        return {"error": "background_script 任务必须提供 script_file"}
    if script_file and ("/" in script_file or ".." in script_file or "\\" in script_file):
        return {"error": "script_file 不能含路径分隔符或 .."}
    if task_kind == "subagent":
        err = _check_subagent_agent_name(agent_name)
        if err:
            return {"error": err}
    try:
        store = _get_store()
        task_id = store.create_task(
            content=content,
            scheduled_at=scheduled_at,
            event_type=event_type,
            is_recurring=is_recurring,
            cron_expr=cron_expr,
            name=name,
            chat_id=chat_id,
            task_kind=task_kind,
            script_file=script_file,
            agent_name=agent_name,
        )
        return {"status": "success", "task_id": task_id, "message": f"已创建定时任务：{content}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def list_scheduled_tasks(status: str = None) -> dict:
    """查询定时任务列表。"""
    try:
        store = _get_store()
        tasks = store.list_tasks(status)
        return {"status": "success", "tasks": tasks, "count": len(tasks)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def cancel_task(task_id: str) -> dict:
    """取消定时任务。"""
    try:
        store = _get_store()
        success = store.cancel_task(task_id)
        return {"status": "success" if success else "error", "message": "任务已取消" if success else "任务不存在或已完成"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def update_task(
    task_id: str,
    content: str = None,
    scheduled_at: str = None,
    cron_expr: str = None,
) -> dict:
    """更新定时任务。"""
    try:
        store = _get_store()
        success = store.update_task(
            task_id=task_id,
            content=content,
            scheduled_at=scheduled_at,
            cron_expr=cron_expr,
        )
        return {"status": "success" if success else "error", "message": "任务已更新" if success else "任务不存在或已完成"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def run_server():
    """运行 MCP 服务器（直接操作数据库）"""
    if TaskStore is None:
        logger.error("TaskStore not available, cannot start MCP server")
        return

    # 初始化 TaskStore
    db_path = _get_db_path()
    store = TaskStore(db_path)
    logger.info(f"Scheduler MCP server starting with database: {db_path}")

    server = Server("scheduler-server")

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name="schedule_task",
                description="""创建定时任务，支持单次和循环任务。

参数：
- content (必填): 任务内容，如 "开会"、"吃药"
- scheduled_at (必填): 首次触发时间，ISO格式，如 "2026-04-06T08:00:00"
- event_type (可选): 事件类型，meeting/task/reminder/recurring，默认 reminder
- is_recurring (可选): 是否循环任务，默认 false
- cron_expr (可选): cron 表达式（循环任务必填），如 "0 8 * * *"（每天8点）

示例：
1. 单次提醒：content="开会", scheduled_at="2026-04-07T15:00:00", event_type="meeting"
2. 每天提醒：content="吃药", scheduled_at="2026-04-06T08:00:00", is_recurring=true, cron_expr="0 8 * * *"

重要：相对时间（明天、下周）必须由 Agent 转换为具体的日期时间。""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "任务内容"},
                        "scheduled_at": {"type": "string", "description": "首次触发时间（ISO格式）"},
                        "event_type": {"type": "string", "enum": ["meeting", "task", "reminder", "recurring"]},
                        "is_recurring": {"type": "boolean", "description": "是否循环任务"},
                        "cron_expr": {"type": "string", "description": "cron 表达式"},
                        "name": {"type": "string", "description": "任务名称（可选，系统自动注入的任务用 name 标识）"},
                        "chat_id": {"type": "string", "description": "群聊 chat_id（兼容保留——定时提醒不推 IM（只写 DB），主 Agent 回复投递目标跟随当前 IM 会话，不按此字段定向）"},
                        "task_kind": {
                            "type": "string",
                            "enum": ["reminder", "background_script", "subagent"],
                            "default": "reminder",
                            "description": "任务类型：reminder=提醒式；background_script=后台静默脚本；subagent=子 Agent 静默执行"
                        },
                        "script_file": {
                            "type": "string",
                            "description": "脚本文件名（仅 background_script 用，如 clean_tmp.py）。脚本须存于 {workspace}/scripts/ 下"
                        },
                        "agent_name": {
                            "type": "string",
                            "description": "子 Agent 名称（仅 subagent 必填；config/agents/ 或 ~/.niu/agents/ 下须存在同名 md，如 journal-daily-agent）"
                        }
                    },
                    "required": ["content", "scheduled_at"]
                }
            ),
            Tool(
                name="list_scheduled_tasks",
                description="""查询定时任务列表。

参数：
- status (可选): 筛选状态，pending/triggered/cancelled

返回：任务列表，包含 id、content、scheduled_at、is_recurring、cron_expr、status、name""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["pending", "triggered", "cancelled"]}
                    }
                }
            ),
            Tool(
                name="cancel_task",
                description="""取消定时任务。

参数：
- task_id: 任务ID

返回：取消结果""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "任务ID"}
                    },
                    "required": ["task_id"]
                }
            ),
            Tool(
                name="update_task",
                description="""更新定时任务。

参数：
- task_id: 任务ID
- content: 新的任务内容（可选）
- scheduled_at: 新的触发时间（可选）
- cron_expr: 新的 cron 表达式（可选）

返回：更新结果""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "任务ID"},
                        "content": {"type": "string", "description": "新的任务内容"},
                        "scheduled_at": {"type": "string", "description": "新的触发时间"},
                        "cron_expr": {"type": "string", "description": "新的 cron 表达式"}
                    },
                    "required": ["task_id"]
                }
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            if name == "schedule_task":
                task_kind = arguments.get("task_kind", "reminder")
                script_file = arguments.get("script_file")
                agent_name = arguments.get("agent_name")
                if task_kind == "background_script" and not script_file:
                    return [TextContent(type="text", text=json.dumps(
                        {"error": "background_script 任务必须提供 script_file"}, ensure_ascii=False))]
                if script_file and ("/" in script_file or ".." in script_file or "\\" in script_file):
                    return [TextContent(type="text", text=json.dumps(
                        {"error": "script_file 不能含路径分隔符或 .."}, ensure_ascii=False))]
                if task_kind == "subagent":
                    err = _check_subagent_agent_name(agent_name)
                    if err:
                        return [TextContent(type="text", text=json.dumps(
                            {"error": err}, ensure_ascii=False))]
                task_id = store.create_task(
                    content=arguments["content"],
                    scheduled_at=arguments["scheduled_at"],
                    event_type=arguments.get("event_type", "reminder"),
                    is_recurring=arguments.get("is_recurring", False),
                    cron_expr=arguments.get("cron_expr"),
                    name=arguments.get("name"),
                    chat_id=arguments.get("chat_id"),
                    task_kind=task_kind,
                    script_file=script_file,
                    agent_name=agent_name
                )

                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "status": "success",
                        "task_id": task_id,
                        "message": f"✅ 已创建定时任务：{arguments['content']}"
                    }, ensure_ascii=False)
                )]

            elif name == "list_scheduled_tasks":
                status = arguments.get("status")
                tasks = store.list_tasks(status)

                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "status": "success",
                        "tasks": tasks,
                        "count": len(tasks)
                    }, ensure_ascii=False, indent=2)
                )]

            elif name == "cancel_task":
                success = store.cancel_task(arguments["task_id"])

                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "status": "success" if success else "error",
                        "message": "✅ 任务已取消" if success else "❌ 任务不存在或已完成"
                    }, ensure_ascii=False)
                )]

            elif name == "update_task":
                success = store.update_task(
                    task_id=arguments["task_id"],
                    content=arguments.get("content"),
                    scheduled_at=arguments.get("scheduled_at"),
                    cron_expr=arguments.get("cron_expr")
                )

                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "status": "success" if success else "error",
                        "message": "✅ 任务已更新" if success else "❌ 任务不存在或已完成"
                    }, ensure_ascii=False)
                )]

            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

        except Exception as e:
            logger.error(f"Tool error: {e}", exc_info=True)
            return [TextContent(
                type="text",
                text=json.dumps({
                    "status": "error",
                    "message": str(e)
                }, ensure_ascii=False)
            )]

    logger.info("Scheduler MCP server starting (direct DB access)...")
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def main():
    """Main entry point - run as MCP server"""
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting scheduler-server in MCP mode")
    asyncio.run(run_server())
