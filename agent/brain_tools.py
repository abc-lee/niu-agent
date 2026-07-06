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

logger = logging.getLogger(__name__)

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


def _get_graph_region_names() -> set[str]:
    """Bug 1: 查图拿到所有真实存在的 brainregion 实体名

    用于读路径差集过滤：缓存中有但图中没有的脑区 = 已删除但缓存未刷新。
    用 LightRAGAdapter 直接查 entity_type="brainregion"，避免顶层 import
    RegionManager 导致循环依赖。

    Returns:
        Set of region entity names (e.g. {"编程开发脑区", "项目管理脑区"}).
        Returns empty set on error or if adapter unavailable.
    """
    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        result = adapter.list_entities(
            list_type="entities",
            entity_type="brainregion",
            limit=1000,
        )
        if not isinstance(result, dict) or result.get("status") != "ok":
            return set()

        names: set[str] = set()
        for entity in result.get("data", []):
            name = entity.get("id", entity.get("entity_name", ""))
            if name:
                names.add(name)
        return names
    except Exception as e:
        logger.warning("查图拿脑区名失败: %s", e)
        return set()


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

    # Bug 1: 差集过滤 — 缓存有但图中没有的脑区 = 已删除但缓存未刷新
    # 主动 remove_region 清理缓存，避免下次查询还差集
    # 守卫：图查询失败（空集）或缓存与图无交集（数据不一致）时跳过过滤，
    # 避免误删测试/异常场景下的有效脑区
    graph_region_names = _get_graph_region_names()
    if graph_region_names:
        cached_ids = {s.region_id for s in all_states}
        # 缓存与图有交集才过滤（说明图查到了真实脑区，缓存中有部分匹配）
        if cached_ids & graph_region_names:
            stale_ids = cached_ids - graph_region_names
            for stale_id in stale_ids:
                try:
                    mgr.remove_region(stale_id)
                    logger.info("brain_region_status 清理幽灵脑区缓存: %s", stale_id)
                except Exception as e:
                    logger.warning("清理幽灵脑区缓存失败 %s: %s", stale_id, e)
            # 过滤掉已清理的脑区
            all_states = [s for s in all_states if s.region_id not in stale_ids]

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


def _reinforce_brain_region_edges(nx_graph, region_key: str) -> int:
    """增强脑区边权重 — 恢复到 INITIAL_WEIGHT（核心逻辑，供测试直接调用）

    只增强实体→脑区的归属边，跳过锚点边（脑区→脑区）和 _session: 前缀边。

    注意：增强不区分优先级 — "用一次就满血"（设计文档3.2节）。
    无论 permanent/long/medium/short，使用后都恢复到 INITIAL_WEIGHT。
    次日衰减从 1.0 重新按各脑区半衰期下降，相当于"遗忘计时器重置"。
    """
    from niu_api.internal.region_manager import INITIAL_WEIGHT

    if region_key not in nx_graph:
        return 0

    reinforced = 0
    for entity_key in list(nx_graph.neighbors(region_key)):
        # 跳过锚点边（脑区→脑区）
        if nx_graph.nodes[entity_key].get("entity_type") == "brainregion":
            continue
        # 跳过 _session: 前缀边（会话临时边，不参与增强）
        edge_data = nx_graph.edges[region_key, entity_key]
        keywords = edge_data.get("keywords") or edge_data.get("type", "")
        if keywords.lower().startswith("_session:"):
            continue

        old_weight = edge_data.get("weight", INITIAL_WEIGHT)

        if old_weight < INITIAL_WEIGHT:
            nx_graph.edges[region_key, entity_key]["weight"] = INITIAL_WEIGHT
            reinforced += 1

    if reinforced > 0:
        logger.debug(f"[Reinforce] region={region_key}: {reinforced} edges restored to {INITIAL_WEIGHT}")

    return reinforced


def reinforce_on_tool_use(tool_name: str) -> str | None:
    """Reinforce brain region when a tool is successfully called.

    Also restores weight of structural edges in the
    LightRAG knowledge graph to INITIAL_WEIGHT, creating a dynamic balance with edge decay.

    Call this from handler.py after a tool is successfully dispatched.

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

    region_id = mgr.reinforce_by_tool_use(tool_name, tool_to_region)

    # Restore structural edge weights to INITIAL_WEIGHT — balances with decay
    if region_id:
        _reinforce_edge_weight(region_id)

    return region_id


def _reinforce_edge_weight(region_id: str) -> int:
    """增强脑区边权重 — 恢复到 INITIAL_WEIGHT（包装函数）

    内部获取 nx_graph，调用 _reinforce_brain_region_edges。
    图引用获取在锁内，避免与衰减线程竞争。
    """
    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter
        from niu_api.internal.lightrag_manager import graph_write_lock

        adapter = LightRAGAdapter()
        region_key = region_id.lower() if isinstance(region_id, str) else region_id

        with graph_write_lock():
            rag = adapter._get_rag()
            if rag is None:
                return 0

            kg = rag.chunk_entity_relation_graph
            if kg is None:
                return 0

            nx_graph = kg._graph if hasattr(kg, "_graph") else kg
            if nx_graph is None:
                return 0

            return _reinforce_brain_region_edges(nx_graph, region_key)

    except Exception as e:
        logger.warning("Edge reinforce failed: %s", e)
        return 0
