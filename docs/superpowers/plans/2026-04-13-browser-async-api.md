# Browser-Server 改用 Playwright async_api 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 browser-server 从 `playwright.sync_api` 改为 `playwright.async_api`，在独立线程中运行自有 asyncio loop，彻底解决 sync/async 架构冲突，同时修复 `_browser` 永远为 None 导致重复启动浏览器的 bug。

**Architecture:** BrowserManager 在独立守护线程中运行自己的 `asyncio.new_event_loop()` + `playwright.async_api`。主进程通过 `threading.Event` + 结果队列与守护线程通信，保持同步调用接口不变。`launch_persistent_context` 返回 BrowserContext（不是 Browser），用 `self._context` 替代 `self._browser` 做状态判断。

**Tech Stack:** Python, playwright.async_api, asyncio, threading, queue

---

## 问题分析

### 官方文档确认

Playwright 官方文档（playwright.dev + microsoft/playwright-python README）明确说明：

> Playwright supports both synchronous and asynchronous APIs; use the async version for projects utilizing asyncio.

- `sync_api`：纯同步环境使用，检测到 asyncio running loop 会抛 `Error`
- `async_api`：原生基于 asyncio 设计，在 FastAPI/asyncio 环境中正常使用

### 根因链

```
FastAPI async endpoint → run_in_executor → sync_chat() → handler.dispatch()
  → ToolRegistry.call("browser_navigate") → browser_navigate()
    → BrowserManager.get_page() → sync_playwright().start()
```

`run_in_executor` 线程中没有 asyncio running loop，所以 `sync_playwright` **理论上可以工作**。但实际有两个 bug：

1. **`_browser` 永远为 None**：`_start_browser()` 用 `launch_persistent_context()` 只设了 `self._context`，不设 `self._browser`。`get_page()` 用 `self._browser` 判断是否已启动 → 永远为 None → 每次都重新启动 → 第二次启动时 Playwright 实例冲突 → "Failed to start browser"

2. **昨天 Agent 直接改了 pip 源码**：注释掉了 `_context_manager.py` 中的 asyncio loop 检测。`pip upgrade` 会覆盖，不安全。

### 解决方案

1. 改用 `playwright.async_api`（官方推荐）
2. BrowserManager 在独立守护线程中运行自有 asyncio loop
3. 用 `self._context` 替代 `self._browser` 做状态判断
4. 恢复 Playwright pip 源码到官方状态（已完成）

---

## 文件结构

```
mcp-servers/browser-server/src/niu_browser_server/
├── __init__.py          # 重写：BrowserManager 改用 async_api + 守护线程

agent/
├── handler.py           # 无需修改（ToolRegistry 同步调用接口不变）

tests/
└── test_browser_manager.py  # 新增：测试 BrowserManager 异步架构
```

---

## 任务 1：编写 BrowserManager 异步架构的失败测试

**文件：**
- 创建：`tests/test_browser_manager.py`

- [ ] **步骤 1：编写失败测试**

```python
"""测试 BrowserManager 异步架构（async_api + 守护线程）。"""
import pytest
import time
from unittest.mock import MagicMock


def test_browser_manager_starts_in_dedicated_thread():
    """BrowserManager 应在独立守护线程中运行自有 asyncio loop。"""
    from niu_browser_server import BrowserManager

    mgr = BrowserManager()
    # BrowserManager 应该有一个守护线程
    assert mgr._daemon_thread is not None, "BrowserManager 应有守护线程"
    assert mgr._daemon_thread.daemon is True, "守护线程应为 daemon"
    assert mgr._daemon_thread.is_alive(), "守护线程应正在运行"


def test_browser_manager_uses_async_api():
    """BrowserManager 应使用 playwright.async_api 而非 sync_api。"""
    from niu_browser_server import BrowserManager
    import inspect

    # 检查 _start_browser 方法不使用 sync_playwright
    source = inspect.getsource(BrowserManager._start_browser_impl)
    assert "sync_playwright" not in source, "_start_browser_impl 不应使用 sync_playwright"
    assert "async_playwright" in source, "_start_browser_impl 应使用 async_playwright"


def test_get_page_uses_context_not_browser():
    """get_page() 应使用 self._context 判断浏览器是否已启动，而非 self._browser。"""
    from niu_browser_server import BrowserManager

    mgr = BrowserManager()
    # 模拟：_context 已设置但 _browser 为 None（launch_persistent_context 的场景）
    mgr._context = MagicMock()
    mgr._page = MagicMock()
    mgr._browser = None  # launch_persistent_context 不设 _browser

    # get_page() 应该能正确识别浏览器已启动
    # 不应该尝试重新启动
    page, error = mgr.get_page()
    assert error is None, f"get_page() 应返回已有页面，不应报错: {error}"
    assert page is not None, "get_page() 应返回 Page 对象"


def test_browser_navigate_returns_success():
    """browser_navigate 应能正常导航并返回成功。"""
    from niu_browser_server import browser_navigate

    result = browser_navigate("https://example.com")
    assert result["status"] == "success", f"导航应成功: {result}"
    assert "example.com" in result["message"]


def test_browser_manager_idle_monitor_uses_context():
    """空闲监控应使用 _context 判断，而非 _browser。"""
    from niu_browser_server import BrowserManager
    import inspect

    source = inspect.getsource(BrowserManager._idle_monitor)
    # _idle_monitor 中不应依赖 self._browser 做判断
    # 应该用 self._context
    assert "self._browser" not in source or "self._context" in source, \
        "_idle_monitor 应使用 _context 而非仅 _browser 做判断"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/test_browser_manager.py -v`

预期：失败（BrowserManager 还是 sync_api 架构，没有守护线程，_browser 判断逻辑未改）

---

## 任务 2：重写 BrowserManager 改用 async_api + 守护线程

**文件：**
- 修改：`mcp-servers/browser-server/src/niu_browser_server/__init__.py`

- [ ] **步骤 3：重写 __init__.py**

```python
"""
Niu Browser MCP Server

Provides browser navigation tool using Playwright.
Uses async_api in a dedicated daemon thread with its own asyncio loop.

Architecture:
- Main process: FastAPI async → run_in_executor → sync handler
- BrowserManager: dedicated daemon thread with own asyncio loop + playwright.async_api
- Communication: call_async() sends coroutines to daemon thread, blocks until result
"""

import asyncio
import os
import queue
import threading
import time
from pathlib import Path
from typing import Literal, Optional
from loguru import logger


# ============== Browser Manager (Singleton, async_api in daemon thread) ==============

class BrowserManager:
    """
    Singleton browser instance manager with async_api in dedicated thread.

    Architecture:
    - Dedicated daemon thread runs its own asyncio event loop
    - All Playwright operations are async, executed in the daemon thread
    - Main thread calls call_async(coro) which sends coroutine to daemon thread
    - Results returned via queue (blocking, with timeout)

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

        self._context = None  # Persistent browser context (async_api)
        self._page = None     # Current page (async_api)
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
        self._call_queue: queue.Queue = queue.Queue()
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

            # Use persistent context to keep cookies and login state
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._user_data_dir),
                headless=False,  # Show browser window
                viewport={"width": 1280, "height": 720}
            )

            # Get or create page
            if len(self._context.pages) > 0:
                self._page = self._context.pages[0]
            else:
                self._page = await self._context.new_page()

            self._last_used = time.time()
            self._error_count = 0
            logger.info(f"Browser started (async_api, headless=False)")
            return True
        except Exception as e:
            logger.error(f"Failed to start browser: {e}")
            return False

    async def _close_browser_impl(self):
        """Close browser (async_api, runs in daemon thread)."""
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.error(f"Error closing browser: {e}")
        finally:
            self._page = None
            self._context = None
            self._playwright = None
            self._last_used = 0
            logger.info("Browser closed (cookies and login state saved)")

    async def _health_check_impl(self) -> bool:
        """Check if browser is healthy (async_api)."""
        if not self._context or not self._page:
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
            # Force cleanup
            self._page = None
            self._context = None
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

    def get_page(self) -> tuple[Optional[object], Optional[str]]:
        """
        Get or create browser page.

        Uses self._context (not self._browser) to check if browser is running,
        because launch_persistent_context() returns BrowserContext, not Browser.

        Returns:
            tuple: (Page instance, error message if failed)
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
            return self._page, None

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
        # page is async_api Page, need to call via call_async
        async def _navigate():
            await page.goto(url, wait_until=wait_until, timeout=30000)

        _browser_manager.call_async(_navigate())
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
```

- [ ] **步骤 4：运行测试验证通过**

运行：`pytest tests/test_browser_manager.py -v`

预期：部分测试通过（架构相关测试通过，实际导航测试需要浏览器环境）

- [ ] **步骤 5：手动验证浏览器启动**

运行：`python -c "from niu_browser_server import browser_navigate; print(browser_navigate('https://example.com'))"`

预期：`{'status': 'success', 'message': 'Navigated to https://example.com'}`

- [ ] **步骤 6：提交**

```bash
git add mcp-servers/browser-server/src/niu_browser_server/__init__.py tests/test_browser_manager.py
git commit -m "refactor: BrowserManager 改用 playwright.async_api + 守护线程

- 替换 sync_api 为 async_api，在独立守护线程中运行自有 asyncio loop
- 修复 _browser 永远为 None 的 bug：改用 _context 做状态判断
- 移除 PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS hack
- 通过 call_async() 桥接同步调用接口与异步 Playwright 操作
- Playwright 升级安全，无需修改 pip 安装目录源码"
```

---

## 任务 3：更新 CLAUDE.md 文档

**文件：**
- 修改：`CLAUDE.md`

- [ ] **步骤 7：更新 browser-server 相关文档**

在 CLAUDE.md 的 MCP 服务器表格中，更新 browser-server 的描述，添加架构说明：

找到 `browser-server` 行，更新为：

```markdown
| `browser-server` | 浏览器自动化（Playwright async_api + 守护线程） | ✅ |
```

在 MCP 同进程架构部分添加说明：

```markdown
**Browser-Server 架构**：
- `playwright.async_api` 在独立守护线程中运行（自有 asyncio loop）
- 主进程通过 `call_async()` 桥接同步调用与异步 Playwright
- `launch_persistent_context()` 返回 BrowserContext，用 `_context` 做状态判断
- 不修改 pip 安装目录源码，Playwright 升级安全
```

- [ ] **步骤 8：提交**

```bash
git add CLAUDE.md
git commit -m "docs: 更新 browser-server 架构文档（async_api + 守护线程）"
```

---

## 任务 4：端到端验证

- [ ] **步骤 9：重启服务并测试**

```bash
# 清理 Python 缓存
find . -name "*.pyc" -delete
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# 重启服务
go run main.go
```

- [ ] **步骤 10：测试"上网查新闻"场景**

在对话中输入"上网查一下今日热点新闻"，检查：

1. 第一轮：LLM 调用 `browser_navigate` → 成功
2. 第二轮：browser-automation Skill 被注入（Pending Skills 机制）
3. 后续轮次：浏览器操作正常，无 "Failed to start browser" 错误
4. 日志中无 "It looks like you are using Playwright Sync API inside the asyncio loop" 错误

---

## 验证清单

- [ ] Playwright pip 源码已恢复官方状态（无本地修改）
- [ ] BrowserManager 使用 async_api（非 sync_api）
- [ ] BrowserManager 在守护线程中运行自有 asyncio loop
- [ ] `get_page()` 使用 `_context` 做状态判断（非 `_browser`）
- [ ] `_idle_monitor()` 使用 `_context` 做状态判断
- [ ] `browser_navigate()` 正常工作
- [ ] 无 "Failed to start browser" 错误
- [ ] 无 asyncio loop 冲突错误
- [ ] Playwright 升级后无需修改源码
- [ ] 所有现有测试通过

---

## 解决方案覆盖矩阵

| 问题 | 严重程度 | 解决方案 | 任务 |
|------|---------|---------|------|
| 1. sync_api 在 asyncio loop 中报错 | CRITICAL | 改用 async_api + 守护线程 | 2 |
| 2. pip 源码被直接修改 | CRITICAL | 已恢复 + 不再修改 | - |
| 3. _browser 永远为 None | HIGH | 改用 _context 做判断 | 2 |
| 4. get_page() 每次重新启动浏览器 | HIGH | _context 判断修复 | 2 |
| 5. _idle_monitor 永远不生效 | MEDIUM | 改用 _context 判断 | 2 |
| 6. PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS hack | MEDIUM | 移除 | 2 |
| 7. 文档未更新 | LOW | 更新 CLAUDE.md | 3 |
