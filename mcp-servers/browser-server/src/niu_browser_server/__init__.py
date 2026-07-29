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

import re
import time
from loguru import logger

from .ws_bridge import WSBridge
from .launcher import launch_browser


# Global state
_browser_proc = None  # Track launched browser process
_ws_bridge: WSBridge | None = None

# Start WSBridge immediately on module load so Extension can connect
# (Extension hub.js auto-reconnects every 3s)
_ws_bridge = WSBridge()
_ws_bridge.start()


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
    2. If not connected → wait for Extension to reconnect (hub.js auto-reconnects every 3s)
    3. If still not connected after wait → try launching browser
    4. Wait for Extension to connect

    Raises:
        RuntimeError: If Extension doesn't connect after reasonable wait
    """
    global _browser_proc

    bridge = _get_bridge()

    if bridge.connected:
        return bridge

    # Extension not connected - wait for auto-reconnect first (hub.js retries every 3s)
    logger.info("Extension not connected, waiting for auto-reconnect...")
    for i in range(10):  # 5 seconds
        if bridge.connected:
            logger.info(f"Extension reconnected after {(i+1)*0.5:.1f}s")
            return bridge
        time.sleep(0.5)

    # Still not connected - try launching browser
    logger.info("Extension not reconnecting, launching browser...")

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
    for i in range(60):  # 30 seconds
        if bridge.connected:
            logger.info(f"Extension connected after {(i+1)*0.5:.1f}s")
            return bridge
        time.sleep(0.5)

    # Still not connected
    raise RuntimeError(
        "Extension not connected after 30 seconds.\n"
        "The browser may be using a locked profile (already running).\n"
        "Please manually load the extension in your browser:\n"
        "1. Open edge://extensions/ (or chrome://extensions/)\n"
        "2. Enable 'Developer mode'\n"
        "3. Click 'Load unpacked' → select extensions/niu-browser-ext/"
    )


def _normalize_url(url: str) -> str:
    """Ensure URL has a valid scheme.

    Without a scheme, Chrome resolves the URL as a relative path against the
    current page. If the active tab is an extension page (hub.html), this
    produces extension://<id>/url=... instead of navigating to the website.
    """
    url = url.strip()
    if url.startswith(("http://", "https://", "ftp://", "file://")):
        return url
    if url.lower().startswith("url="):
        url = url[4:].strip()
    if re.match(r"^[\w.-]+\.\w{2,}", url):
        return "https://" + url
    return url


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
        url = _normalize_url(url)
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
                "tabSummary": data.get("tabSummary", ""),
                "currentTabId": data.get("currentTabId"),
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
                "tabSummary": data.get("tabSummary", ""),
                "currentTabId": data.get("currentTabId"),
            }
        else:
            return {"status": "error", "message": result.get("message", "Unknown error")}

    except Exception as e:
        logger.error(f"browser_interact failed: {e}")
        return {"status": "error", "message": str(e)}


def browser_new_tab(
    url: str,
) -> dict:
    """
    Open a new browser tab and navigate to URL.

    Args:
        url: URL to open in new tab (required, cannot be empty)

    Returns:
        Dict with new tab page state
    """
    if not url or not url.strip():
        return {"status": "error", "message": "url is required for browser_new_tab. Cannot open a blank tab (content script cannot inject into about:blank)."}

    try:
        url = _normalize_url(url)
        bridge = _ensure_connection()

        result = bridge.send_command("create_tab", url=url, timeout=60)

        if result.get("success"):
            data = result.get("data") or {}
            return {
                "status": "success",
                "url": data.get("url", url),
                "title": data.get("title", ""),
                "elements": data.get("elements", ""),
                "pageInfo": data.get("pageInfo", {}),
                "tabSummary": data.get("tabSummary", ""),
                "currentTabId": data.get("currentTabId"),
            }
        else:
            return {
                "status": "error",
                "message": f"New tab failed: {result.get('message', 'Unknown error')}",
            }

    except Exception as e:
        logger.error(f"browser_new_tab failed: {e}")
        return {"status": "error", "message": str(e)}


def browser_switch_tab(
    tab_id: int,
) -> dict:
    """
    切换到指定标签页。

    Args:
        tab_id: 要切换到的标签页 ID（来自之前响应中的 tabSummary）

    Returns:
        切换后标签页的页面状态
    """
    try:
        bridge = _ensure_connection()
        result = bridge.send_command("switch_tab", tabId=tab_id, timeout=30)

        if result.get("success"):
            data = result.get("data") or {}
            return {
                "status": "success",
                "message": f"Switched to tab {tab_id}",
                "url": data.get("url", ""),
                "title": data.get("title", ""),
                "elements": data.get("elements", ""),
                "pageInfo": data.get("pageInfo", {}),
                "tabSummary": data.get("tabSummary", ""),
                "currentTabId": data.get("currentTabId"),
            }
        else:
            return {"status": "error", "message": result.get("message", "Unknown error")}

    except Exception as e:
        logger.error(f"browser_switch_tab failed: {e}")
        return {"status": "error", "message": str(e)}


def browser_close_tab(
    tab_id: int,
) -> dict:
    """
    关闭指定标签页。不能关闭初始标签页。

    Args:
        tab_id: 要关闭的标签页 ID（来自之前响应中的 tabSummary）

    Returns:
        关闭结果和更新后的标签页摘要
    """
    try:
        bridge = _ensure_connection()
        result = bridge.send_command("close_tab", tabId=tab_id, timeout=30)

        if result.get("success"):
            data = result.get("data") or {}
            return {
                "status": "success",
                "message": f"Closed tab {tab_id}",
                "tabSummary": data.get("tabSummary", ""),
                "currentTabId": data.get("currentTabId"),
            }
        else:
            return {"status": "error", "message": result.get("message", "Unknown error")}

    except Exception as e:
        logger.error(f"browser_close_tab failed: {e}")
        return {"status": "error", "message": str(e)}


# ============== Tool Schemas ==============

TOOL_SCHEMAS = {
    "browser_navigate": {
        "name": "browser_navigate",
        "description": "启动浏览器并导航到 URL，自动返回页面结构化状态和标签页列表。LLM 根据返回的元素编号决策下一步操作，根据 tabSummary 管理多个标签页。**使用场景**：用户要求'打开网页'、'访问网站'、'浏览页面'时使用。**返回**：url、title、elements（编号的交互元素）、tabSummary（标签页列表）、currentTabId。",
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
        "description": "与页面元素交互（按索引）：点击、输入文本、选择下拉项、滚动、获取当前状态。每次操作返回更新后的页面状态（含重新编号的元素和标签页摘要）。操作是串行的——始终使用上一次结果的最新索引。",
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
    },
    "browser_new_tab": {
        "name": "browser_new_tab",
        "description": "Open URL in a new browser tab. Use for browsing multiple pages simultaneously or comparing different websites. URL parameter is required - cannot open blank tab.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要在新标签页中打开的 URL（必填）"}
            },
            "required": ["url"]
        }
    },
    "browser_switch_tab": {
        "name": "browser_switch_tab",
        "description": "切换到指定标签页。当需要操作非当前标签页时使用。tabId 来自之前响应中的 tabSummary 表格。",
        "input_schema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "要切换到的标签页 ID（来自 tabSummary）"}
            },
            "required": ["tab_id"]
        }
    },
    "browser_close_tab": {
        "name": "browser_close_tab",
        "description": "关闭指定标签页。不能关闭初始标签页。关闭后自动切换到最后一个剩余标签页。",
        "input_schema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "要关闭的标签页 ID（来自 tabSummary）"}
            },
            "required": ["tab_id"]
        }
    }
}


def get_tool_schemas() -> list[dict]:
    """返回所有工具的 schema 列表（用于 MCP Loader 注册）"""
    return list(TOOL_SCHEMAS.values())
