"""
Brain Region Context Injector

Injects brain region context into the system prompt based on activation levels.
Provides the region status map (always injected) and tiered region knowledge
formatted by format_region_knowledge(): 🟢 lit regions get top 5 current/cached
hit entities, 🟡 dimming regions get top 3 cached hits, ⚫ off regions get none.

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
from niu_api.internal.region_manager import (
    REGION_SUFFIX,
    RegionManager,
    get_default_regions_config,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 实体过滤黑名单（与 agent/runner.py 常量同步维护——跨模块 import 会循环，
# 双份副本须手动保持一致）：
# - entity_type 黑名单：.lower() 归一化后比较（title case 变体也会命中）
# - entity_name 黑名单：case-sensitive 精确匹配（与 runner 现存实现一致）
# ---------------------------------------------------------------------------
_INJECT_ENTITY_TYPE_BLACKLIST = {"mcp_tool", "tool", "brainregion"}
_INJECT_ENTITY_NAME_BLACKLIST = {
    "agent_loop.py", "handler.py", "tool_registry.py", "主Agent",
    "context-manager", "chat_idle事件", "chat-with-file-processor",
    "chat-with-event-manager", "chat-with-journal-agent",
}

# 活跃脑区知识段全局条数上限：🟢 top5 + 🟡 top3 累计超 26 条截断。
# 逐条准入（严格 `>`）：第 26 条照常进入，累计达 26 后停止——
# 4🟢×5 + 2🟡×3 = 26 恰好全量输出；更亮场景截断（最坏 10🟢 压缩 50→26）。
_REGION_ENTRY_CAP = 26


class BrainContextInjector:
    """Brain region context injection

    Takes a query, activates relevant brain regions, and returns the
    region status map plus tiered region knowledge for the system prompt.
    Detailed content is provided by format_region_knowledge(): 🟢 regions get
    their top 5 hit entities (current round first, recent-hit cache fallback),
    🟡 regions get top 3 cached entities, ⚫ regions get none.

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
        # 跨轮缓存"每个脑区最近一次命中"（合并更新：只覆盖本轮命中脑区，
        # 保留未命中脑区旧条目）——供 🟡 档与 🟢 未命中回退使用。
        self._recent_region_entities: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def activate_for_query(
        self,
        query_context: str,
        timeout: int | None = None,
    ) -> tuple[dict[str, list[dict]], dict[str, str], list[str]]:
        """Step 1-3: Get entity mapping, vector-search, activate regions.

        Returns:
            (region_entities, entity_to_region, hit_entities) for use by
            format_region_knowledge() / format_injection_text().
            region_entities: region_id -> list of hit entity dicts
            (each with entity_name/entity_type/description, in query_data order).
        """
        if not query_context:
            return {}, {}, []

        region_members = get_all_region_members()
        entity_to_region: dict[str, str] = {}
        for region_name, members in region_members.items():
            for member in members:
                entity_to_region[member.lower()] = region_name

        hit_entities: list[str] = []
        region_entities: dict[str, list[dict]] = {}

        try:
            query_result = self._adapter.query_data(
                query_context, mode="local", top_k=20, keywords=[query_context],
                timeout=timeout,
            )
            if isinstance(query_result, dict) and query_result.get("status") == "error":
                # E3 契约反转：错误不再伪装为无结果——query_data error dict → raise，
                # 经下方 `except RuntimeError: raise` 重抛传导至 runner 既有 except
                # （脑区激活失败标注可达——断链修复；空激活语义（真空）保持不变）
                raise RuntimeError(query_result.get("message") or "知识图谱不可用")
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
                        if region_name:
                            region_entities.setdefault(region_name, []).append({
                                "entity_name": entity_name,
                                "entity_type": entity_type,
                                "description": entity.get("description", ""),
                            })
        except RuntimeError:
            # E3 契约反转：error dict 传导——不吞错（runner 侧 except 标注可达）
            raise
        except Exception as e:
            logger.warning("脑区注入向量检索失败: %s", e)

        self._activation_mgr.activate_regions(
            hit_entities, entity_to_region
        )

        # 合并更新最近命中缓存：只覆盖本轮命中脑区，保留未命中脑区旧条目
        # （覆盖更新会清空未命中脑区旧条目 → 🟡 档读取时恒空）
        for region_name, entities in region_entities.items():
            self._recent_region_entities[region_name] = entities

        return region_entities, entity_to_region, hit_entities

    def format_injection_text(
        self,
        region_entities: dict[str, list[dict]] | None = None,
        entity_to_region: dict[str, str] | None = None,
        hit_entities: list[str] | None = None,
    ) -> str:
        """Format brain region injection text.

        Returns only the region status map (kept for backward compatibility);
        detailed tiered region knowledge is provided by format_region_knowledge().
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
        region_entities, entity_to_region, hit_entities = self.activate_for_query(query_context)
        return self.format_injection_text(region_entities, entity_to_region, hit_entities)

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

        # 口径与 🟢 图标一致（>0.7）：黄灯不算点亮
        lit_count = sum(1 for r in regions if r.activation > 0.7)
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

        Used when detailed content is provided by format_region_knowledge()
        (tiered by activation level) instead of the old layered injection approach.
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

    def format_region_knowledge(
        self,
        region_entities: dict[str, list[dict]],
    ) -> list[tuple[str, str, str, str]]:
        """Format tiered region knowledge: 🟢 top 5 / 🟡 top 3 / ⚫ none.

        🟢 (activation > 0.7): current-round hits first, recent-hit cache
            fallback (a region stays green for several rounds after its last
            hit; falling back to cache keeps injection monotonic with
            activation: 🟢 >= 🟡 > ⚫).
        🟡 (activation > 0.3): recent-hit cache top 3 (yellow = recently
            relevant, not the current focus).
        ⚫: skipped.

        Filters: entity_type (lowercased) and entity_name (case-sensitive
        exact match) blacklists. Global cap: stops once _REGION_ENTRY_CAP
        entries have been admitted (green regions processed before yellow).

        Returns:
            List of (label, entity_name, entity_type, description) tuples;
            label is like "🟢 工作事务：" (status light + region label).
            Empty list when nothing to inject.
        """
        regions = self._activation_mgr.get_region_map()
        if not regions:
            return []

        # 🟢 先 🟡 后（⚫ 跳过）——cap 截断时绿灯档优先占满额度
        status_order = {STATUS_LIT: 0, STATUS_DIMMING: 1, STATUS_OFF: 2}
        sorted_regions = sorted(
            regions,
            key=lambda r: status_order.get(
                self._activation_mgr.get_status_light(r.activation), 3
            ),
        )

        entries: list[tuple[str, str, str, str]] = []
        for region in sorted_regions:
            light = self._activation_mgr.get_status_light(region.activation)
            if light == STATUS_LIT:
                source = (
                    region_entities.get(region.region_id)
                    or self._recent_region_entities.get(region.region_id, [])
                )
                tier_limit = 5
            elif light == STATUS_DIMMING:
                source = self._recent_region_entities.get(region.region_id, [])
                tier_limit = 3
            else:
                continue

            label = f"{light} {region.label}："
            for entity in source[:tier_limit]:
                name = entity.get("entity_name", "")
                if not name:
                    continue
                etype = entity.get("entity_type", "")
                if (etype or "").lower() in _INJECT_ENTITY_TYPE_BLACKLIST:
                    continue
                if name in _INJECT_ENTITY_NAME_BLACKLIST:
                    continue
                desc = entity.get("description") or ""
                entries.append((label, name, etype, desc))
                # 逐条准入：累计达上限立即停止（严格 `>`——恰好 26 条全量输出）
                if len(entries) >= _REGION_ENTRY_CAP:
                    return entries
        return entries

    def clear_recent_region_entities(self) -> None:
        """清空最近命中缓存（会话边界清理用）。

        /new 或 /clear 清空会话时调用，防止新会话前 ~11-15 轮持续注入
        上一会话的缓存实体（Task 3 在 runner clear 路径接线）。
        """
        self._recent_region_entities = {}

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
        """根据实体名称/类型运行时分类到默认脑区（不写回图谱）

        当实体没有 包含 边（即不在 entity_to_region 映射中）时，
        运行时分类让脑区注入先能工作起来。这只是注入时的运行时分类，
        不写回图谱（不改数据）。

        分类优先级（R10——用户 P6 拍板——keywords 分类机制）：
        1. keywords 匹配（前置）：entity_name 包含某脑区配置的 keywords
           （任一命中）→ 归该脑区（配置顺序首个命中）——全量消费
           get_default_regions_config() 的 keywords 字段（不硬编码子集）
        2. keywords 全 miss → 回退原 entity_type 映射（原逻辑保留原样）

        行为变化标注：keywords 命中的实体分类会变化（归入用户设计的脑区）——
        这是用户 P6 要求的行为（用户设计 keywords 就是为了分类）。

        Args:
            entity_name: 实体名称
            entity_type: 实体类型字符串

        Returns:
            脑区名称字符串
        """
        # R10: 前置 keywords 匹配（用户 P6 设计——配置里每个脑区的 keywords 字段）
        region_name = self._match_region_by_keywords(entity_name)
        if region_name:
            return region_name

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

    def _match_region_by_keywords(self, entity_name: str) -> str:
        """R10: entity_name 包含脑区配置 keywords（任一命中）→ 归该脑区（配置顺序首个）

        全量消费 get_default_regions_config() 的 keywords 字段——配置里每个脑区的
        所有 keywords 都参与匹配（不硬编码子集）。大小写不敏感（与系统
        entity_type/keywords 小写归一化约定一致——如配置 "PDF" 可命中实体名 "pdf"）。
        配置缺失 keywords 字段（.get 默认 []）或 label 时跳过该配置——不崩溃。

        Args:
            entity_name: 实体名称（可为空字符串）

        Returns:
            命中的脑区名称；全 miss 返回空字符串（由调用方回退原 entity_type 映射）
        """
        if not entity_name:
            return ""
        name_lower = entity_name.lower()
        for config in get_default_regions_config():
            label = config.get("label")
            if not isinstance(label, str) or not label:
                continue
            keywords = config.get("keywords") or []
            for keyword in keywords:
                if (
                    isinstance(keyword, str)
                    and keyword
                    and keyword.lower() in name_lower
                ):
                    return f"{label}{REGION_SUFFIX}"
        return ""
