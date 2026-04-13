"""
Niu Browser MCP Server

Controls browser via Chrome Extension, replacing Playwright architecture.

Architecture:
- browser_navigate MCP tool -> launch browser (if needed) -> send command to Extension via WSBridge
- Extension content_script -> extract DOM state, execute operations in page
- Results returned via WebSocket -> Python -> LLM

Advantages over Playwright:
- LLM directly sees indexed interactive elements, no need for code_run
- Extension persists in every page, handles new tabs automatically
- Simulates real mouse events (PointerEvent), harder to detect than Playwright
- No Playwright/BrowserManager/SyncPageProxy/CDP dependency
"""

import subprocess
import time
from loguru import logger

from .ws_bridge import WSBridge
from .launcher import launch_browser


# Global state
_browser_proc: subprocess.Popen | None = None
_ws_bridge: WSBridge | None = None


def _get_bridge() -> WSBridge:
    """Get or create WSBridge singleton (auto-starts on first call)."""
    global _ws_bridge
    if _ws_bridge is None:
        _ws_bridge = WSBridge()
        _ws_bridge.start()
    return _ws_bridge


def _ensure_browser_and_connection() -> WSBridge:
    """Ensure browser is running and Extension is connected."""
    global _browser_proc

    bridge = _get_bridge()

    # If Extension not connected, try launching browser
    if not bridge.connected:
        if _browser_proc is None or _browser_proc.poll() is not None:
            logger.info("Starting browser with extension...")
            _browser_proc = launch_browser()
            # Wait for Extension to connect (max 15 seconds)
            # Browser needs time to start, Extension needs time to load and connect
            for i in range(30):
                if bridge.connected:
                    logger.info(f"Extension connected after {(i+1)*0.5:.1f}s")
                    break
                time.sleep(0.5)
            else:
                logger.warning("Extension not connected after 15s, returning bridge anyway for retry")

    return bridge


def browser_navigate(
    url: str,
    wait_until: str = "domcontentloaded"
) -> dict:
    """
    Launch browser and navigate to URL. Automatically returns structured page state.

    Args:
        url: Target URL
        wait_until: Wait strategy (kept for compatibility, handled by Extension)

    Returns:
        Dict with page state: url, title, elements (indexed interactive elements), pageInfo
    """
    try:
        bridge = _ensure_browser_and_connection()

        # Send navigate command (hub.js handles navigation + returns page state)
        result = bridge.send_command("navigate", url=url, timeout=60)

        if result.get("success"):
            data = result.get("data", {})
            return {
                "status": "success",
                "url": data.get("url", url),
                "title": data.get("title", ""),
                "elements": data.get("elements", ""),
                "pageInfo": data.get("pageInfo", {}),
            }
        else:
            return {
                "status": "error",
                "message": f"Navigation failed: {result.get('message', 'Unknown error')}",
            }

    except Exception as e:
        logger.error(f"browser_navigate failed: {e}")
        return {"status": "error", "message": str(e)}


def browser_interact(
    action: str,
    index: int = 0,
    text: str = "",
    option: str = "",
    direction: str = "down",
    amount: float = 1.0,
) -> dict:
    """
    Interact with page: click, input, select, scroll, get_state.

    Args:
        action: Action type - click, input, select, scroll, get_state
        index: Element index (from elements list returned by browser_navigate)
        text: Input text (for action=input)
        option: Select option (for action=select)
        direction: Scroll direction (for action=scroll)
        amount: Scroll amount in pages (for action=scroll)

    Returns:
        Operation result + updated page state
    """
    try:
        bridge = _ensure_browser_and_connection()

        action_map = {
            "click": lambda: bridge.send_command("click", index=index),
            "input": lambda: bridge.send_command("input_text", index=index, text=text),
            "select": lambda: bridge.send_command("select_option", index=index, option=option),
            "scroll": lambda: bridge.send_command("scroll", direction=direction, amount=amount),
            "get_state": lambda: bridge.send_command("get_state"),
        }

        if action not in action_map:
            return {"status": "error", "message": f"Unknown action: {action}. Supported: {list(action_map.keys())}"}

        result = action_map[action]()

        if result.get("success"):
            data = result.get("data", {})
            return {
                "status": "success",
                "message": result.get("message", "OK"),
                "url": data.get("url", ""),
                "title": data.get("title", ""),
                "elements": data.get("elements", ""),
                "pageInfo": data.get("pageInfo", {}),
            }
        else:
            return {"status": "error", "message": result.get("message", "Unknown error")}

    except Exception as e:
        logger.error(f"browser_interact failed: {e}")
        return {"status": "error", "message": str(e)}


# ============== Tool Schemas ==============

TOOL_SCHEMAS = {
    "browser_navigate": {
        "name": "browser_navigate",
        "description": "启动浏览器并导航到 URL，自动返回页面结构化状态（编号的交互元素列表）。LLM 根据返回的元素编号决策下一步操作。**使用场景**：用户要求'打开网页'、'访问网站'、'浏览页面'时使用。**返回**：url、title、elements（编号的交互元素，如 [0]<button>登录 />）、pageInfo。",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标 URL"},
                "wait_until": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                    "default": "domcontentloaded"
                }
            },
            "required": ["url"]
        }
    },
    "browser_interact": {
        "name": "browser_interact",
        "description": "与页面交互：点击元素、输入文本、选择下拉选项、滚动页面、获取当前页面状态。**使用场景**：在 browser_navigate 之后，根据返回的元素编号执行操作。**参数**：action（click/input/select/scroll/get_state）、index（元素编号，从 elements 列表中获取）、text（输入文本）、option（选择选项）、direction（滚动方向）、amount（滚动量）。**返回**：操作结果 + 更新后的页面状态。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "input", "select", "scroll", "get_state"],
                    "description": "操作类型"
                },
                "index": {"type": "integer", "description": "元素编号（从 elements 列表中获取）"},
                "text": {"type": "string", "description": "输入文本（action=input 时使用）"},
                "option": {"type": "string", "description": "选择选项（action=select 时使用）"},
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "default": "down",
                    "description": "滚动方向（action=scroll 时使用）"
                },
                "amount": {
                    "type": "number",
                    "default": 1.0,
                    "description": "滚动量（页数，action=scroll 时使用）"
                }
            },
            "required": ["action"]
        }
    }
}


def get_tool_schemas() -> list[dict]:
    """Return tool schemas for ToolRegistry"""
    return list(TOOL_SCHEMAS.values())


def main():
    """Entry point for standalone testing"""
    print("Niu Browser Server - Chrome Extension Architecture")
    print(f"Available tools: {len(TOOL_SCHEMAS)}")
    for name in TOOL_SCHEMAS:
        print(f"  - {name}")
