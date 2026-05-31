"""MCP Client Manager — 管理外部 MCP 服务器连接（stdio + HTTP + Sampling）"""
import asyncio
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class MCPClientManager:
    """管理所有 MCP Client 连接（stdio + HTTP）"""

    def __init__(self, sampling_callback: Optional[Callable] = None):
        self._connections: dict = {}  # server_name -> ClientSession
        self._connection_contexts: dict = {}  # server_name -> (transport_cm, session_cm) 用于清理
        self._sampling_callback = sampling_callback

    async def connect_stdio(self, server_name: str, command: str, args: list[str], env: dict = None):
        """连接 stdio 模式的 MCP 服务器

        手动管理上下文生命周期（不用 async with），否则 session 在方法退出时关闭。
        """
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
        )

        stdio_cm = stdio_client(server_params)
        read_stream, write_stream = await stdio_cm.__aenter__()

        session_cm = ClientSession(
            read_stream, write_stream,
            sampling_callback=self._sampling_callback,
        )
        session = await session_cm.__aenter__()
        await session.initialize()

        self._connections[server_name] = session
        self._connection_contexts[server_name] = (stdio_cm, session_cm)
        logger.info(f"Connected to external MCP server (stdio): {server_name}")

    async def connect_http(self, server_name: str, url: str):
        """连接 HTTP 模式的 MCP 服务器"""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        http_cm = streamablehttp_client(url)
        read_stream, write_stream, _ = await http_cm.__aenter__()

        session_cm = ClientSession(
            read_stream, write_stream,
            sampling_callback=self._sampling_callback,
        )
        session = await session_cm.__aenter__()
        await session.initialize()

        self._connections[server_name] = session
        self._connection_contexts[server_name] = (http_cm, session_cm)
        logger.info(f"Connected to external MCP server (http): {server_name}")

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> dict:
        """通过 MCP Client 调用工具（异步）"""
        if server_name not in self._connections:
            raise KeyError(f"MCP server not connected: {server_name}")
        session = self._connections[server_name]
        result = await session.call_tool(tool_name, arguments)
        return result

    def call_tool_sync(self, server_name: str, tool_name: str, arguments: dict) -> dict:
        """同步调用 MCP Client（通过 asyncio 桥接，供 handler.py 使用）"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self.call_tool(server_name, tool_name, arguments), loop
            )
            return future.result(timeout=30)
        else:
            return asyncio.run(self.call_tool(server_name, tool_name, arguments))

    async def list_tools(self, server_name: str) -> list:
        """获取工具列表"""
        if server_name not in self._connections:
            raise KeyError(f"MCP server not connected: {server_name}")
        session = self._connections[server_name]
        result = await session.list_tools()
        return result.tools

    async def disconnect(self, server_name: str):
        """断开连接，清理上下文管理器"""
        if server_name in self._connection_contexts:
            transport_cm, session_cm = self._connection_contexts.pop(server_name)
            try:
                await session_cm.__aexit__(None, None, None)
            except Exception:
                pass
            try:
                await transport_cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._connections.pop(server_name, None)
        logger.info(f"Disconnected from external MCP server: {server_name}")

    async def disconnect_all(self):
        """断开所有连接"""
        for name in list(self._connections.keys()):
            await self.disconnect(name)
