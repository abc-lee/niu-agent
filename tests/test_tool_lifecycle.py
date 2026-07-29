"""
ToolLifecycleManager tests — SKIPPED in disk mode.

tool_lifecycle.py has been deleted. MCP tools are now discovered via
the virtual disk (DiskEngine), not through decay/override scoring.
"""



def test_tool_lifecycle_removed():
    """Verify tool_lifecycle module no longer exists."""
    import importlib
    spec = importlib.util.find_spec("agent.tool_lifecycle")
    assert spec is None, "agent.tool_lifecycle should not exist in disk mode"
