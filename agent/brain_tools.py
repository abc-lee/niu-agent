"""
Brain Region MCP Tools — Tool schemas, handler functions, and singleton accessor.

Provides three MCP tools for manual brain region control:
- brain_region_activate: manually light up brain regions
- brain_region_dim: manually dim brain regions
- brain_region_status: show current brain region states

Also provides a singleton accessor for RegionActivationManager.
"""

from __future__ import annotations

import logging
import threading

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
