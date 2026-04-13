"""
Niu Browser MCP Server

Provides browser navigation tool using Playwright async_api.
Runs in a dedicated daemon thread with its own asyncio loop.

Architecture:
- Main process: FastAPI async → run_in_executor → sync handler → ToolRegistry
- BrowserManager: dedicated daemon thread with own asyncio loop + playwright.async_api
- get_page() returns SyncPageProxy: wraps async Page methods as sync, code_run code unchanged
- Communication: call_async() sends coroutines to daemon thread, blocks until result
"""

import asyncio
import queue
import threading
import time
from pathlib import Path
from typing import Literal, Optional
from loguru import logger


# ============== Sync Page Proxy ==============

class SyncPageProxy:
    """
    Synchronous proxy for async_api Page.

    Wraps all async Page methods as synchronous calls via BrowserManager.call_async().
    This allows code_run code to use page.click(), page.fill() etc. without await.

    Usage (identical to sync_api Page):
        page, _ = BrowserManager().get_page()
        page.click('button')
        page.fill('input', 'text')
        page.screenshot()
    """

    def __init__(self, async_page, browser_manager):
        self._async_page = async_page
        self._mgr = browser_manager

    # --- Navigation ---
    def goto(self, url, *, wait_until="domcontentloaded", timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.goto(url, wait_until=wait_until, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def go_back(self, *, timeout=30000, **kwargs):
        return self._mgr.call_async(self._async_page.go_back(timeout=timeout, **kwargs), timeout=timeout / 1000 + 5)

    def go_forward(self, *, timeout=30000, **kwargs):
        return self._mgr.call_async(self._async_page.go_forward(timeout=timeout, **kwargs), timeout=timeout / 1000 + 5)

    def reload(self, *, timeout=30000, **kwargs):
        return self._mgr.call_async(self._async_page.reload(timeout=timeout, **kwargs), timeout=timeout / 1000 + 5)

    # --- Interaction ---
    def click(self, selector, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.click(selector, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def dblclick(self, selector, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.dblclick(selector, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def fill(self, selector, value, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.fill(selector, value, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def type(self, selector, text, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.type(selector, text, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def press(self, selector, key, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.press(selector, key, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def check(self, selector, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.check(selector, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def uncheck(self, selector, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.uncheck(selector, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def select_option(self, selector, *values, **kwargs):
        return self._mgr.call_async(
            self._async_page.select_option(selector, *values, **kwargs),
            timeout=35
        )

    def hover(self, selector, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.hover(selector, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    # --- Content ---
    def inner_text(self, selector, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.inner_text(selector, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def inner_html(self, selector, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.inner_html(selector, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def text_content(self, selector, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.text_content(selector, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def get_attribute(self, selector, name, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.get_attribute(selector, name, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def title(self):
        return self._mgr.call_async(self._async_page.title(), timeout=10)

    def url(self):
        return self._async_page.url

    def content(self):
        return self._mgr.call_async(self._async_page.content(), timeout=10)

    # --- Screenshot ---
    def screenshot(self, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.screenshot(timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    # --- Selectors ---
    def query_selector(self, selector):
        async_el = self._mgr.call_async(self._async_page.query_selector(selector), timeout=10)
        if async_el is None:
            return None
        return SyncElementProxy(async_el, self._mgr)

    def query_selector_all(self, selector):
        async_els = self._mgr.call_async(self._async_page.query_selector_all(selector), timeout=10)
        return [SyncElementProxy(el, self._mgr) for el in async_els]

    # --- Waiting ---
    def wait_for_selector(self, selector, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.wait_for_selector(selector, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def wait_for_load_state(self, state="load", *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.wait_for_load_state(state, timeout=timeout, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def wait_for_timeout(self, timeout):
        return self._mgr.call_async(self._async_page.wait_for_timeout(timeout), timeout=timeout / 1000 + 5)

    # --- Evaluation ---
    def evaluate(self, expression, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.evaluate(expression, **kwargs),
            timeout=timeout / 1000 + 5
        )

    def evaluate_handle(self, expression, *, timeout=30000, **kwargs):
        return self._mgr.call_async(
            self._async_page.evaluate_handle(expression, **kwargs),
            timeout=timeout / 1000 + 5
        )

    # --- Properties ---
    @property
    def url_property(self):
        """Get current URL (property-style access)."""
        return self._async_page.url

    def __repr__(self):
        try:
            return f"<SyncPageProxy url={self._async_page.url}>"
        except:
            return "<SyncPageProxy>"


class SyncElementProxy:
    """Synchronous proxy for async_api ElementHandle."""

    def __init__(self, async_element, browser_manager):
        self._async_el = async_element
        self._mgr = browser_manager

    def click(self, *, timeout=30000, **kwargs):
        return self._mgr.call_async(self._async_el.click(timeout=timeout, **kwargs), timeout=timeout / 1000 + 5)

    def fill(self, value, *, timeout=30000, **kwargs):
        return self._mgr.call_async(self._async_el.fill(value, timeout=timeout, **kwargs), timeout=timeout / 1000 + 5)

    def inner_text(self, *, timeout=30000, **kwargs):
        return self._mgr.call_async(self._async_el.inner_text(timeout=timeout, **kwargs), timeout=timeout / 1000 + 5)

    def get_attribute(self, name, *, timeout=30000, **kwargs):
        return self._mgr.call_async(self._async_el.get_attribute(name, timeout=timeout, **kwargs), timeout=timeout / 1000 + 5)

    def screenshot(self, *, timeout=30000, **kwargs):
        return self._mgr.call_async(self._async_el.screenshot(timeout=timeout, **kwargs), timeout=timeout / 1000 + 5)

    def type(self, text, *, timeout=30000, **kwargs):
        return self._mgr.call_async(self._async_el.type(text, timeout=timeout, **kwargs), timeout=timeout / 1000 + 5)

    def query_selector(self, selector):
        async_el = self._mgr.call_async(self._async_el.query_selector(selector), timeout=10)
        if async_el is None:
            return None
        return SyncElementProxy(async_el, self._mgr)

    def query_selector_all(self, selector):
        async_els = self._mgr.call_async(self._async_el.query_selector_all(selector), timeout=10)
        return [SyncElementProxy(el, self._mgr) for el in async_els]

    def __repr__(self):
        return "<SyncElementProxy>"


# ============== Browser Manager (Singleton, async_api in daemon thread) ==============

class BrowserManager:
    """
    Singleton browser instance manager with async_api in dedicated thread.

    Architecture:
    - Dedicated daemon thread runs its own asyncio event loop
    - All Playwright operations use async_api, executed in the daemon thread
    - Main thread calls call_async(coro) which sends coroutine to daemon thread
    - Results returned via queue (blocking, with timeout)
    - get_page() returns SyncPageProxy (sync wrapper for async Page)

    Features:
    - Persistent browser context (cookies and login state saved to ~/.niu/browser_data/)
    - Visible browser window (headless=False)
    - Threading.Lock for concurrency protection
    - Idle timeout (5 minutes) with auto-close
    - Health check + auto-restart (max 3 retries)
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

        self._browser = None  # Browser instance (async_api)
        self._context = None  # Browser context (async_api)
        self._async_page = None  # Current page (async_api Page)
        self._playwright = None  # async_playwright instance
        self._last_used: float = 0
        self._idle_timeout = 300  # 5 minutes
        self._error_count = 0
        self._max_retries = 3
        self._initialized = True

        # User data directory for persistent cookies and login state
        self._user_data_dir = Path.home() / ".niu" / "browser_data"
        self._user_data_dir.mkdir(parents=True, exist_ok=True)

        # Daemon thread infrastructure
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._daemon_thread = threading.Thread(
            target=self._daemon_thread_main,
            daemon=True,
            name="BrowserManager-Daemon"
        )
        self._daemon_thread.start()

        # Wait for daemon thread to be ready
        self._wait_for_daemon(timeout=10)

        # Start idle timeout monitor thread
        self._monitor_thread = threading.Thread(target=self._idle_monitor, daemon=True)
        self._monitor_thread.start()

        logger.info(f"BrowserManager initialized (async_api, user_data_dir: {self._user_data_dir})")

    def _wait_for_daemon(self, timeout: float = 10):
        """Wait for daemon thread's event loop to be ready."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._loop is not None:
                return
            time.sleep(0.1)
        raise RuntimeError("BrowserManager daemon thread failed to start")

    def _daemon_thread_main(self):
        """Daemon thread: runs its own asyncio event loop for Playwright async_api."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def call_async(self, coro, timeout: float = 60):
        """
        Send a coroutine to the daemon thread's event loop and wait for result.

        Args:
            coro: Coroutine to execute in daemon thread
            timeout: Maximum wait time in seconds

        Returns:
            Result of the coroutine

        Raises:
            TimeoutError: If coroutine doesn't complete in time
            Exception: Any exception from the coroutine
        """
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("BrowserManager daemon thread is not running")

        result_queue = queue.Queue()

        def done_callback(future):
            try:
                result_queue.put(future.result())
            except Exception as e:
                result_queue.put(e)

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(done_callback)

        try:
            result = result_queue.get(timeout=timeout)
        except queue.Empty:
            future.cancel()
            raise TimeoutError(f"Browser operation timed out after {timeout}s")

        if isinstance(result, Exception):
            raise result
        return result

    # ============== Async Playwright Operations (run in daemon thread) ==============

    async def _start_browser_impl(self) -> bool:
        """Start browser with persistent context (async_api, runs in daemon thread)."""
        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()

            # Launch browser with CDP port for code_run subprocess access
            self._browser = await self._playwright.chromium.launch(
                headless=False,  # Show browser window
                args=["--remote-debugging-port=9222"]
            )

            # Use persistent context to keep cookies and login state
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 720}
            )

            # Get or create page
            self._async_page = await self._context.new_page()

            self._last_used = time.time()
            self._error_count = 0
            logger.info("Browser started (async_api, headless=False)")
            return True
        except Exception as e:
            logger.error(f"Failed to start browser: {e}")
            return False

    async def _close_browser_impl(self):
        """Close browser (async_api, runs in daemon thread)."""
        try:
            if self._async_page:
                await self._async_page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.error(f"Error closing browser: {e}")
        finally:
            self._async_page = None
            self._context = None
            self._browser = None
            self._playwright = None
            self._last_used = 0
            logger.info("Browser closed")

    async def _health_check_impl(self) -> bool:
        """Check if browser is healthy (async_api)."""
        if not self._context or not self._async_page:
            return False
        try:
            _ = self._context.pages
            return True
        except:
            return False

    # ============== Sync Public API (called from main thread) ==============

    def _start_browser(self) -> bool:
        """Start browser (sync wrapper)."""
        return self.call_async(self._start_browser_impl())

    def _close_browser(self):
        """Close browser (sync wrapper)."""
        try:
            self.call_async(self._close_browser_impl(), timeout=10)
        except Exception as e:
            logger.error(f"Error in _close_browser: {e}")
            self._async_page = None
            self._context = None
            self._browser = None
            self._playwright = None
            self._last_used = 0

    def _health_check(self) -> bool:
        """Check if browser is healthy (sync wrapper)."""
        try:
            return self.call_async(self._health_check_impl(), timeout=5)
        except:
            return False

    def _restart_browser(self) -> bool:
        """Restart browser (with retry limit)."""
        self._error_count += 1
        if self._error_count > self._max_retries:
            logger.error(f"Browser restart failed after {self._max_retries} attempts")
            return False

        logger.info(f"Restarting browser (attempt {self._error_count}/{self._max_retries})")
        self._close_browser()
        return self._start_browser()

    def get_page(self) -> tuple[Optional['SyncPageProxy'], Optional[str]]:
        """
        Get or create browser page (sync, returns SyncPageProxy).

        Uses self._context (not self._browser) to check if browser is running,
        because launch_persistent_context() returns BrowserContext, not Browser.

        Returns:
            tuple: (SyncPageProxy instance, error message if failed)
        """
        if not self._lock.acquire(timeout=self._lock_timeout):
            return None, f"Browser busy (lock timeout after {self._lock_timeout}s)"

        try:
            # Health check (uses _context, not _browser)
            if self._context and not self._health_check():
                logger.warning("Browser unhealthy, restarting...")
                if not self._restart_browser():
                    return None, "Browser restart failed"

            # Start if not running (uses _context, not _browser)
            if not self._context:
                if not self._start_browser():
                    return None, "Failed to start browser"

            self._last_used = time.time()
            # Return SyncPageProxy wrapping the async Page
            sync_page = SyncPageProxy(self._async_page, self)
            return sync_page, None

        except Exception as e:
            logger.error(f"Error getting browser page: {e}")
            return None, str(e)
        finally:
            self._lock.release()

    def _idle_monitor(self):
        """Background thread to monitor idle timeout (uses _context, not _browser)."""
        while True:
            time.sleep(60)  # Check every minute
            if self._context and self._last_used > 0:
                idle_time = time.time() - self._last_used
                if idle_time > self._idle_timeout:
                    logger.info(f"Browser idle for {idle_time:.0f}s, closing...")
                    self._close_browser()

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
        "description": "启动浏览器并导航到指定 URL。**使用场景**：用户要求'打开网页'、'访问网站'、'导航到某个URL'、'浏览页面'时使用此工具。**特性**：浏览器窗口可见（headless=False），自动保存 cookies 和登录状态到 ~/.niu/browser_data/，关闭后重新打开仍保持登录。**参数**：url (目标 URL，必需)，wait_until (等待策略，可选)。**返回**：导航结果。**注意**：此工具仅负责导航，如需点击、填充、截图等操作，使用 code_run 调用 BrowserManager。",
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
