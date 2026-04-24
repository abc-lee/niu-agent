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
    """Inject MCP tool descriptions into the LightRAG knowledge graph.

    After tools are registered in ToolRegistry, also inject each tool
    as an entity with entity_type="tool" into LightRAG so that
    LightRAGAdapter.search_tools() can find them.

    Wrapped in try/except so LightRAG failures never break startup.
    """
    try:
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()

        for server_name, _module_name in servers:
            # Get tool schemas for this server from the registry
            for full_name, schema in registry.get_all_schemas().items():
                # Only process tools belonging to this server
                # full_name format: "server-name/tool-name"
                if "/" not in full_name:
                    continue
                parts = full_name.split("/", 1)
                if parts[0] != server_name:
                    continue

                tool_name = parts[1]
                description = schema.get("description", "")
                if not description:
                    continue

                try:
                    result = ingester.inject_entity(
                        name=f"tool:{full_name}",
                        entity_type="tool",
                        description=description,
                        source_id=f"tool:{full_name}",
                        chunk_content=f"{tool_name}: {description}",
                        file_path=f"mcp://{full_name}",
                    )
                    if result.get("status") == "ok":
                        logger.debug(f"[MCP Loader] Injected tool '{full_name}' into LightRAG")
                except Exception as e:
                    logger.debug(f"[MCP Loader] LightRAG inject failed for tool '{full_name}': {e}")

        logger.info("[MCP Loader] Tool descriptions injected into LightRAG")
    except Exception as e:
        # LightRAG not available — non-fatal, startup continues
        logger.debug(f"[MCP Loader] LightRAG tool inject skipped: {e}")


# ============================================================================
# Brain Region Tool Registration
# ============================================================================

def _register_brain_tools(registry: ToolRegistry) -> None:
    """Register brain region MCP tools with ToolRegistry.

    These are built-in tools (no MCP server module) that wrap
    RegionActivationManager for manual brain region control.
    """
    try:
        from agent.brain_tools import (
            BRAIN_TOOL_SCHEMAS,
            handle_brain_region_activate,
            handle_brain_region_dim,
            handle_brain_region_status,
        )

        handlers = {
            "brain_region_activate": handle_brain_region_activate,
            "brain_region_dim": handle_brain_region_dim,
            "brain_region_status": handle_brain_region_status,
        }

        for schema in BRAIN_TOOL_SCHEMAS:
            name = schema["name"]
            handler = handlers.get(name)
            if handler:
                registry.register(name, handler, schema, visibility="static")
            else:
                logger.warning(f"Brain tool schema '{name}' has no handler")

        logger.info("[MCP Loader] Brain region tools registered")
    except Exception as e:
        logger.warning(f"[MCP Loader] Brain region tool registration failed: {e}")


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

    # Set global registry instance
    set_registry(registry)

    # lightrag-query is now provided by lightrag-server MCP module
    # (lightrag-server/lightrag_query). No separate built-in registration needed.

    # Inject MCP tool descriptions into LightRAG knowledge graph
    # so that LightRAGAdapter.search_tools() can find them
    _inject_tools_to_lightrag(registry, servers)

    # Register brain region MCP tools (brain_region_activate, brain_region_dim, brain_region_status)
    _register_brain_tools(registry)

    return registry
