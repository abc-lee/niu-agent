"""
Brain Region Context Injector

Injects brain region context into the system prompt based on activation levels.
Provides the region status map (always injected); detailed content is now
provided by region-filtered search results.

M4 module: Context injection, M1-M3 provide detection, node management, and activation.
"""

from __future__ import annotations

import logging
from typing import Any

from niu_api.internal.lightrag_manager import get_all_region_members
from niu_api.internal.region_activation import (
    STATUS_DIMMING,
    STATUS_LIT,
    STATUS_OFF,
    BrainRegionState,
    RegionActivationManager,
)
from niu_api.internal.region_manager import RegionManager

logger = logging.getLogger(__name__)


class BrainContextInjector:
    """Brain region context injection

    Takes a query, activates relevant brain regions, and returns the
    region status map for the system prompt. Detailed content is now
    provided by region-filtered search results.

    Usage::

        injector = BrainContextInjector(adapter, activation_mgr, region_mgr)
        injection_text = injector.inject_brain_context("Python数据分析")
    """

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
                entity_to_region[member.lower()] = region_name

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
                        region_name = entity_to_region.get(entity_name.lower())
                        if not region_name:
                            region_name = self._classify_entity_to_region(entity_name, entity_type)
                            if region_name:
                                entity_to_region[entity_name.lower()] = region_name
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
        """Format brain region injection text.

        After refactoring, only returns the region status map.
        Detailed content is now provided by region-filtered search results.
        """
        regions = self._activation_mgr.get_region_map()
        if not regions:
            return ""
        return self.format_region_map(regions)

    def inject_brain_context(
        self,
        query_context: str,
    ) -> str:
        """Convenience: activate + format in one call."""
        if not query_context:
            return ""
        region_knowledge, entity_to_region, hit_entities = self.activate_for_query(query_context)
        return self.format_injection_text(region_knowledge, entity_to_region, hit_entities)

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

        lit_count = sum(1 for r in regions if r.activation > 0.3)
        if lit_count > 5:
            lines.append(f"> ⚠ {lit_count}个脑区已点亮，建议关闭与当前会话无关脑区以减少干扰")

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
            member_count = len(self._activation_mgr.get_members_of_region(region.region_id))
            description = self._activation_mgr.get_region_description(region.region_id)
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

    def format_region_map_only(self) -> str:
        """Format brain region status map only, without detailed content.

        Used when detailed content is provided by region-filtered search results
        instead of the old layered injection approach.
        """
        regions = self._activation_mgr.get_region_map()
        if not regions:
            return ""

        # Bug 1: 差集过滤 — 缓存有但图中没有的脑区 = 已删除但缓存未刷新
        # 主动 remove_region 清理缓存，避免下次查询还差集
        # 守卫：图查询失败（空集）或缓存与图无交集（数据不一致）时跳过过滤，
        # 避免误删测试/异常场景下的有效脑区
        graph_region_names = self._get_graph_region_names()
        if graph_region_names:
            cached_ids = {r.region_id for r in regions}
            # 缓存与图有交集才过滤（说明图查到了真实脑区，缓存中有部分匹配）
            if cached_ids & graph_region_names:
                stale_ids = cached_ids - graph_region_names
                for stale_id in stale_ids:
                    try:
                        self._activation_mgr.remove_region(stale_id)
                        logger.info("format_region_map_only 清理幽灵脑区缓存: %s", stale_id)
                    except Exception as e:
                        logger.warning("清理幽灵脑区缓存失败 %s: %s", stale_id, e)
                regions = [r for r in regions if r.region_id not in stale_ids]

        if not regions:
            return ""
        return self.format_region_map(regions)

    def _get_graph_region_names(self) -> set[str]:
        """Bug 1: 查图拿到所有真实存在的 brainregion 实体名

        用于读路径差集过滤。用 self._adapter 直接查 entity_type="brainregion"。
        Returns empty set on error.
        """
        try:
            result = self._adapter.list_entities(
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
            logger.warning("查图拿脑区名失败 (injector): %s", e)
            return set()

    def get_active_regions(self) -> list[BrainRegionState]:
        """Get regions with activation > threshold."""
        return self._activation_mgr.get_active_regions()

    def get_members_of_region(self, region_id: str) -> list[str]:
        """Get entity names belonging to a specific region."""
        return self._activation_mgr.get_members_of_region(region_id)

    def _classify_entity_to_region(self, entity_name: str, entity_type: str) -> str:
        """根据实体类型运行时分类到默认脑区（不写回图谱）

        当实体没有 包含 边（即不在 entity_to_region 映射中）时，
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
