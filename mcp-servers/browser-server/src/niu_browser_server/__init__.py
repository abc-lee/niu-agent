"""
Niu Browser MCP Server

Provides browser automation tools using Playwright.
Supports form filling, page navigation, and data extraction.
"""

import threading
import time
from typing import Any, Optional
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

def browser_navigate(url: str, wait_until: str = "domcontentloaded") -> dict:
    """
    Navigate to URL.

    Args:
        url: Target URL
        wait_until: Wait strategy - 'load' | 'domcontentloaded' | 'networkidle' | 'none'

    Returns:
        {"status": "success/error", "message": ...}
    """
    page, error = _browser_manager.get_page()
    if error:
        return {"status": "error", "message": error}

    try:
        page.goto(url, wait_until=wait_until, timeout=30000)
        _browser_manager.reset_error_count()
        return {"status": "success", "message": f"Navigated to {url}"}
    except Exception as e:
        logger.error(f"Navigate failed: {e}")
        return {"status": "error", "message": f"Navigation failed: {str(e)}"}


def browser_screenshot() -> dict:
    """
    Take screenshot of current page.

    Returns:
        {"status": "success", "screenshot": "base64..."} or {"status": "error", "message": ...}
    """
    page, error = _browser_manager.get_page()
    if error:
        return {"status": "error", "message": error}

    try:
        screenshot_bytes = page.screenshot()
        import base64
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
        _browser_manager.reset_error_count()
        return {"status": "success", "screenshot": screenshot_b64}
    except Exception as e:
        logger.error(f"Screenshot failed: {e}")
        return {"status": "error", "message": f"Screenshot failed: {str(e)}"}


def browser_get_text() -> dict:
    """
    Extract text content from current page.

    Returns:
        {"status": "success", "text": "..."} or {"status": "error", "message": ...}
    """
    page, error = _browser_manager.get_page()
    if error:
        return {"status": "error", "message": error}

    try:
        text = page.inner_text("body")
        _browser_manager.reset_error_count()
        return {"status": "success", "text": text}
    except Exception as e:
        logger.error(f"Get text failed: {e}")
        return {"status": "error", "message": f"Get text failed: {str(e)}"}


def browser_click(selector: str) -> dict:
    """
    Click element by selector.

    Args:
        selector: Playwright selector (role-based, text, or CSS)

    Returns:
        {"status": "success/error", "message": ...}
    """
    page, error = _browser_manager.get_page()
    if error:
        return {"status": "error", "message": error}

    try:
        page.click(selector, timeout=5000)
        _browser_manager.reset_error_count()
        return {"status": "success", "message": f"Clicked element: {selector}"}
    except Exception as e:
        logger.error(f"Click failed: {e}")
        return {"status": "error", "message": f"Click failed: {str(e)}"}


def browser_fill(selector: str, text: str) -> dict:
    """
    Fill input field by selector.

    Args:
        selector: Playwright selector
        text: Text to fill

    Returns:
        {"status": "success/error", "message": ...}
    """
    page, error = _browser_manager.get_page()
    if error:
        return {"status": "error", "message": error}

    try:
        page.fill(selector, text, timeout=5000)
        _browser_manager.reset_error_count()
        return {"status": "success", "message": f"Filled {selector}"}
    except Exception as e:
        logger.error(f"Fill failed: {e}")
        return {"status": "error", "message": f"Fill failed: {str(e)}"}


def browser_wait_for_selector(selector: str, timeout: int = 5000) -> dict:
    """
    Wait for element to appear.

    Args:
        selector: Playwright selector
        timeout: Timeout in milliseconds

    Returns:
        {"status": "success/error", "message": ...}
    """
    page, error = _browser_manager.get_page()
    if error:
        return {"status": "error", "message": error}

    try:
        page.wait_for_selector(selector, timeout=timeout)
        _browser_manager.reset_error_count()
        return {"status": "success", "message": f"Element found: {selector}"}
    except Exception as e:
        logger.error(f"Wait for selector failed: {e}")
        return {"status": "error", "message": f"Element not found within {timeout}ms: {str(e)}"}


def browser_query_selector(selector: str) -> dict:
    """
    Check if element exists.

    Args:
        selector: Playwright selector

    Returns:
        {"status": "success", "exists": true/false} or {"status": "error", "message": ...}
    """
    page, error = _browser_manager.get_page()
    if error:
        return {"status": "error", "message": error}

    try:
        element = page.query_selector(selector)
        exists = element is not None
        _browser_manager.reset_error_count()
        return {"status": "success", "exists": exists}
    except Exception as e:
        logger.error(f"Query selector failed: {e}")
        return {"status": "error", "message": f"Query failed: {str(e)}"}


def browser_fill_multiple(fields: dict) -> dict:
    """
    Fill multiple form fields.

    Args:
        fields: Dict of {selector: text}

    Returns:
        {"status": "success", "results": {...}} or {"status": "error", "message": ...}
    """
    page, error = _browser_manager.get_page()
    if error:
        return {"status": "error", "message": error}

    results = {}
    success_count = 0

    for selector, text in fields.items():
        try:
            page.fill(selector, text, timeout=5000)
            results[selector] = {"status": "success", "text": text}
            success_count += 1
        except Exception as e:
            results[selector] = {"status": "error", "message": str(e)}

    _browser_manager.reset_error_count()
    return {
        "status": "success",
        "message": f"Filled {success_count}/{len(fields)} fields",
        "results": results
    }


def browser_fill_form(url: str, data: dict) -> dict:
    """
    High-level tool: Fill form with natural language field mapping.

    Args:
        url: Form URL
        data: Dict of {field_label: value} using natural language labels

    Returns:
        {"status": "success", "screenshot": "base64..."} or {"status": "error", "message": ...}
    """
    # Navigate to URL
    nav_result = browser_navigate(url, wait_until="networkidle")
    if nav_result["status"] == "error":
        return nav_result

    page, error = _browser_manager.get_page()
    if error:
        return {"status": "error", "message": error}

    # Try to find and fill fields by label
    results = {}
    for label, value in data.items():
        try:
            # Try multiple selector strategies
            selectors = [
                f"input:near(:text('{label}'))",
                f"[aria-label*='{label}']",
                f"[placeholder*='{label}']",
                f"input[name*='{label.lower().replace(' ', '_')}')"
            ]

            filled = False
            for selector in selectors:
                try:
                    page.fill(selector, str(value), timeout=2000)
                    results[label] = {"status": "success", "value": value}
                    filled = True
                    break
                except:
                    continue

            if not filled:
                results[label] = {"status": "error", "message": f"Field not found: {label}"}

        except Exception as e:
            results[label] = {"status": "error", "message": str(e)}

    # Take screenshot
    screenshot_result = browser_screenshot()

    _browser_manager.reset_error_count()
    return {
        "status": "success",
        "message": f"Filled {sum(1 for r in results.values() if r['status'] == 'success')}/{len(data)} fields",
        "results": results,
        "screenshot": screenshot_result.get("screenshot")
    }


def browser_answer_question(url: str, question: str) -> dict:
    """
    High-level tool: Answer question on webpage.

    Args:
        url: Page URL
        question: Question to answer

    Returns:
        {"status": "success", "answer": "..."} or {"status": "error", "message": ...}
    """
    # Navigate to URL
    nav_result = browser_navigate(url)
    if nav_result["status"] == "error":
        return nav_result

    # Get page text
    text_result = browser_get_text()
    if text_result["status"] == "error":
        return text_result

    # Simple keyword matching (would be enhanced with NLP in production)
    page_text = text_result["text"].lower()
    question_lower = question.lower()

    # Extract sentences containing question keywords
    keywords = question_lower.split()
    sentences = page_text.split('.')
    relevant = [s.strip() for s in sentences if any(k in s for k in keywords)]

    _browser_manager.reset_error_count()
    return {
        "status": "success",
        "answer": relevant[0] if relevant else "No relevant answer found",
        "context": relevant[:3] if relevant else []
    }


def browser_extract_data(url: str, selectors: dict) -> dict:
    """
    High-level tool: Extract structured data from page.

    Args:
        url: Page URL
        selectors: Dict of {field_name: selector}

    Returns:
        {"status": "success", "data": {...}} or {"status": "error", "message": ...}
    """
    # Navigate to URL
    nav_result = browser_navigate(url)
    if nav_result["status"] == "error":
        return nav_result

    page, error = _browser_manager.get_page()
    if error:
        return {"status": "error", "message": error}

    # Extract data
    data = {}
    for field_name, selector in selectors.items():
        try:
            element = page.query_selector(selector)
            if element:
                data[field_name] = element.inner_text()
            else:
                data[field_name] = None
        except Exception as e:
            data[field_name] = f"Error: {str(e)}"

    _browser_manager.reset_error_count()
    return {"status": "success", "data": data}


# ============== Tool Schemas ==============

TOOL_SCHEMAS = {
    "browser_navigate": {
        "name": "browser_navigate",
        "description": """浏览器导航工具

参数:
- url: 目标 URL
- wait_until: 等待策略 ('load' | 'domcontentloaded' | 'networkidle' | 'none')

返回:
- status: success | error
- message: 结果描述""",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标 URL"},
                "wait_until": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle", "none"],
                    "default": "domcontentloaded"
                }
            },
            "required": ["url"]
        }
    },
    "browser_screenshot": {
        "name": "browser_screenshot",
        "description": """浏览器截图工具

返回:
- status: success | error
- screenshot: base64 编码的截图（成功时）""",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    "browser_get_text": {
        "name": "browser_get_text",
        "description": """提取页面文本

返回:
- status: success | error
- text: 页面文本内容""",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    "browser_click": {
        "name": "browser_click",
        "description": """点击页面元素

参数:
- selector: Playwright 选择器（role-based、文本或 CSS）

返回:
- status: success | error
- message: 结果描述""",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Playwright 选择器"}
            },
            "required": ["selector"]
        }
    },
    "browser_fill": {
        "name": "browser_fill",
        "description": """填充输入框

参数:
- selector: Playwright 选择器
- text: 要填充的文本

返回:
- status: success | error
- message: 结果描述""",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Playwright 选择器"},
                "text": {"type": "string", "description": "要填充的文本"}
            },
            "required": ["selector", "text"]
        }
    },
    "browser_wait_for_selector": {
        "name": "browser_wait_for_selector",
        "description": """等待元素出现

参数:
- selector: Playwright 选择器
- timeout: 超时时间（毫秒，默认 5000）

返回:
- status: success | error
- message: 结果描述""",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Playwright 选择器"},
                "timeout": {"type": "integer", "default": 5000}
            },
            "required": ["selector"]
        }
    },
    "browser_query_selector": {
        "name": "browser_query_selector",
        "description": """检查元素是否存在

参数:
- selector: Playwright 选择器

返回:
- status: success | error
- exists: true | false""",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Playwright 选择器"}
            },
            "required": ["selector"]
        }
    },
    "browser_fill_multiple": {
        "name": "browser_fill_multiple",
        "description": """批量填充表单字段

参数:
- fields: 字段字典 {选择器: 文本}

返回:
- status: success | error
- results: 每个字段的填充结果""",
        "input_schema": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "object",
                    "additionalProperties": {"type": "string"}
                }
            },
            "required": ["fields"]
        }
    },
    "browser_fill_form": {
        "name": "browser_fill_form",
        "description": """高级工具：智能填充表单（自然语言字段映射）

参数:
- url: 表单 URL
- data: 字段数据 {字段标签: 值}

返回:
- status: success | error
- results: 每个字段的填充结果
- screenshot: 表单截图（base64）""",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "表单 URL"},
                "data": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "字段标签到值的映射"
                }
            },
            "required": ["url", "data"]
        }
    },
    "browser_answer_question": {
        "name": "browser_answer_question",
        "description": """高级工具：在网页上回答问题

参数:
- url: 页面 URL
- question: 问题文本

返回:
- status: success | error
- answer: 答案
- context: 相关上下文""",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "页面 URL"},
                "question": {"type": "string", "description": "问题"}
            },
            "required": ["url", "question"]
        }
    },
    "browser_extract_data": {
        "name": "browser_extract_data",
        "description": """高级工具：提取页面结构化数据

参数:
- url: 页面 URL
- selectors: 字段选择器 {字段名: 选择器}

返回:
- status: success | error
- data: 提取的数据""",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "页面 URL"},
                "selectors": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "字段名到选择器的映射"
                }
            },
            "required": ["url", "selectors"]
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
