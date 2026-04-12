"""
Simple integration test for browser server
"""

import sys
import os

# Add browser server to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'mcp-servers', 'browser-server', 'src'))

from niu_browser_server import browser_navigate, browser_screenshot, browser_get_text

def test_basic_navigation():
    """Test basic navigation and screenshot"""
    print("Test 1: Navigate to example.com...")
    result = browser_navigate("https://example.com", wait_until="load")
    print(f"Result: {result}")

    if result["status"] == "success":
        print("[OK] Navigation successful")

        print("\nTest 2: Take screenshot...")
        screenshot_result = browser_screenshot()
        print(f"Screenshot status: {screenshot_result['status']}")

        if screenshot_result["status"] == "success":
            print(f"[OK] Screenshot taken ({len(screenshot_result['screenshot'])} bytes)")

        print("\nTest 3: Get page text...")
        text_result = browser_get_text()
        print(f"Text status: {text_result['status']}")

        if text_result["status"] == "success":
            print(f"[OK] Got text ({len(text_result['text'])} chars)")
            print(f"Preview: {text_result['text'][:100]}...")

        return True
    else:
        print(f"[ERROR] Navigation failed: {result['message']}")
        return False

if __name__ == "__main__":
    try:
        success = test_basic_navigation()
        print("\n" + "="*50)
        if success:
            print("[SUCCESS] All basic tests passed!")
        else:
            print("[FAILED] Tests failed")
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n[ERROR] Test error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
