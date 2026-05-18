"""飞书 MCP 服务器 — 日历/任务同步工具

工具函数统一返回 dict，由 MCP stdio 入口点负责 JSON 序列化。
实际 API 调用委托给 client.py。
"""

import json
from loguru import logger

from niu_feishu_server.client import (
    feishu_calendar_create as _client_create,
    feishu_calendar_cancel as _client_cancel,
    feishu_calendar_update as _client_update,
    feishu_sync_enabled,
)
from niu_feishu_server.sync import sync_task_to_feishu, cancel_feishu_event


# ============== TOOL_SCHEMAS ==============

TOOL_SCHEMAS = {
    "feishu_calendar_create": {
        "name": "feishu_calendar_create",
        "description": "创建飞书日历事件。用于将提醒/日程同步到飞书日历。",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "事件标题",
                },
                "start_time": {
                    "type": "string",
                    "description": "开始时间，ISO 8601 格式（如 2026-05-18T09:00:00）",
                },
                "end_time": {
                    "type": "string",
                    "description": "结束时间，ISO 8601 格式",
                },
                "recurrence": {
                    "type": "string",
                    "description": "重复规则（RFC 5545 RRULE 格式），可选",
                },
                "description": {
                    "type": "string",
                    "description": "事件描述，可选",
                },
            },
            "required": ["summary", "start_time", "end_time"],
        },
    },
    "feishu_calendar_cancel": {
        "name": "feishu_calendar_cancel",
        "description": "取消飞书日历事件。用于取消已同步的提醒/日程。",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "飞书日历事件 ID",
                },
            },
            "required": ["event_id"],
        },
    },
    "feishu_calendar_update": {
        "name": "feishu_calendar_update",
        "description": "更新飞书日历事件。用于修改已同步的提醒/日程。",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "飞书日历事件 ID",
                },
                "summary": {
                    "type": "string",
                    "description": "新标题，可选",
                },
                "start_time": {
                    "type": "string",
                    "description": "新开始时间（ISO 8601），可选",
                },
                "end_time": {
                    "type": "string",
                    "description": "新结束时间（ISO 8601），可选",
                },
                "recurrence": {
                    "type": "string",
                    "description": "新重复规则（RFC 5545 RRULE），可选",
                },
                "description": {
                    "type": "string",
                    "description": "新描述，可选",
                },
            },
            "required": ["event_id"],
        },
    },
    "feishu_task_sync": {
        "name": "feishu_task_sync",
        "description": "将定时任务同步到飞书日历。自动根据 cron 表达式生成重复规则。",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_name": {
                    "type": "string",
                    "description": "任务名称",
                },
                "cron": {
                    "type": "string",
                    "description": "cron 表达式（5 位）",
                },
                "prompt": {
                    "type": "string",
                    "description": "任务提示词",
                },
            },
            "required": ["task_name", "cron", "prompt"],
        },
    },
    "feishu_task_cancel": {
        "name": "feishu_task_cancel",
        "description": "取消飞书日历上对应的定时任务事件。",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_name": {
                    "type": "string",
                    "description": "任务名称",
                },
            },
            "required": ["task_name"],
        },
    },
}


def get_tool_schemas():
    return list(TOOL_SCHEMAS.values())


# ============== Tool Functions ==============

def feishu_calendar_create(summary: str, start_time: str, end_time: str,
                          recurrence: str | None = None,
                          description: str = "") -> dict:
    """创建飞书日历事件（MCP 工具入口）"""
    if not feishu_sync_enabled():
        return {"error": "飞书日历同步未启用"}
    return _client_create(
        summary=summary, start_time=start_time, end_time=end_time,
        recurrence=recurrence, description=description,
    )


def feishu_calendar_cancel(event_id: str) -> dict:
    """取消飞书日历事件（MCP 工具入口）"""
    if not feishu_sync_enabled():
        return {"error": "飞书日历同步未启用"}
    return _client_cancel(event_id=event_id)


def feishu_calendar_update(event_id: str, *, summary: str | None = None,
                          start_time: str | None = None,
                          end_time: str | None = None,
                          recurrence: str | None = None,
                          description: str | None = None) -> dict:
    """更新飞书日历事件（MCP 工具入口）"""
    if not feishu_sync_enabled():
        return {"error": "飞书日历同步未启用"}
    return _client_update(
        event_id=event_id, summary=summary, start_time=start_time,
        end_time=end_time, recurrence=recurrence, description=description,
    )


def feishu_task_sync(task_name: str, cron: str, prompt: str) -> dict:
    """将定时任务同步到飞书日历（MCP 工具入口）"""
    if not feishu_sync_enabled():
        return {"error": "飞书日历同步未启用"}
    return sync_task_to_feishu(task_name=task_name, cron=cron, prompt=prompt)


def feishu_task_cancel(task_name: str) -> dict:
    """取消飞书日历上的定时任务事件（MCP 工具入口）"""
    if not feishu_sync_enabled():
        return {"error": "飞书日历同步未启用"}
    return cancel_feishu_event(task_name=task_name)
