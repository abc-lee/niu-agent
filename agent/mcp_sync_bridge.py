"""
MCP 同步桥接器

提供同步接口封装异步 MCP 调用，使用后台事件循环避免每次创建新 loop。
"""

import asyncio
import threading
from concurrent.futures import Future
from typing import Any, Dict, Optional

from loguru import logger


class MCPSyncBridge:
    """
    MCP 同步桥接器

    使用后台线程 + 事件循环模式：
    - 单例模式，全局共享一个事件循环
    - 通过 run_coroutine_threadsafe 避免事件循环冲突
    """

    _instance: Optional["MCPSyncBridge"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()

    def start(self):
        """启动后台事件循环"""
        if self._thread is not None and self._thread.is_alive():
            return

        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="mcp-bridge")
        self._thread.start()
        self._started.wait(timeout=5)
        logger.info("[MCPSyncBridge] Started background event loop")

    def _run_loop(self):
        """后台线程运行事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._started.set()
        self._loop.run_forever()

    def call_tool(
        self, server_name: str, tool_name: str, args: dict, timeout: float = 60.0
    ) -> Dict[str, Any]:
        """
        同步调用 MCP 工具

        Args:
            server_name: MCP 服务器名
            tool_name: 工具名
            args: 参数
            timeout: 超时秒数

        Returns:
            工具执行结果
        """
        self.start()  # 确保已启动

        future: Future = asyncio.run_coroutine_threadsafe(
            self._call_tool_async(server_name, tool_name, args), self._loop
        )
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            logger.error(f"[MCPSyncBridge] Timeout: {server_name}/{tool_name}")
            return {"status": "error", "msg": f"Timeout after {timeout}s"}
        except Exception as e:
            logger.error(f"[MCPSyncBridge] Call failed: {e}")
            return {"status": "error", "msg": str(e)}

    async def _call_tool_async(
        self, server_name: str, tool_name: str, args: dict
    ) -> Dict[str, Any]:
        """异步调用 MCP 工具"""
        from agent.mcp_client import call_mcp_server

        return await call_mcp_server(server_name, tool_name, **args)

    def list_tools(self, timeout: float = 30.0) -> list:
        """同步获取工具列表"""
        self.start()
        future: Future = asyncio.run_coroutine_threadsafe(self._list_tools_async(), self._loop)
        try:
            return future.result(timeout=timeout)
        except Exception as e:
            logger.error(f"[MCPSyncBridge] Failed to list tools: {e}")
            return []

    async def _list_tools_async(self) -> list:
        """异步获取工具列表"""
        from agent.mcp_client import list_mcp_tools

        return await list_mcp_tools()

    def stop(self):
        """停止后台事件循环"""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            logger.info("[MCPSyncBridge] Stopped")


# 全局实例
_bridge: Optional[MCPSyncBridge] = None


def get_mcp_bridge() -> MCPSyncBridge:
    """获取 MCP 同步桥接器实例"""
    global _bridge
    if _bridge is None:
        _bridge = MCPSyncBridge()
    return _bridge
