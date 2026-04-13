"""
Test script for Chrome Extension + WebSocket Bridge integration.

Usage:
  python scripts/test_browser_ext.py [--chrome|--edge]

Defaults to Chrome if available, otherwise Edge.
"""

import sys
import time
import json
import re
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-servers" / "browser-server" / "src"))

from loguru import logger

logger.remove()
logger.add(sys.stderr, format="<level>{time:HH:mm:ss} | {level:<7} | {message}</level>", level="DEBUG")


def find_chrome():
    if sys.platform != "win32":
        return None
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                path, _ = winreg.QueryValueEx(key, None)
                if path and Path(path).is_file():
                    return path
        except (FileNotFoundError, OSError):
            pass
    return None


def find_edge():
    if sys.platform != "win32":
        return None
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
                path, _ = winreg.QueryValueEx(key, None)
                if path and Path(path).is_file():
                    return path
        except (FileNotFoundError, OSError):
            pass
    return None


def test_browser_extension():
    from niu_browser_server.ws_bridge import WSBridge
    from niu_browser_server.launcher import launch_browser, EXTENSION_DIR

    # Choose browser
    use_chrome = "--chrome" in sys.argv
    use_edge = "--edge" in sys.argv

    browser_exe = None
    if use_chrome:
        browser_exe = find_chrome()
    elif use_edge:
        browser_exe = find_edge()
    else:
        # Default: prefer Chrome
        browser_exe = find_chrome() or find_edge()

    if not browser_exe:
        logger.error("No browser found! Install Chrome or Edge.")
        return False

    logger.info(f"Using browser: {browser_exe}")

    # Step 1: Verify extension files
    logger.info("=" * 60)
    logger.info("Step 1: Verify extension files")
    logger.info("=" * 60)

    required_files = ["manifest.json", "hub.html", "hub.js", "background.js", "content.js", "dom_tree.js"]
    all_exist = True
    for name in required_files:
        f = EXTENSION_DIR / name
        exists = f.is_file()
        logger.info(f"  {'OK' if exists else 'MISSING'}: {name}")
        if not exists:
            all_exist = False

    if not all_exist:
        logger.error("Extension files missing!")
        return False

    # Step 2: Start WSBridge
    logger.info("")
    logger.info("=" * 60)
    logger.info("Step 2: Start WSBridge")
    logger.info("=" * 60)

    WSBridge._instance = None
    bridge = WSBridge()
    bridge.start()
    time.sleep(1)

    # Step 3: Launch browser
    logger.info("")
    logger.info("=" * 60)
    logger.info("Step 3: Launch browser with extension")
    logger.info("=" * 60)

    proc = launch_browser(browser_exe=browser_exe)
    logger.info(f"Browser PID: {proc.pid}")

    # Step 4: Wait for extension connection
    logger.info("")
    logger.info("=" * 60)
    logger.info("Step 4: Wait for extension hub to connect")
    logger.info("=" * 60)

    connected = False
    for i in range(40):
        if bridge.connected:
            connected = True
            logger.info(f"Connected after {(i+1)*0.5:.1f}s")
            break
        if i % 4 == 0:
            logger.info(f"  Waiting... {(i+1)*0.5:.1f}s")
        time.sleep(0.5)

    if not connected:
        logger.error("Extension NOT connected after 20s")
        return False

    time.sleep(1)

    # Step 5: Navigate to Baidu
    logger.info("")
    logger.info("=" * 60)
    logger.info("Step 5: Navigate to https://www.baidu.com")
    logger.info("=" * 60)

    result = bridge.send_command("navigate", url="https://www.baidu.com", timeout=60)
    logger.info(f"Navigate: success={result.get('success')}")
    if not result.get("success"):
        logger.error(f"  Error: {result.get('message')}")
        return False

    data = result.get("data", {})
    logger.info(f"  URL: {data.get('url')}")
    logger.info(f"  Title: {data.get('title')}")
    elements = data.get("elements", "")
    logger.info(f"  Elements ({len(elements)} chars): {elements[:300]}...")

    # Step 6: Input text into search box
    logger.info("")
    logger.info("=" * 60)
    logger.info("Step 6: Input 'Claude AI' into search box")
    logger.info("=" * 60)

    # Find textarea/input index
    search_index = None
    for line in elements.split("\n"):
        if ("textarea" in line.lower() or "input" in line.lower()) and ("chat" in line.lower() or "kw" in line.lower()):
            match = re.search(r'\[(\d+)\]', line)
            if match:
                search_index = int(match.group(1))
                logger.info(f"  Found search box at index {search_index}")
                break

    if search_index is None:
        logger.warning("  Search box not found, trying index 12")
        search_index = 12

    result = bridge.send_command("input_text", index=search_index, text="Claude AI", timeout=15)
    logger.info(f"Input: success={result.get('success')}, msg={result.get('message', '')}")

    if not result.get("success"):
        logger.error(f"  Input failed: {result.get('message')}")
    else:
        logger.info("  Input succeeded")

    # Step 7: Click search button
    logger.info("")
    logger.info("=" * 60)
    logger.info("Step 7: Click search button")
    logger.info("=" * 60)

    # Find search button
    btn_index = None
    for line in elements.split("\n"):
        if "button" in line.lower() and ("su" in line.lower() or "百度" in line or "搜索" in line):
            match = re.search(r'\[(\d+)\]', line)
            if match:
                btn_index = int(match.group(1))
                logger.info(f"  Found button at index {btn_index}")
                break

    if btn_index is None:
        for line in elements.split("\n"):
            if "su" in line or "chat-submit" in line:
                match = re.search(r'\[(\d+)\]', line)
                if match:
                    btn_index = int(match.group(1))
                    logger.info(f"  Found button at index {btn_index}")
                    break

    if btn_index is not None:
        result = bridge.send_command("click", index=btn_index, timeout=60)
        logger.info(f"Click: success={result.get('success')}")
        if result.get("success"):
            data = result.get("data", {})
            logger.info(f"  URL: {data.get('url', 'N/A')}")
            logger.info(f"  Title: {data.get('title', 'N/A')}")
            new_elements = data.get("elements", "")
            if new_elements:
                logger.info(f"  Elements ({len(new_elements)} chars): {new_elements[:300]}...")
            else:
                logger.warning("  Elements: EMPTY after click")
        else:
            logger.error(f"  Click error: {result.get('message')}")
    else:
        logger.warning("  Button not found, trying direct URL navigation")
        result = bridge.send_command("navigate", url="https://www.baidu.com/s?wd=Claude+AI", timeout=60)
        logger.info(f"  Navigate result: success={result.get('success')}")

    # Step 8: Get state on the results page
    logger.info("")
    logger.info("=" * 60)
    logger.info("Step 8: Get state on search results page")
    logger.info("=" * 60)

    time.sleep(1)
    result = bridge.send_command("get_state", timeout=15)
    logger.info(f"Get state: success={result.get('success')}")
    if result.get("success"):
        data = result.get("data", {})
        logger.info(f"  URL: {data.get('url')}")
        logger.info(f"  Title: {data.get('title')}")
        elements = data.get("elements", "")
        if elements:
            logger.info(f"  Elements ({len(elements)} chars): {elements[:400]}...")
        else:
            logger.warning("  Elements: EMPTY")
        page_info = data.get("pageInfo", {})
        logger.info(f"  Viewport: {page_info.get('viewportWidth')}x{page_info.get('viewportHeight')}")
    else:
        logger.error(f"  Error: {result.get('message')}")

    # Step 9: Scroll down
    logger.info("")
    logger.info("=" * 60)
    logger.info("Step 9: Scroll down")
    logger.info("=" * 60)

    result = bridge.send_command("scroll", direction="down", amount=1, timeout=15)
    logger.info(f"Scroll: success={result.get('success')}")
    if result.get("success"):
        data = result.get("data", {})
        page_info = data.get("pageInfo", {})
        logger.info(f"  ScrollY: {page_info.get('scrollY', 'N/A')}")
        logger.info(f"  Pixels below: {page_info.get('pixelsBelow', 'N/A')}")

    # Step 10: Click a search result link
    logger.info("")
    logger.info("=" * 60)
    logger.info("Step 10: Click first search result link")
    logger.info("=" * 60)

    # Get fresh state first
    state = bridge.send_command("get_state", timeout=15)
    if state.get("success"):
        elems = state.get("data", {}).get("elements", "")
        # Find first link that looks like a search result
        link_index = None
        for line in elems.split("\n"):
            if "<a" in line and "target=_blank" in line:
                match = re.search(r'\[(\d+)\]', line)
                if match:
                    link_index = int(match.group(1))
                    logger.info(f"  Found result link at index {link_index}: {line.strip()[:80]}")
                    break

        if link_index is not None:
            result = bridge.send_command("click", index=link_index, timeout=60)
            logger.info(f"Click link: success={result.get('success')}")
            if result.get("success"):
                data = result.get("data", {})
                logger.info(f"  URL: {data.get('url', 'N/A')}")
                logger.info(f"  Title: {data.get('title', 'N/A')}")
            else:
                logger.error(f"  Click error: {result.get('message')}")
        else:
            logger.warning("  No search result link found")

    # Summary
    logger.info("")
    logger.info("=" * 60)
    logger.info("TEST COMPLETE - Browser still running, Ctrl+C to exit")
    logger.info("=" * 60)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Test ended")


if __name__ == "__main__":
    test_browser_extension()
