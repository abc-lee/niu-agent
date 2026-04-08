"""
MCP Tool Loader

Loads all required MCP modules at startup with strict validation.
Any failure to load critical MCP servers will terminate the application.
"""

from typing import List, Tuple
from agent.tool_registry import ToolRegistry


# ============================================================================
# Required MCP Servers
# ============================================================================

REQUIRED_SERVERS: List[Tuple[str, str]] = [
    ("photo-server", "niu_photo_server"),
    ("config-manager", "niu_config_manager"),
    ("memory-server", "niu_memory_server"),
    ("vector-store", "niu_vector_store"),
    ("kg-server", "niu_kg_server"),
    ("file-parser", "niu_file_parser"),
    ("session-manager", "niu_session_manager"),
    ("scheduler-server", "niu_scheduler_server"),
]


# ============================================================================
# Loader Function
# ============================================================================

def load_mcp_tools() -> ToolRegistry:
    """
    Load all required MCP modules and register their tools.

    Returns:
        ToolRegistry: Registry containing all MCP tool functions and schemas.

    Raises:
        RuntimeError: If any required MCP server fails to load.
    """
    registry = ToolRegistry()
    failed_servers = []

    for server_name, module_name in REQUIRED_SERVERS:
        try:
            module = __import__(module_name, fromlist=["get_tool_schemas"])

            if not registry.register_server(server_name, module):
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

    print(f"[MCP Loader] All {len(REQUIRED_SERVERS)} servers loaded")

    return registry
