"""飞书 MCP 服务器 -- Phase 1 最小工具集（日历同步相关）"""

import json
from loguru import logger

# ============== Tool Schemas ==============

TOOL_SCHEMAS = {
    "feishu_calendar_create": {
        "name": "feishu_calendar_create",
        "description": """创建飞书日历事件（由同步钩子调用，不暴露给主 Agent）。

参数：
- summary: 事件标题
- start_time: 开始时间（ISO格式）
- end_time: 结束时间（ISO格式）
- description: 事件描述（可选）
- recurrence: RRULE 重复规则（可选）""",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "事件标题"},
                "start_time": {"type": "string", "description": "开始时间（ISO格式）"},
                "end_time": {"type": "string", "description": "结束时间（ISO格式）"},
                "description": {"type": "string", "description": "事件描述"},
                "recurrence": {"type": "string", "description": "RRULE 重复规则"},
            },
            "required": ["summary", "start_time", "end_time"]
        },
    },
    "feishu_calendar_cancel": {
        "name": "feishu_calendar_cancel",
        "description": """取消飞书日历事件。

参数：
- event_id: 飞书日历事件ID""",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "飞书日历事件ID"}
            },
            "required": ["event_id"]
        },
    },
    "feishu_calendar_update": {
        "name": "feishu_calendar_update",
        "description": """更新飞书日历事件。

参数：
- event_id: 飞书日历事件ID
- summary: 新标题（可选）
- start_time: 新开始时间（可选）
- end_time: 新结束时间（可选）""",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "飞书日历事件ID"},
                "summary": {"type": "string", "description": "新标题"},
                "start_time": {"type": "string", "description": "新开始时间"},
                "end_time": {"type": "string", "description": "新结束时间"},
            },
            "required": ["event_id"]
        },
    },
}


def get_tool_schemas() -> list[dict]:
    """返回所有工具的 schema 列表（用于 MCP Loader 注册）"""
    return list(TOOL_SCHEMAS.values())


# ============== 同步工具函数（供 scheduler-server 调用） ==============

def feishu_calendar_create(summary: str, start_time: str, end_time: str,
                           description: str = "", recurrence: str = "") -> str:
    """创建飞书日历事件"""
    try:
        from .client import get_feishu_client
        import lark_oapi as lark
        from lark_oapi.api.calendar.v4 import CreateEventRequest, CreateEventRequestBody

        client = get_feishu_client()

        body = CreateEventRequestBody.builder() \
            .summary(summary) \
            .description(description) \
            .start_time({"timestamp": _iso_to_timestamp(start_time)}) \
            .end_time({"timestamp": _iso_to_timestamp(end_time)}) \
            .build()

        if recurrence:
            body.recurrence = [recurrence]

        # 使用主日历
        request = CreateEventRequest.builder() \
            .calendar_id("primary") \
            .request_body(body) \
            .build()

        response = client.calendar.v4.event.create(request)

        if response.success():
            event_id = response.data.event.event_id
            logger.info(f"[Feishu] Calendar event created: {event_id}")
            return json.dumps({"status": "success", "event_id": event_id})
        else:
            logger.error(f"[Feishu] Calendar create failed: {response.code} {response.msg}")
            return json.dumps({"status": "error", "message": f"{response.code}: {response.msg}"})

    except Exception as e:
        logger.error(f"[Feishu] Calendar create error: {e}")
        return json.dumps({"status": "error", "message": str(e)})


def feishu_calendar_cancel(event_id: str) -> str:
    """取消飞书日历事件"""
    try:
        from .client import get_feishu_client
        import lark_oapi as lark
        from lark_oapi.api.calendar.v4 import DeleteEventRequest

        client = get_feishu_client()

        request = DeleteEventRequest.builder() \
            .calendar_id("primary") \
            .event_id(event_id) \
            .build()

        response = client.calendar.v4.event.delete(request)

        if response.success():
            logger.info(f"[Feishu] Calendar event cancelled: {event_id}")
            return json.dumps({"status": "success"})
        else:
            logger.error(f"[Feishu] Calendar cancel failed: {response.code} {response.msg}")
            return json.dumps({"status": "error", "message": f"{response.code}: {response.msg}"})

    except Exception as e:
        logger.error(f"[Feishu] Calendar cancel error: {e}")
        return json.dumps({"status": "error", "message": str(e)})


def feishu_calendar_update(event_id: str, summary: str = None,
                           start_time: str = None, end_time: str = None) -> str:
    """更新飞书日历事件"""
    try:
        from .client import get_feishu_client
        import lark_oapi as lark
        from lark_oapi.api.calendar.v4 import PatchEventRequest, PatchEventRequestBody

        client = get_feishu_client()

        body_builder = PatchEventRequestBody.builder()
        if summary:
            body_builder = body_builder.summary(summary)
        if start_time:
            body_builder = body_builder.start_time({"timestamp": _iso_to_timestamp(start_time)})
        if end_time:
            body_builder = body_builder.end_time({"timestamp": _iso_to_timestamp(end_time)})

        request = PatchEventRequest.builder() \
            .calendar_id("primary") \
            .event_id(event_id) \
            .request_body(body_builder.build()) \
            .build()

        response = client.calendar.v4.event.patch(request)

        if response.success():
            logger.info(f"[Feishu] Calendar event updated: {event_id}")
            return json.dumps({"status": "success"})
        else:
            logger.error(f"[Feishu] Calendar update failed: {response.code} {response.msg}")
            return json.dumps({"status": "error", "message": f"{response.code}: {response.msg}"})

    except Exception as e:
        logger.error(f"[Feishu] Calendar update error: {e}")
        return json.dumps({"status": "error", "message": str(e)})


def _iso_to_timestamp(iso_str: str) -> int:
    """ISO 时间字符串 -> Unix 时间戳（秒）"""
    from datetime import datetime
    dt = datetime.fromisoformat(iso_str)
    return int(dt.timestamp())


def main():
    """MCP stdio 入口点（Phase 1 不需要独立运行，保留入口）"""
    from mcp.server import Server
    from mcp.types import Tool, TextContent
    import asyncio

    server = Server("feishu-server")

    @server.list_tools()
    async def list_tools():
        return [Tool(**schema) for schema in get_tool_schemas()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name in TOOL_SCHEMAS:
            fn = globals().get(name)
            if fn:
                result = fn(**arguments)
                return [TextContent(type="text", text=result)]
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    asyncio.run(server.run())