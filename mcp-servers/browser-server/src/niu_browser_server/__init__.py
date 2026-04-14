"""
Niu Browser MCP Server

Controls browser via Chrome Extension, replacing Playwright architecture.

Architecture:
- Check if Extension is connected
- If connected: operate directly (user's browser is running with extension)
- If not connected: launch user's default browser with extension loaded
- Extension's background.js auto-opens hub tab on browser startup
- Hub connects to Python backend via WebSocket

Advantages:
- Shares user's browser session (cookies, logins)
- Extension persists across tabs, handles navigation automatically
- Simulates real mouse events, harder to detect
"""

import time
from loguru import logger

from .ws_bridge import WSBridge
from .launcher import launch_browser


# Global state
_browser_proc = None  # Track launched browser process
_ws_bridge: WSBridge | None = None


def _get_bridge() -> WSBridge:
    """Get or create WSBridge singleton."""
    global _ws_bridge
    if _ws_bridge is None:
        _ws_bridge = WSBridge()
        _ws_bridge.start()
    return _ws_bridge


def _ensure_connection() -> WSBridge:
    """
    Ensure Extension is connected.

    Flow:
    1. If Extension connected → return immediately
    2. If not connected → launch user's default browser with extension
    3. Wait for Extension to connect

    Raises:
        RuntimeError: If Extension doesn't connect after reasonable wait
    """
    global _browser_proc

    bridge = _get_bridge()

    if bridge.connected:
        return bridge

    # Extension not connected - try launching browser
    logger.info("Extension not connected, launching browser...")

    try:
        _browser_proc = launch_browser()
    except Exception as e:
        logger.error(f"Failed to launch browser: {e}")
        raise RuntimeError(
            f"Failed to launch browser: {e}\n"
            "Please manually load the extension:\n"
            "1. Open edge://extensions/ (or chrome://extensions/)\n"
            "2. Enable 'Developer mode'\n"
            "3. Click 'Load unpacked' → select extensions/niu-browser-ext/"
        )

    # Wait for Extension to connect
    logger.info("Waiting for Extension to connect...")
    for i in range(30):  # 15 seconds
        if bridge.connected:
            logger.info(f"Extension connected after {(i+1)*0.5:.1f}s")
            return bridge
        time.sleep(0.5)

    # Still not connected
    raise RuntimeError(
        "Extension not connected after 15 seconds.\n"
        "The browser may be using a locked profile (already running).\n"
        "Please manually load the extension in your browser:\n"
        "1. Open edge://extensions/ (or chrome://extensions/)\n"
        "2. Enable 'Developer mode'\n"
        "3. Click 'Load unpacked' → select extensions/niu-browser-ext/"
    )


def browser_navigate(
    url: str,
    wait_until: str = "domcontentloaded"
) -> dict:
    """
    Navigate browser to URL. Uses Extension (connects or launches browser if needed).

    Args:
        url: Target URL
        wait_until: Wait strategy (kept for compatibility, handled by Extension)

    Returns:
        Dict with page state: url, title, elements (indexed interactive elements), pageInfo
    """
    try:
        bridge = _ensure_connection()

        # Send navigate command (hub.js handles navigation + returns page state)
        result = bridge.send_command("navigate", url=url, timeout=60)

        if result.get("success"):
            data = result.get("data") or {}
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
        bridge = _ensure_connection()

        action_map = {
            "click": lambda: bridge.send_command("click", index=index, timeout=60),
            "input": lambda: bridge.send_command("input_text", index=index, text=text, timeout=60),
            "select": lambda: bridge.send_command("select_option", index=index, option=option, timeout=60),
            "scroll": lambda: bridge.send_command("scroll", direction=direction, amount=amount, timeout=60),
            "get_state": lambda: bridge.send_command("get_state", timeout=60),
        }

        if action not in action_map:
            return {"status": "error", "message": f"Unknown action: {action}. Supported: {list(action_map.keys())}"}

        result = action_map[action]()

        if result.get("success"):
            data = result.get("data") or {}
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
        "description": "操作浏览器页面元素（点击、输入、选择、滚动）。通过元素编号操作，每次操作后返回新的页面状态。**核心**：操作串行，每次用最新返回的编号。**返回**：url、title、elements（更新后的编号元素）、pageInfo。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "input", "select", "scroll", "get_state"],
                    "description": "操作类型"
                },
                "index": {"type": "integer", "description": "元素编号（从 browser_navigate 返回的 elements 中获取）"},
                "text": {"type": "string", "description": "输入文本（action=input 时使用）"},
                "option": {"type": "string", "description": "下拉选项（action=select 时使用）"},
                "direction": {"type": "string", "enum": ["up", "down"], "description": "滚动方向"},
                "amount": {"type": "number", "description": "滚动页数（支持小数，如 0.5=半页）"}
            },
            "required": ["action"]
        }
    }
}
