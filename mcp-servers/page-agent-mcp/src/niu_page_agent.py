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
        "description": """Execute a browser automation task in interactive mode.

BEHAVIOR:
- Simple operations (navigate, click, read): usually 5-15 seconds
- Complex forms: may take 1-2 minutes to complete
- If initial method fails: browser agent MAY try alternative approaches (this is built-in)
- Each call: independent with 2-minute timeout (auto-resets per call)

YOU (Main Agent) CONTROL THE WORKFLOW:
- Break complex tasks into smaller steps
- Each execute_task call is a checkpoint
- If timeout or error: analyze result and decide next steps
- Total control is YOURS through multiple calls

Example MBTI test workflow:
1. execute_task("Navigate to [URL], get first question") → returns question
2. [You analyze and decide answer]
3. execute_task("Click option B, return next question") → returns next question
4. [Repeat...]

Timeouts are NORMAL - they mean the browser agent tried its best but couldn't complete. You decide whether to retry, use alternative approach, or ask user for help.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Natural language task description. Include:\n1. Current context (URL, page state)\n2. What to do\n3. What to return\n\nExamples:\n- 'Navigate to https://example.com, return page title'\n- 'On current MBTI test page, click option A and return next question'\n- 'Fill the form with name=John, email=john@example.com, return success or error'"
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

        # 交互模式：2分钟超时，支持复杂表单填写等操作
        # 如果扩展内部尝试多种方法可能超时，主Agent会收到超时错误并决定下一步
        # 每个execute_task调用都会重置这个超时计时器
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        try:
            return json.loads(error_body)
        except:
            return {"error": error_body}
    except Exception as e:
        # 超时或其他错误，尝试停止任务以清理状态
        if endpoint == "/execute":
            try:
                stop_task()
            except:
                pass
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
    # 添加交互模式提示（扩展可能不完全遵守，但有助于指导行为）
    interactive_hint = """
INTERACTIVE MODE: Return results promptly. If you encounter difficulties, report them clearly so Main Agent can decide next steps.
"""
    enhanced_task = interactive_hint + "\n" + task

    result = _http_post("/execute", {"task": enhanced_task})

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
