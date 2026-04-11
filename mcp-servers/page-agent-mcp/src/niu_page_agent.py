"""
Page Agent MCP Server - Python HTTP Client

Connects to page-agent-mcp Node.js server via HTTP REST API.
The Node.js server runs on port 38402 and provides HTTP API endpoints.
"""

import json
import urllib.request
import urllib.error
from typing import Any, Dict, List


# ============================================================================
# Configuration
# ============================================================================

API_PORT = 38402
API_BASE_URL = f"http://localhost:{API_PORT}"


# ============================================================================
# Tool Schemas (must match page-agent-mcp/src/index.js)
# ============================================================================

TOOL_SCHEMAS = {
    "execute_task": {
        "description": "Execute a task in user's browser. The task description should be specific and include the expected result.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Task description in natural language. Give specific instructions for the task. Steps are preferable. Include the information you want to get after the task is done."
                }
            },
            "required": ["task"]
        }
    },
    "get_status": {
        "description": "Check the current status of the Page Agent hub. Returns { connected, busy }.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    "stop_task": {
        "description": "Stop the currently running browser automation task.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    }
}


def get_tool_schemas() -> List[Dict[str, Any]]:
    """Return tool schemas for page-agent-mcp"""
    schemas = []
    for tool_name, tool_def in TOOL_SCHEMAS.items():
        schemas.append({
            "name": tool_name,
            "description": tool_def["description"],
            "input_schema": tool_def["input_schema"]
        })
    return schemas


# ============================================================================
# HTTP Client
# ============================================================================

def _http_post(endpoint: str, data: dict = None) -> dict:
    """Send HTTP POST request and return response"""
    url = f"{API_BASE_URL}{endpoint}"
    headers = {"Content-Type": "application/json"}

    try:
        req_data = json.dumps(data if data else {}).encode('utf-8')
        req = urllib.request.Request(url, data=req_data, headers=headers, method='POST')

        # 超时时间设置为10分钟，支持复杂浏览器自动化任务
        with urllib.request.urlopen(req, timeout=600) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        try:
            return json.loads(error_body)
        except:
            return {"error": error_body}
    except Exception as e:
        return {"error": str(e)}


def _http_get(endpoint: str) -> dict:
    """Send HTTP GET request and return response"""
    url = f"{API_BASE_URL}{endpoint}"

    try:
        req = urllib.request.Request(url, method='GET')

        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        try:
            return json.loads(error_body)
        except:
            return {"error": error_body}
    except Exception as e:
        return {"error": str(e)}


def execute_task(task: str) -> str:
    """
    Execute a task in user's browser via HTTP API.

    Args:
        task: Task description in natural language.

    Returns:
        Task result or error message.
    """
    result = _http_post("/execute", {"task": task})

    if "error" in result:
        return f"Error: {result['error']}"
    elif result.get("success"):
        return f"Task completed.\n\n{result.get('data', '')}"
    else:
        return f"Task failed.\n\n{result.get('data', '')}"


def get_status() -> str:
    """
    Get the status of Page Agent hub via HTTP API.

    Returns:
        JSON string with { connected, busy }.
    """
    result = _http_get("/status")
    return json.dumps(result, ensure_ascii=False)


def stop_task() -> str:
    """
    Stop the currently running task via HTTP API.

    Returns:
        Status message.
    """
    result = _http_post("/stop")

    if "error" in result:
        return f"Error: {result['error']}"
    else:
        return result.get("message", "Stop signal sent.")
