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

from niu_api.internal.lightrag_manager import get_all_region_members
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

# Namespace prefix for brain region entities (backward compat: read old-format names)
REGION_PREFIX = "brain:region:"
# New naming convention: label + suffix
REGION_SUFFIX = "脑区"

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

    def activate_for_query(
        self,
        query_context: str,
    ) -> tuple[dict[str, str], dict[str, str], list[str]]:
        """Step 1-3: Get entity mapping, vector-search, activate regions.

        Returns:
            (region_knowledge, entity_to_region, hit_entities) for use by format_injection_text().
        """
        if not query_context:
            return {}, {}, []

        region_members = get_all_region_members()
        entity_to_region: dict[str, str] = {}
        for region_name, members in region_members.items():
            for member in members:
                entity_to_region[member] = region_name

        hit_entities: list[str] = []
        region_knowledge: dict[str, str] = {}

        try:
            query_result = self._adapter.query_data(
                query_context, mode="local", top_k=20, keywords=[query_context]
            )
            if query_result and isinstance(query_result, dict):
                data = query_result.get("data", {})
                if not data:
                    data = query_result
                entities = data.get("entities", [])
                logger.info(
                    "脑区注入: query_data返回 %d 个实体, query=%s",
                    len(entities), query_context[:30],
                )
                for entity in entities:
                    entity_name = entity.get("entity_name", entity.get("id", ""))
                    entity_type = entity.get("entity_type", "")
                    if entity_name:
                        hit_entities.append(entity_name)
                        region_name = entity_to_region.get(entity_name)
                        if not region_name:
                            region_name = self._classify_entity_to_region(entity_name, entity_type)
                            if region_name:
                                entity_to_region[entity_name] = region_name
                        if region_name and region_name not in region_knowledge:
                            desc = entity.get("description", "")
                            if desc:
                                region_knowledge[region_name] = desc
        except Exception as e:
            logger.warning("脑区注入向量检索失败: %s", e)

        self._activation_mgr.activate_regions(
            hit_entities, entity_to_region
        )

        return region_knowledge, entity_to_region, hit_entities

    def format_injection_text(
        self,
        region_knowledge: dict[str, str],
        entity_to_region: dict[str, str] | None = None,
        hit_entities: list[str] | None = None,
    ) -> str:
        """Step 4: Format injection content by activation level."""
        return self._format_injection_content(region_knowledge, entity_to_region, hit_entities)

    def inject_brain_context(
        self,
        query_context: str,
    ) -> str:
        """Convenience: activate + format in one call."""
        region_knowledge, entity_to_region, hit_entities = self.activate_for_query(query_context)
        return self.format_injection_text(region_knowledge, entity_to_region, hit_entities)

    # ------------------------------------------------------------------
    # Formatting: region map (always injected)
    # ------------------------------------------------------------------

    def format_region_map(
        self,
        regions: list[BrainRegionState],
        region_members_map: dict[str, list[str]] | None = None,
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
            member_count = self._get_member_count(region.region_id)
            description = self._get_region_description(region.region_id)
            if description:
                short_desc = description[:30] + ("..." if len(description) > 30 else "")
                lines.append(
                    f"{light} {region.label} — {short_desc} ({member_count}实体)"
                )
            else:
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
            cleaned_knowledge = knowledge.replace("<SEP>", "\n")
            snippets = [s.strip() for s in cleaned_knowledge.split("\n") if s.strip()]
            top_snippets = snippets[:MAX_KNOWLEDGE_SNIPPETS]
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
        member_count: int | None = None,
    ) -> str:
        """Mid activation (0.3-0.7): inject summary

        Format:
        ### [项目管理] (近期)
        你在参与AI_Bot项目，是主开发者。项目使用Python/Web技术栈。
        """
        description = self._get_region_description(region.region_id)
        if not description:
            count = member_count if member_count is not None else self._get_member_count(region.region_id)
            description = f"相关区域，包含{count}个实体"

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

    def _classify_entity_to_region(self, entity_name: str, entity_type: str) -> str:
        """根据实体类型运行时分类到默认脑区（不写回图谱）

        当实体没有 _region:contains 边（即不在 entity_to_region 映射中）时，
        根据其 entity_type 做简单分类，让脑区注入先能工作起来。
        这只是注入时的运行时分类，不写回图谱（不改数据）。

        Args:
            entity_name: 实体名称（未使用，保留用于未来扩展）
            entity_type: 实体类型字符串

        Returns:
            脑区名称字符串
        """
        et = (entity_type or "").lower()
        # 聊天历史
        if et in ("chat", "chatmessage", "session", "conversation", "dialog",
                  "对话", "聊天", "会话"):
            return "聊天历史脑区"
        # 文档库
        if et in ("document", "文档", "file", "pdf", "note", "markdown",
                  "presentation", "spreadsheet", "text"):
            return "文档库脑区"
        # 人际关系
        if et in ("person", "人物", "people"):
            return "人际关系脑区"
        # 工作事务
        if et in ("project", "task", "meeting", "decision", "issue",
                  "milestone", "organization", "company",
                  "项目", "任务", "会议"):
            return "工作事务脑区"
        # 生活事务
        if et in ("health", "finance", "travel", "event", "location",
                  "place", "activity",
                  "健康", "财务", "旅行"):
            return "生活事务脑区"
        # 知识体系 (default)
        return "知识体系脑区"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _format_injection_content(
        self,
        region_knowledge: dict[str, str],
        entity_to_region: dict[str, str] | None = None,
        hit_entities: list[str] | None = None,
    ) -> str:
        """Format the full injection content based on current activation levels."""
        all_regions = self._activation_mgr.get_region_map()

        if not all_regions:
            return ""

        # Build region_id -> members from entity_to_region if provided
        region_members_map: dict[str, list[str]] | None = None
        if hit_entities and entity_to_region:
            # 向量检索命中实体：只用 Top N，而非脑区全部成员
            region_members_map = {}
            for entity in hit_entities:
                rid = entity_to_region.get(entity)
                if rid:
                    region_members_map.setdefault(rid, []).append(entity)
        elif entity_to_region:
            # fallback: 无 hit_entities 时用全部成员（兼容旧调用方式）
            region_members_map = {}
            for entity, rid in entity_to_region.items():
                region_members_map.setdefault(rid, []).append(entity)

        parts: list[str] = []

        # Always inject: region map
        region_map = self.format_region_map(all_regions, region_members_map)
        if region_map:
            parts.append(region_map)

        # Separate regions by activation level
        high_regions = [r for r in all_regions if r.activation > 0.7]
        mid_regions = [r for r in all_regions if 0.3 < r.activation <= 0.7]

        logger.info(
            "脑区注入格式化: total=%d, high=%d, mid=%d, knowledge_keys=%s",
            len(all_regions), len(high_regions), len(mid_regions),
            list(region_knowledge.keys()),
        )

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
                if region_members_map:
                    members = region_members_map.get(region.region_id, [])
                else:
                    members = self._get_members(region.region_id)
                knowledge = region_knowledge.get(region.region_id, "")
                detailed = self.format_detailed_region(
                    region, members, per_region_budget, knowledge
                )
                if detailed:
                    parts.append(detailed)

        # Mid activation: summary content
        if mid_regions:
            parts.append("")  # blank line separator
            for region in mid_regions:
                if region_members_map:
                    mc = len(region_members_map.get(region.region_id, []))
                else:
                    mc = None
                summary = self.format_summary_region(region, member_count=mc)
                if summary:
                    parts.append(summary)

        parts.append("\U0001f4a1 以上实体名可直接作为KG查询的keywords参数使用，例如：disk(\"/lightrag/lightrag_search_entities '实体名' --keywords '实体名'\")")

        result = "\n".join(parts)
        result = result.replace("<SEP>", "\n")
        return result

    def _get_members(self, region_id: str) -> list[str]:
        """Get member entity names for a region from NetworkX graph."""
        from niu_api.internal.lightrag_manager import get_region_members
        return get_region_members(region_id)

    def _get_member_count(self, region_id: str) -> int:
        """Get member count for a region."""
        return len(self._get_members(region_id))

    def _get_region_description(self, region_id: str) -> str:
        """Get region description from activation manager.

        Delegates to RegionActivationManager.get_region_description() which
        stores descriptions from BrainRegionInfo.
        """
        return self._activation_mgr.get_region_description(region_id)
