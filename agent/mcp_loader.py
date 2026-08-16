"""
MCP Tool Loader

Loads all required MCP modules at startup with strict validation.
Any failure to load critical MCP servers will terminate the application.
"""

import sys
from pathlib import Path

from loguru import logger

from agent.tool_registry import ToolRegistry, set_registry

# ============================================================================
# Required MCP Servers
# ============================================================================

REQUIRED_SERVERS: list[tuple[str, str]] = [
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

OPTIONAL_SERVERS: list[tuple[str, str]] = [
    ("ha-server", "niu_ha_server"),
]


# ============================================================================
# MCP 加载失败状态槽（E4-08/E4-16）
# ============================================================================
# 收集 (server_name, reason) 供前端 SSE 连接建立后拉取显示（拉取模式——不在启动
# 完成时推送：R2 B P3 实证推送必然先于前端订阅发出丢失，SSE 无重放缓冲）。
# 服务端保留至下次加载周期（load_mcp_tools 重新开始时清空）；
# 不随前端显示清除（R4 P1：清除则第二窗口/重连拉取恒空 = 静默丢失）。
_mcp_load_failures: list[dict] = []


def reset_mcp_load_failures() -> None:
    """清空失败状态槽——下一次加载周期开始时调用（保留至本次加载周期结束）"""
    _mcp_load_failures.clear()


def record_mcp_load_failure(server_name: str, reason: str) -> None:
    """记录一条 MCP 服务器加载失败（同 server+reason 去重）"""
    entry = {"server": server_name, "reason": reason}
    if entry not in _mcp_load_failures:
        _mcp_load_failures.append(entry)


def get_mcp_load_failures() -> list[dict]:
    """返回失败状态槽副本（查询不改变状态槽内容）"""
    return list(_mcp_load_failures)


def _fold_and_cap_reason(reason: str, limit: int = 200) -> str:
    """统一 reason 记录格式：换行折叠（\n → 空格）+ 保尾截断（对齐 E4 T2 verify_fail_reason 先例）。

    在记录端而非渲染端处理——状态槽内 reason 恒为单行且长度有界，
    渲染端直接展示无需再处理（保持状态槽原始语义）。防异常文本
    （如超长数据库错误消息/多行 traceback 文本）膨胀状态槽或破坏前端单行布局。
    """
    folded = reason.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if len(folded) > limit:
        return "..." + folded[-(limit - 3):]
    return folded


# ============================================================================
# Config Loader
# ============================================================================

def _load_mcp_config() -> dict:
    """Load MCP server configuration from ~/.niu/config/mcp-servers.yaml"""
    import yaml
    from niu_api.config import _get_mcp_servers_path

    config_path = Path(_get_mcp_servers_path())

    if not config_path.exists():
        logger.warning(f"MCP config file not found: {config_path}")
        return {}

    try:
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"Failed to load MCP config: {e}")
        return {}


def _add_server_workdirs_to_sys_path(config: dict) -> None:
    """Add server workdirs to sys.path for module imports"""
    project_root = Path(__file__).parent.parent

    for _server_name, server_config in config.items():
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

def _inject_tools_to_lightrag(registry: ToolRegistry, servers: list[tuple[str, str]]) -> None:
    """No-op in disk mode — tools discovered via disk YAML, not LightRAG."""
    logger.debug("[MCP Loader] Tool injection to LightRAG skipped (disk mode)")


# ============================================================================
# Loader Function
# ============================================================================

def load_mcp_tools(required_servers: list[tuple[str, str]] | None = None) -> ToolRegistry:
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

    # 新加载周期开始：清空失败状态槽（服务端保留至下次加载周期的边界）
    reset_mcp_load_failures()

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
        error_msg = "Critical MCP servers failed to load:\n" + "\n".join(
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
                # register_server 内部已收集失败（缺 get_tool_schemas/注册异常），不重复记录

        except ImportError as e:
            logger.debug(f"Optional server {server_name} not available: {e}")
            record_mcp_load_failure(server_name, _fold_and_cap_reason(f"模块不可用: {e}"))
        except Exception as e:
            logger.warning(f"Optional server {server_name} error: {e}")
            record_mcp_load_failure(server_name, _fold_and_cap_reason(f"加载异常: {e}"))

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
            record_mcp_load_failure(server_name, _fold_and_cap_reason(f"连接失败: {e}"))
