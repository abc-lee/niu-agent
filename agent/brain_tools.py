"""
Brain Region MCP Tools — Tool schemas, handler functions, and singleton accessor.

Provides three MCP tools for manual brain region control:
- brain_region_activate: manually light up brain regions
- brain_region_dim: manually dim brain regions
- brain_region_status: show current brain region states

Also provides a singleton accessor for RegionActivationManager, and
a lazy tool_to_region mapping builder from LightRAG entities.

M5 module: MCP tools + API endpoints + tool dispatch reinforce.
"""

from __future__ import annotations

import logging
from typing import Any

from niu_api.internal.region_activation import RegionActivationManager

logger = logging.getLogger(__name__)

# ============== Singleton Accessor ==============

_activation_mgr: RegionActivationManager | None = None


def set_activation_mgr(mgr: RegionActivationManager) -> None:
    """Set the global RegionActivationManager singleton."""
    global _activation_mgr
    _activation_mgr = mgr
    logger.info("Brain tools: activation manager set")


def get_activation_mgr() -> RegionActivationManager | None:
    """Get the global RegionActivationManager singleton."""
    return _activation_mgr


# ============== Tool-to-Region Mapping ==============

_tool_to_region: dict[str, str] | None = None


def set_tool_to_region(mapping: dict[str, str]) -> None:
    """Set the tool_name -> region_id mapping."""
    global _tool_to_region
    _tool_to_region = mapping
    logger.info("Brain tools: tool-to-region mapping set (%d entries)", len(mapping))


def get_tool_to_region() -> dict[str, str]:
    """Get the tool_name -> region_id mapping.

    Lazily builds from LightRAG entities if not set.
    Returns empty dict if LightRAG is unavailable.
    """
    global _tool_to_region
    if _tool_to_region is not None:
        return _tool_to_region

    # Lazy build from LightRAG entities with entity_type="mcp_tool"
    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        result = adapter.list_entities(
            list_type="entities",
            entity_type="mcp_tool",
            limit=1000,
        )

        if isinstance(result, dict) and result.get("status") == "ok":
            mapping: dict[str, str] = {}
            for entity in result.get("data", []):
                entity_name = entity.get("id", entity.get("entity_name", ""))
                description = entity.get("description", "")
                # Extract community_id from description metadata
                # Format: "description | brain_meta_community_id:community_3 | ..."
                community_id = _extract_community_id(description)
                if community_id:
                    mapping[entity_name] = community_id

            _tool_to_region = mapping
            logger.info(
                "Lazily built tool-to-region mapping: %d entries",
                len(mapping),
            )
            return mapping
    except Exception as e:
        logger.debug("Failed to build tool-to-region mapping: %s", e)

    _tool_to_region = {}
    return _tool_to_region


def _extract_community_id(description: str) -> str:
    """Extract community_id from description metadata.

    Looks for 'brain_meta_community_id:xxx' pattern.
    """
    import re

    match = re.search(r"brain_meta_community_id:(\S+)", description)
    if match:
        return match.group(1)
    return ""


# ============== Tool Schemas ==============

BRAIN_REGION_ACTIVATE_SCHEMA = {
    "name": "brain_region_activate",
    "description": (
        "主动点亮一个或多个脑区，使其知识立即注入上下文。"
        "当你判断接下来的工作需要某个领域的知识时使用。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "regions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要点亮的脑区名称列表，如 ['编程开发', '项目管理']",
            },
            "reason": {
                "type": "string",
                "description": "为什么要点亮这些脑区（用于记忆记录）",
            },
        },
        "required": ["regions"],
    },
}

BRAIN_REGION_DIM_SCHEMA = {
    "name": "brain_region_dim",
    "description": (
        "主动关闭一个或多个脑区，停止注入其详细知识。"
        "当你确认某领域知识不再需要时使用，可节省上下文空间。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "regions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要关闭的脑区名称列表",
            },
        },
        "required": ["regions"],
    },
}

BRAIN_REGION_STATUS_SCHEMA = {
    "name": "brain_region_status",
    "description": (
        "Show current brain region activation states. "
        "Shows which regions are lit up and their activation levels."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "include_dark": {
                "type": "boolean",
                "description": "Include regions below activation threshold. Default: false",
            },
        },
    },
}

# All brain tool schemas for registration
BRAIN_TOOL_SCHEMAS = [
    BRAIN_REGION_ACTIVATE_SCHEMA,
    BRAIN_REGION_DIM_SCHEMA,
    BRAIN_REGION_STATUS_SCHEMA,
]


# ============== Tool Handler Functions ==============


def handle_brain_region_activate(params: dict[str, Any]) -> str:
    """Handle brain_region_activate tool call.

    Args:
        params: Tool call parameters with 'regions' and optional 'reason'.

    Returns:
        Formatted status string showing activated regions.
    """
    mgr = get_activation_mgr()
    if mgr is None:
        return "[Brain] Activation manager not initialized. Brain regions are not available."

    regions = params.get("regions", [])
    reason = params.get("reason", "")

    if not regions:
        return "[Brain] No regions specified. Use 'regions' parameter with a list of region names."

    # Call manual_activate on the manager
    mgr.manual_activate(regions)

    # Build formatted status
    lines: list[str] = []
    if reason:
        lines.append(f"[Brain] Activated regions (reason: {reason}):")
    else:
        lines.append("[Brain] Activated regions:")

    for label in regions:
        state = mgr.find_region_by_label(label)
        if state is not None:
            light = mgr.get_status_light(state.activation)
            lines.append(
                f"  {light} {state.label} — activation: {state.activation:.2f}"
            )
        else:
            lines.append(f"  [?] {label} — region not found")

    return "\n".join(lines)


def handle_brain_region_dim(params: dict[str, Any]) -> str:
    """Handle brain_region_dim tool call.

    Args:
        params: Tool call parameters with 'regions'.

    Returns:
        Formatted status string showing dimmed regions.
    """
    mgr = get_activation_mgr()
    if mgr is None:
        return "[Brain] Activation manager not initialized. Brain regions are not available."

    regions = params.get("regions", [])

    if not regions:
        return "[Brain] No regions specified. Use 'regions' parameter with a list of region names."

    # Call manual_dim on the manager
    mgr.manual_dim(regions)

    # Build formatted status
    lines: list[str] = []
    lines.append("[Brain] Dimmed regions:")

    for label in regions:
        state = mgr.find_region_by_label(label)
        if state is not None:
            light = mgr.get_status_light(state.activation)
            lines.append(
                f"  {light} {state.label} — activation: {state.activation:.2f} (dimmed)"
            )
        else:
            lines.append(f"  [?] {label} — region not found")

    return "\n".join(lines)


def handle_brain_region_status(params: dict[str, Any]) -> str:
    """Handle brain_region_status tool call.

    Args:
        params: Tool call parameters with optional 'include_dark'.

    Returns:
        Formatted status string showing region states.
    """
    mgr = get_activation_mgr()
    if mgr is None:
        return "[Brain] Activation manager not initialized. Brain regions are not available."

    include_dark = params.get("include_dark", False)

    all_states = mgr.get_region_map()

    if not all_states:
        return "[Brain] No brain regions initialized."

    # Filter by activation threshold unless include_dark
    if include_dark:
        states = sorted(all_states, key=lambda s: s.activation, reverse=True)
    else:
        states = sorted(
            [s for s in all_states if s.activation > 0.1],
            key=lambda s: s.activation,
            reverse=True,
        )

    if not states:
        return "[Brain] No active brain regions. Use include_dark=true to see all regions."

    # Build formatted status table
    lines: list[str] = []
    lines.append(f"[Brain] Region status ({len(states)} regions):")
    lines.append("  Status | Region          | Activation")
    lines.append("  -------+-----------------+-----------")

    for state in states:
        light = mgr.get_status_light(state.activation)
        dimmed_tag = " (dimmed)" if state.manually_dimmed else ""
        lines.append(
            f"  {light}     | {state.label:<15s} | {state.activation:.2f}{dimmed_tag}"
        )

    return "\n".join(lines)


# ============== Tool Dispatch Reinforce ==============


def reinforce_on_tool_use(tool_name: str) -> str | None:
    """Reinforce brain region when a tool is successfully called.

    Call this from handler.py after tool_lifecycle.hit_tool(tool_name).

    Args:
        tool_name: Name of the tool that was just called.

    Returns:
        The reinforced region_id, or None.
    """
    mgr = get_activation_mgr()
    if mgr is None:
        return None

    tool_to_region = get_tool_to_region()
    if not tool_to_region:
        return None

    return mgr.reinforce_by_tool_use(tool_name, tool_to_region)
