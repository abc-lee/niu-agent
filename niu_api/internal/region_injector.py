"""
Brain Region Context Injector

Injects brain region context into the system prompt based on activation levels.
Provides layered injection: region map (always), detailed content (high activation),
summary content (mid activation), and activation-weighted search result boosting.

M4 module: Context injection, M1-M3 provide detection, node management, and activation.
"""

from __future__ import annotations

import logging
from typing import Any

from niu_api.internal.region_activation import (
    BrainRegionState,
    RegionActivationManager,
    STATUS_DIMMING,
    STATUS_LIT,
    STATUS_OFF,
)
from niu_api.internal.region_manager import RegionManager

logger = logging.getLogger(__name__)

# ============== Constants ==============

# Namespace prefix for brain region entities (must match region_manager)
REGION_PREFIX = "brain:region:"

# Chars per token estimate for Chinese text
CHARS_PER_TOKEN = 4

# Maximum knowledge snippets in detailed region output
MAX_KNOWLEDGE_SNIPPETS = 3


class BrainContextInjector:
    """Brain region activation-weighted context injection

    Takes a query, activates relevant brain regions, and returns formatted
    injection text for the system prompt. Three layers of injection:

    1. Region map (always injected): status lights for all regions
    2. Detailed content (activation > 0.7): entities + relations + snippets
    3. Summary content (0.3 < activation <= 0.7): brief summary

    Usage::

        injector = BrainContextInjector(adapter, activation_mgr, region_mgr)
        injection_text = injector.inject_brain_context("Python数据分析")
    """

    CONTEXT_BUDGET = {
        "total": 4000,
        "high_activation": 2000,
        "mid_activation": 1200,
        "low_activation": 400,
        "skills": 400,
    }

    def __init__(
        self,
        adapter: Any,  # LightRAGAdapter
        activation_mgr: RegionActivationManager,
        region_mgr: RegionManager,
    ) -> None:
        self._adapter = adapter
        self._activation_mgr = activation_mgr
        self._region_mgr = region_mgr

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def inject_brain_context(
        self,
        query_context: str,
    ) -> str:
        """Main entry: activate regions + get layered injection content

        Steps:
        1. Use query_context to do LightRAG query -> extract hit entities
        2. activation_mgr.activate_regions(hit_entities, entity_to_region)
        3. If hit brain:region:* master node -> secondary local query to expand
        4. activation_mgr.decay_all()
        5. Format injection content by activation level

        Returns injection text (empty string if no active regions or on error).

        Note: This method is synchronous — all adapter calls (query_data, query)
        are sync wrappers that internally handle their own async bridging via
        call_async. Declaring this as async would cause nested call_async deadlock.
        """
        if not query_context:
            return ""

        # Step 1: Query LightRAG to find hit entities
        hit_entities: list[str] = []
        region_knowledge: dict[str, str] = {}  # region_label -> knowledge text

        try:
            query_result = self._adapter.query_data(
                query_context, mode="local", top_k=20
            )

            if query_result and isinstance(query_result, dict):
                data = query_result.get("data", {})
                if not data:
                    data = query_result
                entities = data.get("entities", [])
                hit_entities = [
                    e.get("entity_name", e.get("id", ""))
                    for e in entities
                    if e.get("entity_name") or e.get("id")
                ]
        except Exception as e:
            logger.warning("脑区注入查询失败: %s", e)

        # Step 2: Activate regions based on hit entities
        entity_to_region = self._activation_mgr.get_entity_to_region_map()
        self._activation_mgr.activate_regions(
            hit_entities, entity_to_region
        )

        # Step 3: Expand region master node knowledge
        for entity in hit_entities:
            if entity.startswith(REGION_PREFIX):
                try:
                    knowledge = self._adapter.query(
                        entity, mode="local", only_need_context=True
                    )
                    if knowledge and isinstance(knowledge, str):
                        # Extract label from "brain:region:{label}"
                        label = entity[len(REGION_PREFIX):]
                        region_knowledge[label] = knowledge
                except Exception as e:
                    logger.debug("脑区知识扩展查询失败: %s — %s", entity, e)

        # Step 4: Decay all regions
        self._activation_mgr.decay_all()

        # Step 5: Format injection content by activation level
        return self._format_injection_content(region_knowledge)

    # ------------------------------------------------------------------
    # Formatting: region map (always injected)
    # ------------------------------------------------------------------

    def format_region_map(
        self,
        regions: list[BrainRegionState],
    ) -> str:
        """Region map (always injected, ~150-200 tokens)

        Format:
        ## 脑区状态 (N个脑区)
        🟢 编程开发 — Python/NumPy/Web技术栈，你擅长编程 (6实体)
        🟢 项目管理 — AI_Bot项目，你是主开发者 (4实体)
        🟡 日常偏好 — 你偏好暗色主题，远程办公 (3实体)
        ⚫ 财务知识 — 报销流程、预算审批 (2实体)
        """
        if not regions:
            return ""

        lines = [f"## 脑区状态 ({len(regions)}个脑区)"]

        # Sort: lit first, then dimming, then off; within same status, by label
        status_order = {STATUS_LIT: 0, STATUS_DIMMING: 1, STATUS_OFF: 2}
        sorted_regions = sorted(
            regions,
            key=lambda r: (
                status_order.get(
                    self._activation_mgr.get_status_light(r.activation), 3
                ),
                r.label,
            ),
        )

        for region in sorted_regions:
            light = self._activation_mgr.get_status_light(region.activation)
            # Get member count from region_id if available
            member_count = self._get_member_count(region.region_id)
            lines.append(
                f"{light} {region.label} ({member_count}实体)"
            )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Formatting: detailed region (high activation > 0.7)
    # ------------------------------------------------------------------

    def format_detailed_region(
        self,
        region: BrainRegionState,
        members: list[str],
        budget: int,
        knowledge: str = "",
    ) -> str:
        """High activation (>0.7): inject entities + relations + doc snippets

        Format:
        ### [编程开发] (活跃)
        实体: Python(expert), NumPy, Data_Analysis, Web_Development
        关系:
        - 你擅长Python(expert级别)，从2019年开始用于AI/ML
        - Python与NumPy通过数据科学生态关联
        知识: [相关文档片段，最多3条]

        Strictly control within budget tokens, truncate low-priority content.
        Truncation order: knowledge snippets -> relations -> entities.
        """
        budget_chars = budget * CHARS_PER_TOKEN

        # Header
        header = f"### [{region.label}] (活跃)"
        header_chars = len(header)

        # Entity line
        entity_text = ", ".join(members) if members else "(无实体)"
        entity_line = f"实体: {entity_text}"

        # Knowledge snippets (max 3)
        knowledge_line = ""
        if knowledge:
            snippets = knowledge.split("\n")
            top_snippets = [s.strip() for s in snippets[:MAX_KNOWLEDGE_SNIPPETS] if s.strip()]
            if top_snippets:
                knowledge_line = "知识: " + "; ".join(top_snippets)

        # Build content, applying budget control
        parts = [header, entity_line]
        current_chars = header_chars + len(entity_line)

        if knowledge_line:
            if current_chars + len(knowledge_line) <= budget_chars:
                parts.append(knowledge_line)
                current_chars += len(knowledge_line)
            else:
                # Truncate knowledge: fill remaining budget (account for "..." suffix)
                remaining = budget_chars - current_chars - len("知识: ") - len("...")
                if remaining > 20:
                    truncated = knowledge_line[len("知识: "):][:remaining] + "..."
                    parts.append(f"知识: {truncated}")
                    current_chars += len(f"知识: {truncated}")

        # Account for newline separators between parts
        current_chars += max(len(parts) - 1, 0)

        # Check if entity line itself exceeds budget (truncate entities)
        if current_chars > budget_chars:
            # Truncate entity list to fit budget
            available = budget_chars - header_chars - len("实体: ") - len("...")
            if available > 0:
                truncated_members = entity_text[:available] + "..."
                parts = [header, f"实体: {truncated_members}"]
            else:
                parts = [header]

        result = "\n".join(parts)
        return result

    # ------------------------------------------------------------------
    # Formatting: summary region (mid activation 0.3-0.7)
    # ------------------------------------------------------------------

    def format_summary_region(
        self,
        region: BrainRegionState,
    ) -> str:
        """Mid activation (0.3-0.7): inject summary

        Format:
        ### [项目管理] (近期)
        你在参与AI_Bot项目，是主开发者。项目使用Python/Web技术栈。
        """
        description = self._get_region_description(region.region_id)
        if not description:
            description = f"相关区域，包含{self._get_member_count(region.region_id)}个实体"

        return f"### [{region.label}] (近期)\n{description}"

    # ------------------------------------------------------------------
    # Activation-weighted result boosting
    # ------------------------------------------------------------------

    def apply_activation_weight(
        self,
        query_results: list[dict],
        boost_factor: float = 0.3,
    ) -> list[dict]:
        """Activation-weighted query results

        Entities in activated regions get boosted scores:
        final_score = lightrag_score + region.activation * boost_factor

        Results sorted by final_score descending.
        """
        if not query_results:
            return []

        entity_to_region = self._activation_mgr.get_entity_to_region_map()
        boosted: list[dict] = []

        for result in query_results:
            # Copy to avoid mutation
            boosted_result = dict(result)

            # Get entity name from result
            entity_name = result.get("entity_name", result.get("id", ""))
            original_score = result.get("score", 0.0)

            # Find which region this entity belongs to
            region_id = entity_to_region.get(entity_name)
            boost = 0.0
            if region_id:
                state = self._activation_mgr.get_region_state(region_id)
                if state:
                    boost = state.activation * boost_factor

            boosted_result["score"] = original_score + boost
            boosted.append(boosted_result)

        # Sort by final score descending
        boosted.sort(key=lambda r: r.get("score", 0.0), reverse=True)
        return boosted

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _format_injection_content(
        self,
        region_knowledge: dict[str, str],
    ) -> str:
        """Format the full injection content based on current activation levels."""
        all_regions = self._activation_mgr.get_region_map()

        if not all_regions:
            return ""

        parts: list[str] = []

        # Always inject: region map
        region_map = self.format_region_map(all_regions)
        if region_map:
            parts.append(region_map)

        # Separate regions by activation level
        high_regions = [r for r in all_regions if r.activation > 0.7]
        mid_regions = [r for r in all_regions if 0.3 < r.activation <= 0.7]

        # Sort by activation descending
        high_regions.sort(key=lambda r: r.activation, reverse=True)
        mid_regions.sort(key=lambda r: r.activation, reverse=True)

        # High activation: detailed content
        if high_regions:
            parts.append("")  # blank line separator
            high_budget = self.CONTEXT_BUDGET["high_activation"]
            per_region_budget = max(
                high_budget // max(len(high_regions), 1), 200
            )

            for region in high_regions:
                members = self._get_members(region.region_id)
                knowledge = region_knowledge.get(region.label, "")
                detailed = self.format_detailed_region(
                    region, members, per_region_budget, knowledge
                )
                if detailed:
                    parts.append(detailed)

        # Mid activation: summary content
        if mid_regions:
            parts.append("")  # blank line separator
            for region in mid_regions:
                summary = self.format_summary_region(region)
                if summary:
                    parts.append(summary)

        return "\n".join(parts)

    def _get_members(self, region_id: str) -> list[str]:
        """Get member entity names for a region from activation manager."""
        return self._activation_mgr.get_members_of_region(region_id)

    def _get_member_count(self, region_id: str) -> int:
        """Get member count for a region."""
        return len(self._get_members(region_id))

    def _get_region_description(self, region_id: str) -> str:
        """Get region description from activation manager.

        Delegates to RegionActivationManager.get_region_description() which
        stores descriptions from BrainRegionInfo.
        """
        return self._activation_mgr.get_region_description(region_id)
