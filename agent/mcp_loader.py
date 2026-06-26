"""
MCP Tool Loader

Loads all required MCP modules at startup with strict validation.
Any failure to load critical MCP servers will terminate the application.
"""

import sys
from pathlib import Path
from typing import List, Tuple, Optional
from loguru import logger
from agent.tool_registry import ToolRegistry, set_registry


# ============================================================================
# Required MCP Servers
# ============================================================================

REQUIRED_SERVERS: List[Tuple[str, str]] = [
    ("photo-server", "niu_photo_server"),
    ("config-manager", "niu_config_manager"),
    ("memory-server", "niu_memory_server"),
    # vector-store removed — replaced by lightrag-server (LightRAG unified tools)
    ("lightrag-server", "niu_lightrag_server"),
    # kg-server removed — replaced by LightRAG adapter/pipeline
    ("file-parser", "niu_file_parser"),
    ("session-manager", "niu_session_manager"),
    ("scheduler-server", "niu_scheduler_server"),
    ("browser-server", "niu_browser_server"),
    ("brain-region-server", "niu_brain_region_server"),
]

OPTIONAL_SERVERS: List[Tuple[str, str]] = [
    ("ha-server", "niu_ha_server"),
]


# ============================================================================
# Config Loader
# ============================================================================

def _load_mcp_config() -> dict:
    """Load MCP server configuration from config/mcp-servers.yaml"""
    import yaml

    config_path = Path(__file__).parent.parent / "config" / "mcp-servers.yaml"

    if not config_path.exists():
        logger.warning(f"MCP config file not found: {config_path}")
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"Failed to load MCP config: {e}")
        return {}


def _add_server_workdirs_to_sys_path(config: dict) -> None:
    """Add server workdirs to sys.path for module imports"""
    project_root = Path(__file__).parent.parent

    for server_name, server_config in config.items():
        if not isinstance(server_config, dict):
            continue

        workdir = server_config.get("workdir")
        if not workdir:
            continue

        # Resolve workdir relative to project root
        workdir_path = (project_root / workdir).resolve()

        if workdir_path.exists() and str(workdir_path) not in sys.path:
            sys.path.insert(0, str(workdir_path))
            logger.debug(f"Added to sys.path: {workdir_path}")


# ============================================================================
# Built-in Tool Registration
# ============================================================================

def _inject_tools_to_lightrag(registry: ToolRegistry, servers: List[Tuple[str, str]]) -> None:
    """No-op in disk mode — tools discovered via disk YAML, not LightRAG."""
    logger.debug("[MCP Loader] Tool injection to LightRAG skipped (disk mode)")


# ============================================================================
# Loader Function
# ============================================================================

def load_mcp_tools(required_servers: Optional[List[Tuple[str, str]]] = None) -> ToolRegistry:
    """
    Load all required MCP modules and register their tools.

    Args:
        required_servers: Optional list of (server_name, module_name) tuples.
                         If not provided, uses the default REQUIRED_SERVERS list.

    Returns:
        ToolRegistry: Registry containing all MCP tool functions and schemas.

    Raises:
        RuntimeError: If any required MCP server fails to load.
    """
    servers = required_servers or REQUIRED_SERVERS

    # Load MCP configuration and add workdirs to sys.path
    config = _load_mcp_config()
    # 确保项目根目录在 sys.path（MCP 服务器需要 import agent.*）
    from pathlib import Path as _Path
    _project_root = str(_Path(__file__).resolve().parent.parent)
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

    _add_server_workdirs_to_sys_path(config)

    registry = ToolRegistry()
    failed_servers = []

    for server_name, module_name in servers:
        try:
            module = __import__(module_name, fromlist=["get_tool_schemas"])

            # 从配置中提取该 server 的 tools visibility 映射
            visibility_map = None
            server_config = config.get(server_name, {})
            if isinstance(server_config, dict) and "tools" in server_config:
                visibility_map = server_config["tools"]

            if not registry.register_server(server_name, module, visibility_map):
                failed_servers.append(f"{server_name} (registration failed)")

        except ImportError as e:
            failed_servers.append(f"{server_name} (import failed: {e})")
        except Exception as e:
            failed_servers.append(f"{server_name} (error: {e})")

    # Strict validation: any failure terminates startup
    if failed_servers:
        error_msg = f"Critical MCP servers failed to load:\n" + "\n".join(
            f"  - {s}" for s in failed_servers
        )
        raise RuntimeError(error_msg)

    logger.info(f"All {len(servers)} servers loaded")

    # Load optional servers (failure does not terminate startup)
    for server_name, module_name in OPTIONAL_SERVERS:
        server_config = config.get(server_name, {})
        if not isinstance(server_config, dict) or not server_config:
            logger.debug(f"Optional server {server_name} not configured, skipping")
            continue

        try:
            module = __import__(module_name, fromlist=["get_tool_schemas"])

            visibility_map = None
            if isinstance(server_config, dict) and "tools" in server_config:
                visibility_map = server_config["tools"]

            if registry.register_server(server_name, module, visibility_map):
                logger.info(f"Optional server loaded: {server_name}")
            else:
                logger.warning(f"Optional server {server_name} registration failed")

        except ImportError as e:
            logger.debug(f"Optional server {server_name} not available: {e}")
        except Exception as e:
            logger.warning(f"Optional server {server_name} error: {e}")

    # Set global registry instance
    set_registry(registry)

    # lightrag-query is now provided by lightrag-server MCP module
    # (lightrag-server/lightrag_query). No separate built-in registration needed.

    # Tool injection to LightRAG is a no-op in disk mode
    # (tools discovered via disk YAML, not LightRAG entities)
    _inject_tools_to_lightrag(registry, servers)

    return registry


# ============================================================================
# External Server Support
# ============================================================================

def is_external_server(server_config: dict) -> bool:
    """判断 MCP 服务器是否为外部服务器（stdio/HTTP 模式）

    Args:
        server_config: mcp-servers.yaml 中单个服务器的配置

    Returns:
        True 表示外部服务器（stdio/HTTP），False 表示内部服务器（同进程）
    """
    mode = server_config.get("mode", "")
    return mode in ("stdio", "http")


async def load_external_servers(mcp_client, registry=None):
    """加载外部 MCP 服务器（stdio/HTTP 模式）

    读取 mcp-servers.yaml 配置，连接所有外部服务器，
    注册外部工具到 ToolRegistry。

    Args:
        mcp_client: MCPClientManager 实例
        registry: ToolRegistry 实例（默认使用全局 registry）
    """
    from agent.tool_registry import get_registry

    if registry is None:
        registry = get_registry()

    config = _load_mcp_config()
    if not config:
        logger.warning("No MCP server config found")
        return

    for server_name, server_config in config.items():
        if not is_external_server(server_config):
            continue

        mode = server_config.get("mode", "")
        logger.info(f"Loading external MCP server: {server_name} (mode={mode})")

        try:
            if mode == "stdio":
                command = server_config.get("command", "")
                args = server_config.get("args", [])
                env = server_config.get("env", None)
                await mcp_client.connect_stdio(server_name, command, args, env)
            elif mode == "http":
                url = server_config.get("url", "")
                await mcp_client.connect_http(server_name, url)

            # 获取工具列表并注册到 ToolRegistry
            tools = await mcp_client.list_tools(server_name)

            # 读取 visibility 配置
            tools_config = server_config.get("tools", {})

            for tool in tools:
                full_name = f"{server_name}/{tool.name}"
                registry._external_tools[full_name] = (server_name, tool.name)

                # visibility 从配置文件读取，未列出的工具默认 hidden
                tool_config = tools_config.get(tool.name, {})
                visibility = tool_config.get("visibility", "hidden")

                registry._schemas[full_name] = {
                    "name": full_name,
                    "description": tool.description,
                    "input_schema": tool.inputSchema,
                    "visibility": visibility,
                }
                registry._server_tools.setdefault(server_name, []).append(tool.name)

            logger.info(f"External MCP server {server_name} loaded: {len(tools)} tools")

        except Exception as e:
            logger.error(f"Failed to load external MCP server {server_name}: {e}")
