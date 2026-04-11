"""
End-to-End Test for Page Agent MCP Integration
Tests the complete flow: Python -> HTTP API -> HubBridge -> Chrome Extension -> Browser
"""
import json
import time
import sys

# Add path to niu_page_agent module
sys.path.insert(0, 'E:\\tools\\ai-bot\\mcp-servers\\page-agent-mcp\\src')

from niu_page_agent import get_status, execute_task, stop_task

def test_extension_connection():
    """Step 1: Check if Chrome extension is connected"""
    print("=" * 60)
    print("Step 1: Check Extension Connection")
    print("=" * 60)

    status = json.loads(get_status())
    print(f"Hub status: {status}")

    if status['connected']:
        print("[OK] Chrome extension is connected!")
        return True
    else:
        print("[FAIL] Chrome extension NOT connected")
        print("\nTo connect the extension:")
        print("1. Install Page Agent Chrome extension from:")
        print("   https://chromewebstore.google.com/detail/akldabonmimlicnjlflnapfeklbfemhj")
        print("2. Open any webpage (e.g., https://www.baidu.com)")
        print("3. Extension will auto-connect to ws://localhost:38401")
        return False


def test_browser_navigation():
    """Step 2: Test browser navigation"""
    print("\n" + "=" * 60)
    print("Step 2: Test Browser Navigation")
    print("=" * 60)

    task = "Open https://www.baidu.com in the current tab"
    print(f"Task: {task}")
    print("Executing... (this should open Baidu in your browser)")

    result = execute_task(task)
    print(f"Result: {result}")

    if "completed" in result.lower():
        print("[OK] Navigation successful!")
        return True
    else:
        print(f"[FAIL] Navigation failed: {result}")
        return False


def test_browser_interaction():
    """Step 3: Test browser interaction (search)"""
    print("\n" + "=" * 60)
    print("Step 3: Test Browser Interaction (Search)")
    print("=" * 60)

    # Wait a bit for the page to load
    print("Waiting 2 seconds for page to load...")
    time.sleep(2)

    task = """In the current page (Baidu), find the search input box and type "Python tutorial", then click the search button"""
    print(f"Task: {task}")
    print("Executing... (you should see typing and clicking in the browser)")

    result = execute_task(task)
    print(f"Result: {result}")

    if "completed" in result.lower():
        print("[OK] Interaction successful!")
        return True
    else:
        print(f"[FAIL] Interaction failed: {result}")
        return False


def main():
    print("Page Agent MCP - End-to-End Test")
    print("=" * 60)

    # Step 1: Check connection
    if not test_extension_connection():
        print("\n[ABORT] Extension not connected. Please install and connect the extension first.")
        return

    # Step 2: Test navigation
    if not test_browser_navigation():
        print("\n[ABORT] Navigation test failed.")
        return

    # Step 3: Test interaction
    if not test_browser_interaction():
        print("\n[FAIL] Interaction test failed.")
        return

    print("\n" + "=" * 60)
    print("[SUCCESS] All tests passed!")
    print("You should have seen:")
    print("1. Baidu homepage opened")
    print("2. 'Python tutorial' typed in search box")
    print("3. Search button clicked")
    print("=" * 60)


if __name__ == "__main__":
    main()
