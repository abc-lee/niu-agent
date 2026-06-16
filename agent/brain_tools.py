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
import threading
from typing import TYPE_CHECKING

from niu_api.internal.region_activation import RegionActivationManager
from niu_api.internal.region_manager import ANCHOR_RELATION, STRUCTURAL_EDGE_TYPES_LOWER

logger = logging.getLogger(__name__)

# ============== Edge Weight Constants ==============

REINFORCE_DELTA = 0.15  # Edge weight boost per reinforcement (was 0.1)
MAX_EDGE_WEIGHT = 2.0   # Maximum edge weight allowed (was 1.0)

# ============== Singleton Accessor ==============

_activation_mgr: RegionActivationManager | None = None
_activation_mgr_lock = threading.Lock()


def set_activation_mgr(mgr: RegionActivationManager) -> None:
    """Set the global RegionActivationManager singleton."""
    global _activation_mgr
    with _activation_mgr_lock:
        _activation_mgr = mgr
    logger.info("Brain tools: activation manager set")


def get_activation_mgr() -> RegionActivationManager | None:
    """Get the global RegionActivationManager singleton."""
    with _activation_mgr_lock:
        return _activation_mgr


# ============== Tool-to-Region Mapping ==============

_tool_to_region: dict[str, str] | None = None
_tool_to_region_lock = threading.Lock()


def invalidate_tool_to_region() -> None:
    """Invalidate the cached tool_to_region mapping (called after region changes)."""
    global _tool_to_region
    with _tool_to_region_lock:
        _tool_to_region = None


def set_tool_to_region(mapping: dict[str, str]) -> None:
    """Set the tool_name -> region_id mapping."""
    global _tool_to_region
    with _tool_to_region_lock:
        _tool_to_region = mapping
    logger.info("Brain tools: tool-to-region mapping set (%d entries)", len(mapping))


def get_tool_to_region() -> dict[str, str]:
    """Get the tool_name -> region_id mapping.

    Lazily builds from LightRAG entities if not set.
    Returns empty dict if LightRAG is unavailable.
    """
    global _tool_to_region
    with _tool_to_region_lock:
        if _tool_to_region is not None:
            return _tool_to_region

        # Lazy build from LightRAG entities with entity_type="Tool"
        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter

            adapter = LightRAGAdapter()
            result = adapter.list_entities(
                list_type="entities",
                entity_type="Tool",
                limit=1000,
            )

            if isinstance(result, dict) and result.get("status") == "ok":
                cid_mapping: dict[str, str] = {}  # tool_name -> community_id
                for entity in result.get("data", []):
                    entity_name = entity.get("id", entity.get("entity_name", ""))
                    description = entity.get("description", "")
                    # Extract community_id from description metadata
                    # Format: "description | brain_meta_region_id:community_3 | ..."
                    community_id = _extract_community_id(description)
                    if community_id:
                        cid_mapping[entity_name] = community_id

                # Translate community_id -> region.name (the _regions dict key)
                # so reinforce_by_tool_use can find regions correctly
                activation_mgr = get_activation_mgr()
                if activation_mgr is not None:
                    # Build community_id -> region_id reverse lookup
                    cid_to_rid: dict[str, str] = {}
                    for state in activation_mgr.get_region_map():
                        if state.community_id:
                            cid_to_rid[state.community_id] = state.region_id
                    mapping = {
                        tool: cid_to_rid.get(cid, cid)
                        for tool, cid in cid_mapping.items()
                    }
                else:
                    # Fallback: keep community_id values (will be rebuilt later)
                    mapping = cid_mapping

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
    """Extract region_id from description metadata.

    Looks for 'brain_meta_region_id:xxx' pattern.
    """
    import re

    match = re.search(r"brain_meta_region_id:(\S+)", description)
    if match:
        return match.group(1)
    return ""


# ============== Tool Schemas ==============

BRAIN_REGION_ACTIVATE_SCHEMA = {
    "name": "brain_region/activate",
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
    "name": "brain_region/dim",
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
            "reason": {
                "type": "string",
                "description": "为什么要关闭这些脑区（用于记忆记录）",
            },
        },
        "required": ["regions"],
    },
}

BRAIN_REGION_STATUS_SCHEMA = {
    "name": "brain_region/status",
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


def handle_brain_region_activate(regions: list[str], reason: str = "") -> str:
    """Handle brain_region_activate tool call.

    Args:
        regions: List of region names to activate.
        reason: Optional reason for activation (for memory logging).

    Returns:
        Formatted status string showing activated regions.
    """
    mgr = get_activation_mgr()
    if mgr is None:
        return "[Brain] Activation manager not initialized. Brain regions are not available."

    if not regions:
        return "[Brain] No regions specified. Use 'regions' parameter with a list of region names."

    # Call manual_activate on the manager, get activated region_ids
    activated_ids = mgr.manual_activate(regions)

    # Build formatted status using the returned set of activated region_ids
    lines: list[str] = []
    if reason:
        lines.append(f"[Brain] Activated regions (reason: {reason}):")
    else:
        lines.append("[Brain] Activated regions:")

    for region_id in activated_ids:
        state = mgr.get_region_state(region_id)
        if state is not None:
            light = mgr.get_status_light(state.activation)
            lines.append(
                f"  {light} {state.label} — activation: {state.activation:.2f}"
            )
        else:
            lines.append(f"  [?] {region_id} — region state missing")

    # Also report labels that were not found
    found_labels = set()
    for region_id in activated_ids:
        state = mgr.get_region_state(region_id)
        if state is not None:
            found_labels.add(state.label)
    for label in regions:
        if label not in found_labels:
            lines.append(f"  [?] {label} — region not found")

    return "\n".join(lines)


def handle_brain_region_dim(regions: list[str], reason: str = "") -> str:
    """Handle brain_region_dim tool call.

    Args:
        regions: List of region names to dim.
        reason: Optional reason for dimming (for memory logging).

    Returns:
        Formatted status string showing dimmed regions.
    """
    mgr = get_activation_mgr()
    if mgr is None:
        return "[Brain] Activation manager not initialized. Brain regions are not available."

    if not regions:
        return "[Brain] No regions specified. Use 'regions' parameter with a list of region names."

    # Call manual_dim on the manager
    mgr.manual_dim(regions, reason=reason)

    # Build formatted status
    lines: list[str] = []
    if reason:
        lines.append(f"[Brain] Dimmed regions (reason: {reason}):")
    else:
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


def handle_brain_region_status(include_dark: bool = False) -> str:
    """Handle brain_region_status tool call.

    Args:
        include_dark: Whether to include regions below activation threshold.

    Returns:
        Formatted status string showing region states.
    """
    mgr = get_activation_mgr()
    if mgr is None:
        return "[Brain] Activation manager not initialized. Brain regions are not available."

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


def reinforce_on_tool_use(tool_name: str, reinforce_delta: float = REINFORCE_DELTA) -> str | None:
    """Reinforce brain region when a tool is successfully called.

    Also boosts weight of structural edges (_region: prefix) in the
    LightRAG knowledge graph, creating a dynamic balance with edge decay.

    Call this from handler.py after a tool is successfully dispatched.

    Args:
        tool_name: Name of the tool that was just called.
        reinforce_delta: Edge weight boost value (default REINFORCE_DELTA=0.15).

    Returns:
        The reinforced region_id, or None.
    """
    mgr = get_activation_mgr()
    if mgr is None:
        return None

    tool_to_region = get_tool_to_region()
    if not tool_to_region:
        return None

    region_id = mgr.reinforce_by_tool_use(tool_name, tool_to_region)

    # Boost structural edge weights in LightRAG graph
    if region_id:
        _reinforce_edge_weight(region_id, reinforce_delta)

    return region_id


def _reinforce_edge_weight(region_id: str, delta: float = REINFORCE_DELTA) -> None:
    """Boost weight of structural edges for a brain region node.

    Directly operates on the internal NetworkX graph under write lock,
    following the same pattern as decay_structural_edges.
    """
    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter
        from niu_api.internal.lightrag_manager import graph_write_lock

        adapter = LightRAGAdapter()
        rag = adapter._get_rag()
        if rag is None:
            return

        kg = rag.chunk_entity_relation_graph
        if kg is None:
            return

        nx_graph = kg._graph if hasattr(kg, "_graph") else kg
        if nx_graph is None:
            return

        region_key = region_id.lower() if isinstance(region_id, str) else region_id

        with graph_write_lock():
            if region_key not in nx_graph:
                return

            for neighbor_id in list(nx_graph.neighbors(region_key)):
                edge_data = nx_graph.get_edge_data(region_key, neighbor_id)
                if edge_data is None:
                    continue
                keywords = edge_data.get("keywords") or edge_data.get("type", "")
                kw_lower = keywords.lower()
                # 锚点边是永久结构边，不需要强化
                if kw_lower == ANCHOR_RELATION.lower():
                    continue
                if kw_lower in STRUCTURAL_EDGE_TYPES_LOWER or kw_lower.startswith("_session:"):
                    old_weight = float(edge_data.get("weight", 0.5))
                    new_weight = min(MAX_EDGE_WEIGHT, old_weight + delta)
                    if new_weight > old_weight:
                        edge_data["weight"] = new_weight
                        logger.debug(
                            "Edge weight reinforced: %s -> %s (%s): %.2f -> %.2f",
                            region_key, neighbor_id, keywords, old_weight, new_weight,
                        )
    except Exception as e:
        logger.debug("Edge weight reinforce failed: %s", e)
