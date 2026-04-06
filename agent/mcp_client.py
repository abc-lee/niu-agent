"""
MCP Client Manager

管理多个 MCP 服务器的连接，提供工具调用接口。

设计：
- 每次调用时临时连接，用完关闭
- 简单可靠，避免 context manager 跨任务问题
- 工具名格式：server_name/tool_name

用法：
    from agent.mcp_client import call_mcp_tool, list_mcp_tools

    tools = await list_mcp_tools()
    result = await call_mcp_tool("photo-server/ingest_photo", {"path": "/path/to/photo.jpg"})
"""

import asyncio
import os
import sys
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from loguru import logger

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class MCPServerConfig:
    """MCP 服务器配置"""

    name: str
    command: str
    args: List[str]
    workdir: Optional[str] = None
    env: Optional[Dict[str, str]] = None


def get_python_path() -> str:
    """获取 Python 解释器路径"""
    # 1. 打包的 Python
    packaged = os.path.join(os.path.dirname(__file__), "..", "python", "Scripts", "python.exe")
    if os.path.exists(packaged):
        return os.path.abspath(packaged)

    # 2. 虚拟环境
    venv = os.path.join(os.path.dirname(__file__), "..", "venv", "Scripts", "python.exe")
    if os.path.exists(venv):
        return os.path.abspath(venv)

    # 3. 系统 Python
    return sys.executable


def load_mcp_configs() -> Dict[str, MCPServerConfig]:
    """加载 MCP 服务器配置

    注意：embedding-service 已整合到 niu_api 内部，不再是独立服务
    """
    python_path = get_python_path()
    base_dir = os.path.dirname(__file__)
    servers_dir = os.path.join(base_dir, "..", "mcp-servers")

    # 继承当前进程的所有环境变量
    # 这样 MCP 服务器可以访问 PYTHONPATH、PATH 等
    inherited_env = os.environ.copy()

    # 添加主项目根目录到 PYTHONPATH，确保 MCP 服务器能找到主项目模块
    project_root = os.path.abspath(os.path.join(base_dir, ".."))
    pythonpath = inherited_env.get("PYTHONPATH", "")
    if pythonpath:
        inherited_env["PYTHONPATH"] = f"{project_root};{pythonpath}"
    else:
        inherited_env["PYTHONPATH"] = project_root

    # 获取 scheduler 数据库路径
    def _get_scheduler_db_path() -> str:
        """获取 scheduler 数据库路径"""
        import json as json_module
        from pathlib import Path as PathModule

        # 优先使用环境变量
        db_path = os.environ.get("SCHEDULER_DB_PATH")
        if db_path and PathModule(db_path).parent.exists():
            return db_path

        # 从 ~/.niu/memory.json 读取工作目录
        memory_path = PathModule.home() / ".niu" / "memory.json"
        if memory_path.exists():
            try:
                with open(memory_path, "r", encoding="utf-8") as f:
                    memory = json_module.load(f)
                    workspace = memory.get("workspace", {}).get("path")
                    if workspace and PathModule(workspace).exists():
                        return str(PathModule(workspace) / "scheduled_tasks.db")
            except Exception:
                pass

        # 默认路径
        return str(PathModule.home() / ".niu" / "scheduled_tasks.db")

    scheduler_db_path = _get_scheduler_db_path()

    configs = {
        "photo-server": MCPServerConfig(
            name="photo-server",
            command=python_path,
            args=["-m", "niu_photo_server"],
            workdir=os.path.join(servers_dir, "photo-server", "src"),
            env=inherited_env,  # 继承环境变量
        ),
        "kg-server": MCPServerConfig(
            name="kg-server",
            command=python_path,
            args=["-m", "niu_kg_server"],
            workdir=os.path.join(servers_dir, "kg-server", "src"),
            env=inherited_env,
        ),
        "vector-store": MCPServerConfig(
            name="vector-store",
            command=python_path,
            args=["-m", "niu_vector_store"],
            workdir=os.path.join(servers_dir, "vector-store", "src"),
            env=inherited_env,
        ),
        "config-manager": MCPServerConfig(
            name="config-manager",
            command=python_path,
            args=["-m", "niu_config_manager"],
            workdir=os.path.join(servers_dir, "config-manager", "src"),
            env=inherited_env,
        ),
        "file-parser": MCPServerConfig(
            name="file-parser",
            command=python_path,
            args=["-m", "niu_file_parser"],
            workdir=os.path.join(servers_dir, "file-parser", "src"),
            env=inherited_env,
        ),
        "memory-server": MCPServerConfig(
            name="memory-server",
            command=python_path,
            args=["-m", "niu_memory_server"],
            workdir=os.path.join(servers_dir, "memory-server", "src"),
            env=inherited_env,
        ),
        "scheduler-server": MCPServerConfig(
            name="scheduler-server",
            command=python_path,
            args=["-m", "niu_scheduler_server"],
            workdir=os.path.join(servers_dir, "scheduler-server", "src"),
            env={
                **inherited_env,
                "SCHEDULER_DB_PATH": scheduler_db_path,
            },
        ),
    }

    return configs


# 全局配置
_MCP_CONFIGS = load_mcp_configs()

# 缓存的工具列表
_CACHED_MCP_TOOLS: Optional[List[Dict]] = None


async def call_mcp_server(server_name: str, action: str, **kwargs) -> Any:
    """
    连接 MCP 服务器并执行操作

    Args:
        server_name: 服务器名称
        action: 操作类型 ("list_tools" 或工具名)
        **kwargs: 工具参数

    Returns:
        操作结果
    """
    config = _MCP_CONFIGS.get(server_name)
    if not config:
        raise ValueError(f"Unknown MCP server: {server_name}")

    server_params = StdioServerParameters(
        command=config.command,
        args=config.args,
        env=config.env or {},
        cwd=config.workdir,  # Set working directory so Python can find the module
    )

    logger.info(f"Connecting to MCP server: {server_name} for {action}")

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                if action == "list_tools":
                    result = await session.list_tools()
                    return [
                        {
                            "name": f"{server_name}/{tool.name}",
                            "description": tool.description or "",
                            "input_schema": tool.inputSchema,
                        }
                        for tool in result.tools
                    ]
                else:
                    # 调用工具
                    result = await session.call_tool(action, kwargs)

                    if result.content:
                        text_content = []
                        for block in result.content:
                            if hasattr(block, "text"):
                                text_content.append(block.text)
                            elif hasattr(block, "data"):
                                text_content.append(str(block.data))
                        return {"status": "success", "content": "\n".join(text_content)}
                    return {"status": "success", "content": ""}

    except Exception as e:
        logger.error(f"MCP call failed: {server_name}/{action} - {e}")
        if action == "list_tools":
            return []
        return {"status": "error", "error": str(e)}


async def list_mcp_tools(force_reload: bool = False) -> List[Dict]:
    """
    列出所有 MCP 服务器的工具（带缓存）

    Args:
        force_reload: 强制重新加载（忽略缓存）

    Returns:
        所有工具列表
    """
    global _CACHED_MCP_TOOLS

    # 使用缓存
    if _CACHED_MCP_TOOLS is not None and not force_reload:
        logger.info(f"Using cached MCP tools: {len(_CACHED_MCP_TOOLS)} tools")
        return _CACHED_MCP_TOOLS

    logger.info("Loading MCP tools from servers...")
    all_tools = []
    for server_name in _MCP_CONFIGS:
        try:
            tools = await call_mcp_server(server_name, "list_tools")
            all_tools.extend(tools)
        except Exception as e:
            logger.warning(f"Failed to list tools from {server_name}: {e}")

    # 缓存结果
    _CACHED_MCP_TOOLS = all_tools
    logger.info(f"Cached MCP tools: {len(all_tools)} tools")
    return all_tools


async def call_mcp_tool(full_tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """
    调用 MCP 工具

    Args:
        full_tool_name: 完整工具名 (server_name/tool_name)
        arguments: 工具参数

    Returns:
        工具执行结果
    """
    if "/" not in full_tool_name:
        return {"status": "error", "error": f"Invalid tool name: {full_tool_name}"}

    server_name, tool_name = full_tool_name.split("/", 1)
    return await call_mcp_server(server_name, tool_name, **arguments)


# 便捷函数 - 保持向后兼容
class MCPClientManager:
    """向后兼容的包装器"""

    def __init__(self):
        self.configs = _MCP_CONFIGS

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def list_all_tools(self) -> List[Dict]:
        return await list_mcp_tools()

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await call_mcp_server(server_name, tool_name, **arguments)

    async def call_tool_by_full_name(
        self, full_tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await call_mcp_tool(full_tool_name, arguments)


def get_mcp_manager() -> MCPClientManager:
    """获取 MCP 客户端管理器"""
    return MCPClientManager()


def get_cached_mcp_tools() -> List[Dict]:
    """
    同步获取缓存的 MCP 工具列表

    Returns:
        MCP 工具列表（如果缓存不存在则返回空列表）
    """
    return _CACHED_MCP_TOOLS or []


def get_mcp_tools_for_servers(server_names: List[str]) -> List[Dict]:
    """
    获取指定 MCP 服务器的工具列表（带降级机制）

    Args:
        server_names: MCP 服务器名称列表（如 ["photo-server", "vector-store"]）

    Returns:
        过滤后的 MCP 工具列表
    """
    cached = get_cached_mcp_tools()

    # 如果缓存不存在，尝试通过 MCPSyncBridge 加载
    if not cached:
        try:
            from .mcp_sync_bridge import get_mcp_bridge

            bridge = get_mcp_bridge()
            cached = bridge.list_tools(timeout=30)
            if cached:
                logger.info(f"Loaded {len(cached)} MCP tools via bridge")
        except Exception as e:
            logger.warning(f"Failed to load MCP tools via bridge: {e}")

    if not cached:
        return []

    # 过滤出指定服务器的工具
    filtered = []
    for tool in cached:
        tool_name = tool.get("name", "")
        # 工具名格式：server_name/tool_name
        if "/" in tool_name:
            server = tool_name.split("/")[0]
            if server in server_names:
                filtered.append(tool)

    # 检查是否有服务器的工具缺失
    cached_servers = {t["name"].split("/")[0] for t in cached if "/" in t.get("name", "")}
    missing_servers = set(server_names) - cached_servers

    if missing_servers:
        logger.warning(f"Missing MCP tools for servers: {missing_servers}, attempting to load...")
        try:
            from .mcp_sync_bridge import get_mcp_bridge
            bridge = get_mcp_bridge()

            # 尝试加载缺失的工具
            tools = bridge.list_tools(timeout=30)
            for tool in tools:
                tool_name = tool.get("name", "")
                if "/" in tool_name:
                    server = tool_name.split("/")[0]
                    if server in missing_servers:
                        filtered.append(tool)

            logger.info(f"Loaded missing tools for {missing_servers}")
        except Exception as e:
            logger.error(f"Failed to load missing MCP tools: {e}")

    return filtered
