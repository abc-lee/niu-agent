"""
Niu Browser MCP Server

Provides browser navigation tool using Playwright.
Other browser operations can be done via code_run with BrowserManager.
"""

import threading
import time
from typing import Literal, Optional
from loguru import logger
from playwright.sync_api import Browser, Page, Playwright, sync_playwright

# ============== Browser Manager (Singleton) ==============

class BrowserManager:
    """
    Singleton browser instance manager with lifecycle management.

    Features:
    - Singleton browser instance
    - Threading.Lock with timeout for concurrency protection
    - Idle timeout (5 minutes) with background thread
    - Health check + auto-restart (max 3 retries)

    Usage in code_run:
        from niu_browser_server import BrowserManager
        page, error = BrowserManager().get_page()
        if page:
            page.click('button')
            page.fill('input', 'text')
    """

    _instance: Optional['BrowserManager'] = None
    _lock = threading.Lock()
    _lock_timeout = 30  # seconds

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._playwright: Optional[Playwright] = None
        self._last_used: float = 0
        self._idle_timeout = 300  # 5 minutes
        self._error_count = 0
        self._max_retries = 3
        self._initialized = True

        # Start idle timeout monitor thread
        self._monitor_thread = threading.Thread(target=self._idle_monitor, daemon=True)
        self._monitor_thread.start()

        logger.info("BrowserManager initialized")

    def _idle_monitor(self):
        """Background thread to monitor idle timeout"""
        while True:
            time.sleep(60)  # Check every minute
            if self._browser and self._last_used > 0:
                idle_time = time.time() - self._last_used
                if idle_time > self._idle_timeout:
                    logger.info(f"Browser idle for {idle_time:.0f}s, closing...")
                    self._close_browser()

    def _start_browser(self) -> bool:
        """Start browser instance"""
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self._page = self._browser.new_page()
            self._last_used = time.time()
            self._error_count = 0
            logger.info("Browser started successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to start browser: {e}")
            return False

    def _close_browser(self):
        """Close browser instance"""
        try:
            if self._page:
                self._page.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.error(f"Error closing browser: {e}")
        finally:
            self._page = None
            self._browser = None
            self._playwright = None
            self._last_used = 0
            logger.info("Browser closed")

    def _health_check(self) -> bool:
        """Check if browser is healthy"""
        if not self._browser or not self._page:
            return False
        try:
            # Try to access browser context
            _ = self._browser.contexts
            return True
        except:
            return False

    def _restart_browser(self) -> bool:
        """Restart browser (with retry limit)"""
        self._error_count += 1
        if self._error_count > self._max_retries:
            logger.error(f"Browser restart failed after {self._max_retries} attempts")
            return False

        logger.info(f"Restarting browser (attempt {self._error_count}/{self._max_retries})")
        self._close_browser()
        return self._start_browser()

    def get_page(self) -> tuple[Optional[Page], Optional[str]]:
        """
        Get or create browser page.

        Returns:
            tuple: (Page instance, error message if failed)
        """
        if not self._lock.acquire(timeout=self._lock_timeout):
            return None, f"Browser busy (lock timeout after {self._lock_timeout}s)"

        try:
            # Health check
            if self._browser and not self._health_check():
                logger.warning("Browser unhealthy, restarting...")
                if not self._restart_browser():
                    return None, "Browser restart failed"

            # Start if not running
            if not self._browser:
                if not self._start_browser():
                    return None, "Failed to start browser"

            self._last_used = time.time()
            return self._page, None

        except Exception as e:
            logger.error(f"Error getting browser page: {e}")
            return None, str(e)
        finally:
            self._lock.release()

    def reset_error_count(self):
        """Reset error count after successful operation"""
        self._error_count = 0


# Global instance
_browser_manager = BrowserManager()


# ============== Tool Functions ==============

def browser_navigate(
    url: str,
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = "domcontentloaded"
) -> dict:
    """
    Navigate to URL.

    Args:
        url: Target URL
        wait_until: Wait strategy

    Returns:
        {"status": "success/error", "message": ...}
    """
    page, error = _browser_manager.get_page()
    if error:
        return {"status": "error", "message": error}

    if not page:
        return {"status": "error", "message": "Failed to get browser page"}

    try:
        page.goto(url, wait_until=wait_until, timeout=30000)
        _browser_manager.reset_error_count()
        return {"status": "success", "message": f"Navigated to {url}"}
    except Exception as e:
        logger.error(f"Navigate failed: {e}")
        return {"status": "error", "message": f"Navigation failed: {str(e)}"}


# ============== Tool Schemas ==============

TOOL_SCHEMAS = {
    "browser_navigate": {
        "name": "browser_navigate",
        "description": "启动浏览器并导航到指定 URL。**使用场景**：用户要求'打开网页'、'访问网站'、'导航到某个URL'、'浏览页面'时使用此工具。**参数**：url (目标 URL，必需)，wait_until (等待策略，可选)。**返回**：导航结果。**注意**：此工具仅负责导航，如需点击、填充、截图等操作，使用 code_run 调用 BrowserManager。",
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
    }
}


def get_tool_schemas() -> list[dict]:
    """Return tool schemas for ToolRegistry"""
    return list(TOOL_SCHEMAS.values())


def main():
    """Entry point for standalone testing"""
    print("Niu Browser Server - Use via MCP loader")
    print(f"Available tools: {len(TOOL_SCHEMAS)}")
    for name in TOOL_SCHEMAS:
        print(f"  - {name}")
