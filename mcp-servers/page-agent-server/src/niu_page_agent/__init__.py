"""
Page-Agent MCP Server - AI-native Browser Automation

提供双层工具架构：
- 细粒度工具：browser_navigate, browser_click, browser_input, browser_screenshot
- 粗粒度工具：execute_browser_task
"""
from typing import Any, Dict, List

from .browser_tools import (
    browser_navigate,
    browser_click,
    browser_input,
    browser_screenshot,
    execute_browser_task,
)


TOOL_SCHEMAS = {
    "browser_navigate": {
        "description": "导航到指定 URL",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "目标 URL"
                }
            },
            "required": ["url"]
        }
    },
    "browser_click": {
        "description": "点击页面上的元素",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS 选择器或元素描述"
                }
            },
            "required": ["selector"]
        }
    },
    "browser_input": {
        "description": "在页面元素中输入文本",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS 选择器或元素描述"
                },
                "text": {
                    "type": "string",
                    "description": "要输入的文本"
                }
            },
            "required": ["selector", "text"]
        }
    },
    "browser_screenshot": {
        "description": "截取当前页面截图，返回 base64 编码的图片",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    "execute_browser_task": {
        "description": "执行浏览器自动化任务（粗粒度工具），适合批量操作和表单填写",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "任务描述，例如：填写登录表单、批量注册账号"
                },
                "data": {
                    "type": "object",
                    "description": "任务数据，例如表单字段和值"
                }
            },
            "required": ["task"]
        }
    }
}


def get_tool_schemas() -> List[Dict[str, Any]]:
    """返回工具 schema 列表"""
    schemas = []
    for tool_name, tool_def in TOOL_SCHEMAS.items():
        schemas.append({
            "name": tool_name,
            "description": tool_def["description"],
            "input_schema": tool_def["input_schema"]
        })
    return schemas


__all__ = [
    "get_tool_schemas",
    "browser_navigate",
    "browser_click",
    "browser_input",
    "browser_screenshot",
    "execute_browser_task",
]
