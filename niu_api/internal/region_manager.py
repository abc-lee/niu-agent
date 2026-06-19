"""
Brain Region Master Node Manager

Creates and manages brain region entities in the LightRAG knowledge graph
for each Leiden community. Each region master node serves as:
- Semantic pointer for search
- Search entry via 脑区锚点 relation from Niu
- Metadata container (brain_meta_* attributes in description)

Entity names use natural language format (e.g., "编程开发脑区").

M2 module: Region node lifecycle, M1 provides community detection.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from niu_api.internal.region_detector import CommunityDetectionResult, RegionPartition

logger = logging.getLogger(__name__)


def _read_context_window_size() -> int:
    """Read context window size from user config.

    Returns 200000 as default if config is missing or unreadable.
    """
    try:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "config", "user-config.json",
        )
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("context", {}).get("contextWindowSize", 200000)
    except Exception:
        pass
    return 200000


# ============== Constants ==============

# Region entity name format: "{label}脑区" (natural language)
# e.g., "编程开发脑区", "聊天历史脑区"
REGION_SUFFIX = "脑区"

# Entity type for brain region master nodes
REGION_ENTITY_TYPE = "brainregion"

# Relation keywords
ANCHOR_RELATION = "脑区锚点"
BELONGS_TO_RELATION = "包含"

# 脑区边衰减优先级体系
PRIORITY_HALFLIFE = {
    "permanent": 360,  # 衰减但保底冻结，永不删除
    "long": 360,
    "medium": 180,
    "short": 90,
}
FLOOR_WEIGHT = 0.1       # 保底权重 / 删除阈值
INITIAL_WEIGHT = 1.0     # 边初始权重 / 增强恢复目标值
DEFAULT_PRIORITY = "medium"  # 非默认脑区和旧配置的回退值


def daily_decay_rate(priority: str) -> float:
    """根据优先级计算日衰减率（半衰期模型）"""
    halflife = PRIORITY_HALFLIFE.get(priority)
    if halflife is None:
        halflife = PRIORITY_HALFLIFE[DEFAULT_PRIORITY]
    return 0.5 ** (1.0 / halflife)


def _decay_brain_region_edges(nx_graph) -> dict:
    """衰减脑区边权重 — 半衰期模型 + 保底机制（核心逻辑，供测试直接调用）

    只衰减实体→脑区的归属边。知识关系边（实体→实体）不受影响。
    锚点边（脑区→脑区）和 _session: 前缀边被跳过。
    """
    decayed = 0
    deleted = 0
    protected = 0
    skipped_anchor = 0

    brain_regions = [
        n for n in nx_graph.nodes()
        if nx_graph.nodes[n].get("entity_type") == "brainregion"
    ]

    for region_key in brain_regions:
        desc = nx_graph.nodes[region_key].get("description", "")
        priority = parse_priority_from_description(desc)
        decay_rate = daily_decay_rate(priority)

        neighbors = list(nx_graph.neighbors(region_key))

        for entity_key in neighbors:
            # 跳过锚点边（脑区之间的导航边）
            if nx_graph.nodes[entity_key].get("entity_type") == "brainregion":
                skipped_anchor += 1
                continue

            edge_data = nx_graph.edges[region_key, entity_key]
            # 跳过 _session: 前缀边（会话临时边，不参与衰减）
            keywords = edge_data.get("keywords") or edge_data.get("type", "")
            if keywords.lower().startswith("_session:"):
                continue

            old_weight = edge_data.get("weight", INITIAL_WEIGHT)

            new_weight = old_weight * decay_rate

            total_degree = nx_graph.degree(entity_key)

            if priority == "permanent":
                # permanent 级：保底冻结，永不删除
                new_weight = max(new_weight, FLOOR_WEIGHT)
                nx_graph.edges[region_key, entity_key]["weight"] = new_weight
                decayed += 1
                protected += 1
            elif total_degree <= 1:
                # 孤立实体：保底保护
                new_weight = max(new_weight, FLOOR_WEIGHT)
                nx_graph.edges[region_key, entity_key]["weight"] = new_weight
                decayed += 1
                protected += 1
            elif new_weight < FLOOR_WEIGHT:
                # 非 permanent + 总边数>=2 + 低于保底 → 删除
                nx_graph.remove_edge(region_key, entity_key)
                deleted += 1
            else:
                nx_graph.edges[region_key, entity_key]["weight"] = new_weight
                decayed += 1

    return {
        "decayed": decayed,
        "deleted": deleted,
        "protected": protected,
        "skipped_anchor": skipped_anchor,
    }


# Source identifiers for injected data
REGION_SOURCE_ID = "brain"
REGION_FILE_PATH = "brain://region"

# Self entity name (natural language, no prefix)
NIU_ENTITY = "Niu"

# Maximum number of entity descriptions to include in region summary
MAX_SUMMARY_ENTITIES = 10

# Minimum community size to create a brain region (must match region_detector default)
MIN_COMMUNITY_SIZE = 100


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class BrainRegionInfo:
    """Brain region master node information"""

    name: str  # "编程开发脑区" (natural language)
    label: str  # "编程开发" (human-readable name)
    community_id: str  # "community_3"
    description: str  # LLM-generated summary
    size: int  # number of entities in region
    representative: str  # highest-degree entity
    members: list[str]  # all entity names in region
    updated_at: float  # last update timestamp


# ---------------------------------------------------------------------------
# Description encoding helpers
# ---------------------------------------------------------------------------


def _encode_description(
    summary: str,
    region_id: str,
    size: int,
    representative: str,
    updated_at: float,
    priority: str = DEFAULT_PRIORITY,
) -> str:
    """Encode region metadata into description using <SEP> separator.

    LightRAG stores custom attributes as flat text in the description field
    (GraphML limitation). The brain_meta_* attributes are embedded using
    <SEP> separators, following LightRAG's GRAPH_FIELD_SEP convention.
    """
    parts = [
        summary,
        f"brain_meta_region_id:{region_id}",
        f"brain_meta_size:{size}",
        f"brain_meta_representative:{representative}",
        f"brain_meta_updated_at:{int(updated_at)}",
        f"brain_meta_priority:{priority}",
    ]
    return "<SEP>".join(parts)


def parse_priority_from_description(description: str) -> str:
    """从 description 中解析 brain_meta_priority 字段"""
    import re
    if not description:
        return DEFAULT_PRIORITY
    # 使用与 _parse_description() 相同的分隔符处理方式
    parts = re.split(r'<SEP>|\s\|\s', description)
    for part in parts:
        part = part.strip()
        if part.startswith("brain_meta_priority:"):
            val = part[len("brain_meta_priority:"):]
            if val in PRIORITY_HALFLIFE:
                return val
            # 旧配置值警告（设计文档6.2节要求）
            if val in ("core", "category"):
                logger.warning(
                    "旧优先级值 '%s' 不再支持，回退到 DEFAULT_PRIORITY ('%s')。"
                    "请更新 preferences.json 中的 priority 字段。",
                    val, DEFAULT_PRIORITY,
                )
            return DEFAULT_PRIORITY
    return DEFAULT_PRIORITY


def _parse_description(description: str) -> dict[str, str]:
    """Parse brain_meta_* attributes from flat description text.

    Returns:
        Dict with all brain_meta_* keys plus summary.
        Always includes: summary, region_id, size, representative, updated_at.
        Additional keys (e.g. shrink_count) are preserved dynamically.
    """
    result: dict[str, str] = {
        "summary": "",
        "region_id": "",
        "size": "",
        "representative": "",
        "updated_at": "",
    }

    if not description:
        return result

    parts = re.split(r'<SEP>|\s\|\s', description)
    summary_parts: list[str] = []

    for part in parts:
        part = part.strip()
        match = re.match(r"brain_meta_(\w+):(.*)", part)
        if match:
            key = match.group(1)
            value = match.group(2)
            result[key] = value
        else:
            summary_parts.append(part)

    result["summary"] = "<SEP>".join(summary_parts)
    return result


def _format_summary_for_display(parsed: dict) -> str:
    """Format parsed description summary for frontend display."""
    return parsed.get("summary", "").replace("<SEP>", "、")


# ---------------------------------------------------------------------------
# Region Manager
# ---------------------------------------------------------------------------


class RegionManager:
    """Brain region master node lifecycle management

    Creates region entities (natural language names) for each Leiden community,
    serving as semantic pointers, search entries, and metadata containers.

    All public methods are synchronous. Internal adapter/ingester calls
    are sync methods that themselves use call_async for the LightRAG
    event loop, so wrapping RegionManager methods in call_async would
    cause a deadlock.

    Usage::

        manager = RegionManager(adapter, ingester)
        region_names = manager.create_region_nodes(partition_result)
        regions = manager.get_all_regions()
    """

    def __init__(self, adapter: Any, ingester: Any) -> None:
        self._adapter = adapter  # LightRAGAdapter
        self._ingester = ingester  # LightRAGIngester

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_region_nodes(
        self,
        partition_result: CommunityDetectionResult,
        skip_community_ids: set[str] | None = None,
    ) -> list[str]:
        """Create master nodes + relationships for each community

        Uses batch injection: collects all entities, relationships and chunks first,
        then calls inject_custom_kg once.

        Args:
            partition_result: Community detection result from M1
            skip_community_ids: Community IDs to skip (already handled by drift update)

        Returns:
            List of newly created region names (excludes existing regions that were only updated)
        """
        all_entities: list[dict] = []
        all_relationships: list[dict] = []
        all_chunks: list[dict] = []
        created_regions: list[str] = []
        stale_edge_cleanup: list[tuple[str, set[str]]] = []  # (region_name, new_members_set)

        # Pre-fetch existing region labels and names for LLM dedup + skip logic
        existing_region_names: set[str] = set()
        existing_labels: list[str] = []
        try:
            for region in self.get_all_regions():
                existing_region_names.add(region.name.lower())
                label = region.label or region.name.removesuffix(REGION_SUFFIX)
                existing_labels.append(label)
        except Exception:
            pass

        # Pass 1: Filter valid communities and collect data
        valid_communities: list[tuple] = []  # (partition, members, entity_summaries)
        for partition in partition_result.partitions:
            # Skip partitions already handled by drift update
            community_id = f"community_{partition.region_id}"
            if skip_community_ids and community_id in skip_community_ids:
                logger.debug("跳过漂移脑区对应的分区: %s", community_id)
                continue
            members = [
                name
                for name in partition.entity_names
                if not name.endswith(REGION_SUFFIX)
            ]
            if not members or len(members) < MIN_COMMUNITY_SIZE:
                logger.debug(
                    "社区 %d 成员数 %d < %d，跳过",
                    partition.region_id,
                    len(members),
                    MIN_COMMUNITY_SIZE,
                )
                continue

            entity_summaries = self._build_entity_summaries(
                members, partition.entity_types, partition.entity_name_to_type
            )
            valid_communities.append((partition, members, entity_summaries))

        # Pass 2: Generate all labels (batch for 3+, individual for fewer)
        entity_summaries_list = [es for _, _, es in valid_communities]
        labels = self._generate_labels(entity_summaries_list, existing_labels)

        # Pass 3: Build entities, relationships, chunks using generated labels
        for (partition, members, entity_summaries), region_label in zip(valid_communities, labels):
            region_summary = self._generate_region_summary(entity_summaries)
            representative = members[0].replace("<SEP>", "-").replace("|", "-") if members else ""
            community_id = f"community_{partition.region_id}"
            now = time.time()
            region_name = f"{region_label}{REGION_SUFFIX}"
            is_existing = region_name.lower() in existing_region_names

            description = _encode_description(
                summary=region_summary,
                region_id=community_id,
                size=len(members),
                representative=representative,
                updated_at=now,
                priority=DEFAULT_PRIORITY,
            )

            # Always upsert entity (updates description for existing regions)
            all_entities.append({
                "entity_name": region_name,
                "entity_type": REGION_ENTITY_TYPE,
                "description": description,
                "source_id": REGION_SOURCE_ID,
            })

            if is_existing:
                # D-7 fix: For stable regions with changed membership,
                # inject new edges first then remove stale edges (same
                # inject-before-delete pattern as _update_drifted_regions).
                # Skip only when membership is identical.
                current_members = {m.lower() if isinstance(m, str) else m for m in self.get_region_members(region_name)}
                new_members_lower = {m.lower() if isinstance(m, str) else m for m in members}
                if current_members == new_members_lower:
                    logger.debug("稳定脑区成员未变: %s", region_name)
                    continue

                # Members changed — inject new edges for members not yet in graph
                added_members = new_members_lower - current_members
                if added_members:
                    for member in members:
                        if (member.lower() if isinstance(member, str) else member) not in current_members:
                            all_relationships.append({
                                "src_id": region_name,
                                "tgt_id": member,
                                "keywords": BELONGS_TO_RELATION,
                                "description": f"{member} belongs to region {region_label}",
                                "weight": INITIAL_WEIGHT,
                                "source_id": REGION_SOURCE_ID,
                                "file_path": REGION_FILE_PATH,
                            })
                    logger.info(
                        "稳定脑区成员变更: %s (+%d 成员)",
                        region_name, len(added_members),
                    )
                # Track stale edge removal (execute after batch inject)
                removed_members = current_members - new_members_lower
                if removed_members:
                    stale_edge_cleanup.append((region_name, {m.lower() if isinstance(m, str) else m for m in members}))
                    logger.info(
                        "稳定脑区成员变更: %s (-%d 旧成员, 将在注入后清理)",
                        region_name, len(removed_members),
                    )
                continue

            # Below only for NEW regions — relationships + chunks
            top_members = members[:MAX_SUMMARY_ENTITIES]
            chunk_source_id = f"{REGION_SOURCE_ID}_{region_name}"

            all_chunks.append({
                "content": f"{region_label}脑区：{', '.join(top_members)}",
                "source_id": chunk_source_id,
                "file_path": REGION_FILE_PATH,
            })

            all_relationships.append({
                "src_id": NIU_ENTITY,
                "tgt_id": region_name,
                "keywords": ANCHOR_RELATION,
                "description": f"Brain region anchor: {region_label}",
                "weight": INITIAL_WEIGHT,
                "source_id": REGION_SOURCE_ID,
                "file_path": REGION_FILE_PATH,
            })

            for member in members:
                all_relationships.append({
                    "src_id": region_name,
                    "tgt_id": member,
                    "keywords": BELONGS_TO_RELATION,
                    "description": f"{member} belongs to region {region_label}",
                    "weight": INITIAL_WEIGHT,
                    "source_id": REGION_SOURCE_ID,
                    "file_path": REGION_FILE_PATH,
                })

            created_regions.append(region_name)
            logger.info(
                "收集脑区节点: %s (社区 %d, %d 成员, 代表: %s)",
                region_name,
                partition.region_id,
                len(members),
                representative,
            )

        # Batch inject all collected data in one call
        if all_entities or all_relationships:
            result = self._ingester.inject_custom_kg(
                entities=all_entities,
                relationships=all_relationships,
                chunks=all_chunks,
                source_id=REGION_SOURCE_ID,
            )
            if isinstance(result, dict) and result.get("status") == "error":
                logger.warning(
                    "批量注入脑区实体失败: %s (collected %d regions)",
                    result.get("message", "unknown"),
                    len(created_regions),
                )
                stale_edge_cleanup.clear()  # 注入失败，不清理旧边
                return []
            logger.info(
                "批量注入 %d 个脑区实体, %d 条关系, %d 个chunks",
                len(all_entities),
                len(all_relationships),
                len(all_chunks),
            )

        # D-7 fix: Remove stale "包含" edges for stable regions with changed membership
        # Execute AFTER batch inject to follow inject-before-delete pattern
        if stale_edge_cleanup:
            from niu_api.internal.lightrag_manager import remove_region_stale_edges
            for region_name, new_members in stale_edge_cleanup:
                try:
                    removed_count = remove_region_stale_edges(
                        region_name, BELONGS_TO_RELATION, new_members,
                    )
                    if removed_count > 0:
                        logger.info(
                            "稳定脑区旧边清理: %s 移除 %d 条过期包含边",
                            region_name, removed_count,
                        )
                except Exception as e:
                    logger.warning(
                        "稳定脑区旧边清理失败: %s — %s (继续处理其他脑区)",
                        region_name, e,
                    )

        logger.info("共创建 %d 个脑区节点", len(created_regions))
        return created_regions

    def update_region_summaries(
        self,
        region_names: list[str],
    ) -> None:
        """Re-generate summaries for specified regions (after membership changes)

        For each region:
        1. Get current members via get_region_members()
        2. Re-generate summary via _generate_region_summary() (no LLM call)
        3. Update master node via inject_entity (overwrite)

        Args:
            region_names: List of region entity names to update
        """
        all_entities: list[dict] = []

        # Pre-fetch all region entities once (avoids N+1 list_entities calls)
        region_desc_map: dict[str, str] = {}
        list_result = self._adapter.list_entities(
            list_type="entities", entity_type=REGION_ENTITY_TYPE, limit=1000
        )
        if isinstance(list_result, dict) and list_result.get("status") == "ok":
            for entity in list_result.get("data", []):
                name = entity.get("id") or entity.get("entity_name", "")
                if name:
                    region_desc_map[name] = entity.get("description", "")

        for region_name in region_names:
            if is_default_region(region_name):
                logger.debug("跳过默认脑区摘要更新: %s", region_name)
                continue

            # Step 1: Get current members
            members = self.get_region_members(region_name)

            if not members:
                logger.debug("脑区 %s 无成员，跳过摘要更新", region_name)
                continue

            # Step 2: Get current region description from pre-fetched map
            current_desc = region_desc_map.get(region_name, "")

            if not current_desc:
                # Fallback: try explore_node for backward compatibility
                explore_result = self._adapter.explore_node(region_name, depth=0)
                if explore_result and explore_result.get("center"):
                    for node in explore_result.get("nodes", []):
                        if node.get("id") == region_name or node.get("name") == region_name:
                            current_desc = node.get("description", "")
                            break

            if not current_desc:
                logger.debug(
                    "脑区 %s 无现有描述，跳过摘要更新（避免覆盖为空）",
                    region_name,
                )
                continue

            parsed = _parse_description(current_desc)
            community_id = parsed.get("region_id", "")
            representative = members[0].replace("<SEP>", "-").replace("|", "-") if members else ""

            # Preserve dynamic metadata keys (e.g. shrink_count) that
            # _encode_description does not include in its standard 5 fields
            STANDARD_KEYS = {"summary", "region_id", "size", "representative", "updated_at", "priority"}
            extra_meta = {
                k: v for k, v in parsed.items()
                if k not in STANDARD_KEYS and v
            }

            # Build entity summaries with type labels from graph
            # Read entity types from NetworkX graph to preserve type info (D-16 fix)
            from niu_api.internal.lightrag_manager import graph_read_lock
            entity_name_to_type: dict[str, str] = {}
            try:
                rag = self._adapter._get_rag()
                if rag is not None:
                    kg = rag.chunk_entity_relation_graph
                    nx_graph = kg._graph if hasattr(kg, "_graph") else kg
                    if nx_graph is not None:
                        with graph_read_lock():
                            for member in members:
                                member_lower = member.lower() if isinstance(member, str) else member
                                if member_lower in nx_graph:
                                    node_data = nx_graph.nodes[member_lower]
                                    etype = node_data.get("entity_type", "")
                                    if etype:
                                        entity_name_to_type[member] = etype
            except Exception:
                pass  # Read failure falls back to empty mapping — no worse than current code
            entity_summaries = self._build_entity_summaries(members, {}, entity_name_to_type or None)
            region_summary = self._generate_region_summary(entity_summaries)

            now = time.time()
            priority = parse_priority_from_description(current_desc)
            description = _encode_description(
                summary=region_summary,
                region_id=community_id,
                size=len(members),
                representative=representative,
                updated_at=now,
                priority=priority,
            )

            # Append preserved dynamic metadata
            for key, value in extra_meta.items():
                description += f"<SEP>brain_meta_{key}:{value}"

            # Collect updated entity for batch inject
            all_entities.append({
                "entity_name": region_name,
                "entity_type": REGION_ENTITY_TYPE,
                "description": description,
            })

            logger.info(
                "更新脑区摘要: %s (%d 成员)", region_name, len(members)
            )

        # Batch inject all updated entities in one call
        if all_entities:
            self._ingester.inject_custom_kg(
                entities=all_entities,
                relationships=[],
                chunks=[],
                source_id=REGION_SOURCE_ID,
            )

    def get_all_regions(self) -> list[BrainRegionInfo]:
        """Query all entity_type=BrainRegion entities from LightRAG

        No longer async — internal calls (adapter) are synchronous methods
        that themselves use call_async for the LightRAG event loop, so wrapping
        this method in call_async would cause a deadlock.

        Returns:
            List of BrainRegionInfo for all region master nodes
        """
        result = self._adapter.list_entities(
            list_type="entities",
            entity_type=REGION_ENTITY_TYPE,
            limit=1000,
        )

        if not isinstance(result, dict) or result.get("status") != "ok":
            logger.warning("查询 BrainRegion 实体失败")
            return []

        data = result.get("data", [])
        regions: list[BrainRegionInfo] = []

        for entity in data:
            entity_name = entity.get("id", entity.get("entity_name", ""))
            description = entity.get("description", "")

            parsed = _parse_description(description)

            # Extract label from entity name: "{label}脑区"
            label = entity_name
            if entity_name.endswith(REGION_SUFFIX):
                label = entity_name[: -len(REGION_SUFFIX)]

            # 将 <SEP> 替换为 "、" 用于前端展示
            display_summary = _format_summary_for_display(parsed)

            regions.append(
                BrainRegionInfo(
                    name=entity_name,
                    label=label,
                    community_id=parsed.get("region_id", ""),
                    description=display_summary,
                    size=int(parsed.get("size", "0") or "0"),
                    representative=parsed.get("representative", ""),
                    members=[],  # Members not included in list_entities result
                    updated_at=float(parsed.get("updated_at", "0") or "0"),
                )
            )

        return regions

    def get_region_members(self, region_name: str) -> list[str]:
        """Get members by reading 包含 edges from NetworkX graph.

        Delegates to lightrag_manager.get_region_members() which directly
        reads the in-memory graph — more reliable than explore_node.
        """
        from niu_api.internal.lightrag_manager import get_region_members as lightrag_get_region_members
        return lightrag_get_region_members(region_name)

    def cleanup_stale_regions(
        self,
        current_partition: CommunityDetectionResult,
        drift_threshold: float = 0.3,
        dry_run: bool = False,
    ) -> tuple[list[str], list[str], set[str]]:
        """Remove stale and detect drifted regions using Jaccard similarity.

        Instead of matching by community_id (unstable across Leiden runs),
        compares actual membership overlap between existing regions and
        new partition communities.

        Args:
            current_partition: Current community detection result
            drift_threshold: Jaccard index below which a region is considered
                drifted (default 0.3). Regions with best_jaccard >= threshold
                are stable; 0 < best_jaccard < threshold → drifted;
                best_jaccard == 0 → stale (removed).
            dry_run: If True, only detect without executing changes.

        Returns:
            Tuple of (removed_region_names, drifted_region_names,
            drifted_community_ids)
        """
        from niu_api.internal.lightrag_manager import get_all_region_members

        # Step 1: Batch-read all region members from graph
        region_member_map: dict[str, list[str]] = get_all_region_members()

        # Step 2: Build community_id → member set mapping from partition
        community_members: dict[str, set[str]] = {}
        for partition in current_partition.partitions:
            cid = f"community_{partition.region_id}"
            community_members[cid] = set(partition.entity_names)

        # Step 3: Get all existing regions
        existing_regions = self.get_all_regions()

        # Safety check: if region_member_map is empty but non-default regions
        # exist, the read may have failed — skip drift detection to avoid
        # false removals
        non_default_regions = [
            r for r in existing_regions if not is_default_region(r.name)
        ]
        if not region_member_map and non_default_regions:
            logger.warning(
                "get_all_region_members 返回空但存在 %d 个非默认脑区，跳过漂移检测避免误删",
                len(non_default_regions),
            )
            return ([], [], set())

        removed: list[str] = []
        drift_info: dict[str, tuple[str, set[str]]] = {}  # region_name → (best_cid, best_members)

        for region in existing_regions:
            if is_default_region(region.name):
                logger.debug("保护默认脑区: %s", region.name)
                continue

            current_members = set(region_member_map.get(region.name, []))

            # Find best-matching community by Jaccard similarity
            best_jaccard = 0.0
            best_cid = ""
            best_members: set[str] = set()

            for cid, members in community_members.items():
                if not current_members and not members:
                    continue
                union = current_members | members
                if not union:
                    continue
                intersection = current_members & members
                jaccard = len(intersection) / len(union)
                if jaccard > best_jaccard:
                    best_jaccard = jaccard
                    best_cid = cid
                    best_members = members

            if best_jaccard >= drift_threshold:
                # Region is stable — no action needed
                logger.debug(
                    "脑区 %s 稳定 (Jaccard=%.2f, best_cid=%s)",
                    region.name, best_jaccard, best_cid,
                )
            elif best_jaccard > 0:
                # Region has drifted — record for update
                logger.info(
                    "脑区 %s 漂移 (Jaccard=%.2f, best_cid=%s)",
                    region.name, best_jaccard, best_cid,
                )
                drift_info[region.name] = (best_cid, best_members)
            else:
                # Region is stale — no overlap at all
                if not dry_run:
                    delete_result = self._adapter.delete_entity(region.name)
                    if isinstance(delete_result, dict) and delete_result.get("status") == "ok":
                        removed.append(region.name)
                        logger.info(
                            "删除过时脑区: %s (Jaccard=0, 无成员重叠)",
                            region.name,
                        )
                    else:
                        logger.warning(
                            "删除过时脑区失败: %s — %s",
                            region.name,
                            delete_result.get("message", "unknown") if isinstance(delete_result, dict) else "error",
                        )
                else:
                    logger.info(
                        "[dry_run] 将删除过时脑区: %s (Jaccard=0)",
                        region.name,
                    )

        # Step 5: Generate drifted lists from drift_info (always, regardless of update outcome)
        drifted_names: list[str] = []
        drifted_cids: set[str] = set()
        for region_name, (cid, _members) in drift_info.items():
            drifted_names.append(region_name)
            drifted_cids.add(cid)

        # Execute drift updates (skip in dry_run)
        if drift_info and not dry_run:
            try:
                self._update_drifted_regions(drift_info, current_partition)
            except Exception as e:
                logger.warning(
                    "漂移更新执行失败 (drifted 列表仍将返回): %s", e,
                )

        if removed:
            logger.info("共清理 %d 个过时脑区节点", len(removed))
        if drifted_names:
            logger.info("共检测到 %d 个漂移脑区", len(drifted_names))

        return (removed, drifted_names, drifted_cids)

    def _update_drifted_regions(
        self,
        drift_info: dict[str, tuple[str, set[str]]],
        current_partition: CommunityDetectionResult,
    ) -> None:
        """Update regions whose membership has drifted.

        For each drifted region:
        1. Re-generate summary with type info from partition data
        2. Inject new entity description + membership edges (upsert)
        3. Remove stale "包含" edges (members no longer in new_member_set)

        Order matters: inject-before-delete avoids the zero-member window
        if inject_custom_kg fails after stale edges have been removed.
        """
        # Build community_id -> partition lookup for type info
        partition_map: dict[str, RegionPartition] = {}
        for partition in current_partition.partitions:
            cid = f"community_{partition.region_id}"
            partition_map[cid] = partition

        all_entities: list[dict] = []
        all_relationships: list[dict] = []
        region_new_members: dict[str, set[str]] = {}

        for region_name, (best_cid, new_members) in drift_info.items():
            if not new_members:
                continue
            region_new_members[region_name] = new_members

            # Step 1: Re-generate summary with type info from partition
            partition = partition_map.get(best_cid)
            entity_summaries = self._build_entity_summaries(
                list(new_members),
                partition.entity_types if partition else {},
                partition.entity_name_to_type if partition else None,
            )
            summary = self._generate_region_summary(entity_summaries)
            representative = list(new_members)[0].replace("<SEP>", "-").replace("|", "-")
            # Preserve priority from existing region description
            old_desc = ""
            try:
                explore_result = self._adapter.explore_node(region_name, depth=0)
                if explore_result and explore_result.get("center"):
                    for node in explore_result.get("nodes", []):
                        if node.get("id") == region_name or node.get("name") == region_name:
                            old_desc = node.get("description", "")
                            break
            except Exception:
                pass
            priority = parse_priority_from_description(old_desc)
            now = time.time()
            description = _encode_description(
                summary=summary, region_id=best_cid,
                size=len(new_members), representative=representative,
                updated_at=now,
                priority=priority,
            )
            all_entities.append({
                "entity_name": region_name, "entity_type": REGION_ENTITY_TYPE,
                "description": description, "source_id": REGION_SOURCE_ID,
            })
            # Step 2: New membership edges
            for member in new_members:
                all_relationships.append({
                    "src_id": region_name, "tgt_id": member,
                    "keywords": BELONGS_TO_RELATION,
                    "description": f"{member} belongs to region {region_name}",
                    "weight": INITIAL_WEIGHT, "source_id": REGION_SOURCE_ID,
                    "file_path": REGION_FILE_PATH,
                })

        # Step 3: Inject FIRST (before removing stale edges)
        if all_entities or all_relationships:
            try:
                self._ingester.inject_custom_kg(
                    entities=all_entities, relationships=all_relationships,
                    chunks=[], source_id=REGION_SOURCE_ID,
                )
            except Exception as e:
                logger.error(
                    "漂移更新注入失败: %d entities, %d relationships -- %s",
                    len(all_entities), len(all_relationships), e,
                )
                # Do NOT remove stale edges — inject failed,
                # keeping old edges is safer than having zero members
                return

        # Step 4: Remove stale "包含" edges (only after successful inject)
        from niu_api.internal.lightrag_manager import remove_region_stale_edges
        for region_name, new_members in region_new_members.items():
            try:
                removed_count = remove_region_stale_edges(
                    region_name, BELONGS_TO_RELATION, new_members,
                )
                logger.debug(
                    "漂移更新: 移除 %s 的 %d 条过期包含边",
                    region_name, removed_count,
                )
            except Exception as e:
                logger.warning(
                    "漂移更新: 移除 %s 的过期包含边失败: %s (继续处理其他脑区)",
                    region_name, e,
                )

    def dissolve_shrunk_regions(
        self,
        shrink_threshold: int = 100,  # Threshold raised from 3 to 100
        shrink_rounds: int = 3,
    ) -> list[str]:
        """Dissolve regions that have been shrinking for multiple sync cycles.

        A region is "shrunk" when its member count < shrink_threshold.
        After shrink_rounds consecutive sync cycles of being shrunk,
        the region is dissolved: members are reassigned to the most
        similar neighbor region, and the region node is deleted.

        Shrink tracking is stored in the region description field
        as ``brain_meta_shrink_count:N``.

        Args:
            shrink_threshold: Minimum members before region is "shrunk" (default 100)
                Raised from 3 to reduce noise from small region dissolution.
            shrink_rounds: Consecutive shrunk cycles before dissolution (default 3)

        Returns:
            List of dissolved region entity names.
        """
        existing_regions = self.get_all_regions()
        dissolved: list[str] = []
        dissolved_names: set[str] = set()  # Track dissolved names for stale snapshot filtering

        # Pre-fetch raw descriptions from KG (get_all_regions strips brain_meta_* metadata)
        region_raw_desc_map: dict[str, str] = {}
        list_result = self._adapter.list_entities(
            list_type="entities", entity_type=REGION_ENTITY_TYPE, limit=1000
        )
        if isinstance(list_result, dict) and list_result.get("status") == "ok":
            for entity in list_result.get("data", []):
                name = entity.get("id") or entity.get("entity_name", "")
                if name:
                    region_raw_desc_map[name] = entity.get("description", "")

        for region in existing_regions:
            # Protect default regions (defined in preferences.json)
            if is_default_region(region.name):
                continue

            members = self.get_region_members(region.name)
            current_size = len(members)

            # Parse shrink count from RAW KG description (not stripped summary)
            raw_desc = region_raw_desc_map.get(region.name, "")
            # Fallback: try explore_node if list_entities didn't return this region
            if not raw_desc:
                try:
                    explore_result = self._adapter.explore_node(region.name, depth=0)
                    if explore_result and explore_result.get("center"):
                        for node in explore_result.get("nodes", []):
                            if node.get("id") == region.name or node.get("name") == region.name:
                                raw_desc = node.get("description", "")
                                break
                except Exception:
                    pass

            parsed = _parse_description(raw_desc)
            shrink_count = int(parsed.get("shrink_count", "0") or "0")

            if current_size < shrink_threshold:
                shrink_count += 1
            else:
                shrink_count = 0

            # Check dissolution threshold before writing shrink_count
            if shrink_count >= shrink_rounds:
                # Region will be dissolved — skip shrink_count write
                target_region = self._find_most_similar_neighbor(
                    region, existing_regions, dissolved_names
                )

                reassign_rels: list[dict] = []
                if target_region:
                    # Reassign members to target via belongs_to relations
                    # (injected AFTER delete to avoid duplicate edges)
                    for member in members:
                        reassign_rels.append({
                            "src_id": target_region.name,
                            "tgt_id": member,
                            "keywords": BELONGS_TO_RELATION,
                            "description": f"{member} belongs to region {target_region.label}",
                            "weight": INITIAL_WEIGHT,  # Unified initial weight
                            "source_id": REGION_SOURCE_ID,
                            "file_path": REGION_FILE_PATH,
                        })

                # Delete the dissolved region node first (cascades old belongs_to edges)
                delete_result = self._adapter.delete_entity(region.name)
                if isinstance(delete_result, dict) and delete_result.get("status") == "ok":
                    dissolved.append(region.name)
                    dissolved_names.add(region.name)
                    logger.info(
                        "解散萎缩脑区: %s (成员 %d, 萎缩 %d 轮, 归入 %s)",
                        region.name, current_size, shrink_count,
                        target_region.name if target_region else "无",
                    )

                    # Now inject new belongs_to relations for target region
                    if target_region and reassign_rels:
                        try:
                            self._ingester.inject_custom_kg(
                                entities=[],
                                relationships=reassign_rels,
                                chunks=[],
                                source_id=REGION_SOURCE_ID,
                            )
                        except Exception as e:
                            logger.debug("重新分配成员失败 %s -> %s: %s",
                                         region.name, target_region.name, e)
                else:
                    logger.warning("解散脑区失败: %s", region.name)
            elif shrink_count > 0 or parsed.get("shrink_count", "0") != "0":
                # Persist shrink_count (incremented or reset to 0)
                # Reset-to-0 write is needed so next sync doesn't read stale count
                now = time.time()
                priority = parse_priority_from_description(raw_desc)
                updated_desc = _encode_description(
                    summary=parsed.get("summary", ""),
                    region_id=region.community_id,
                    size=current_size,
                    representative=region.representative,
                    updated_at=now,
                    priority=priority,
                )
                # Append shrink_count + preserve other dynamic metadata
                updated_desc += f"<SEP>brain_meta_shrink_count:{shrink_count}"
                STANDARD_KEYS = {"summary", "region_id", "size", "representative", "updated_at", "shrink_count", "priority"}
                for key, value in parsed.items():
                    if key not in STANDARD_KEYS and value:
                        updated_desc += f"<SEP>brain_meta_{key}:{value}"

                try:
                    self._ingester.inject_custom_kg(
                        entities=[{
                            "entity_name": region.name,
                            "entity_type": REGION_ENTITY_TYPE,
                            "description": updated_desc,
                        }],
                        relationships=[],
                        chunks=[],
                        source_id=REGION_SOURCE_ID,
                    )
                except Exception as e:
                    logger.debug("更新萎缩计数失败 %s: %s", region.name, e)

        if dissolved:
            logger.info("共解散 %d 个萎缩脑区", len(dissolved))
        return dissolved

    def _find_most_similar_neighbor(
        self,
        region: BrainRegionInfo,
        all_regions: list[BrainRegionInfo],
        excluded_names: set[str] | None = None,
    ) -> BrainRegionInfo | None:
        """Find the most similar neighbor region by entity type distribution.

        Uses cosine similarity on entity_type count vectors derived from
        actual member entities (via explore_node), not from description text.
        Excludes the region itself, default regions (defined in preferences.json),
        and any names in excluded_names (e.g. already dissolved regions).
        """
        import math

        # Build entity type distribution from actual member entities
        region_types = self._get_entity_type_distribution(region.name)

        best_score = -1.0
        best_region: BrainRegionInfo | None = None
        _excluded = excluded_names or set()

        for other in all_regions:
            if other.name == region.name:
                continue
            if is_default_region(other.name):
                continue
            if other.name in _excluded:
                continue

            other_types = self._get_entity_type_distribution(other.name)

            # Cosine similarity
            all_keys = set(region_types.keys()) | set(other_types.keys())
            dot = sum(region_types.get(k, 0) * other_types.get(k, 0) for k in all_keys)
            norm_a = math.sqrt(sum(v * v for v in region_types.values())) if region_types else 0
            norm_b = math.sqrt(sum(v * v for v in other_types.values())) if other_types else 0

            if norm_a > 0 and norm_b > 0:
                score = dot / (norm_a * norm_b)
            else:
                score = 0.0

            if score > best_score:
                best_score = score
                best_region = other

        return best_region

    def _get_entity_type_distribution(self, region_name: str) -> dict[str, int]:
        """Get entity type distribution for a region's members via explore_node.

        Returns a dict of entity_type -> count for all member entities.
        Falls back to empty dict if explore fails.
        """
        type_counts: dict[str, int] = {}
        try:
            result = self._adapter.explore_node(region_name, depth=1)
            if result and isinstance(result, dict):
                for node in result.get("nodes", []):
                    node_name = node.get("name", node.get("id", ""))
                    # Skip the region node itself
                    if node_name == region_name:
                        continue
                    etype = node.get("entityType", node.get("type", "Other"))
                    type_counts[etype] = type_counts.get(etype, 0) + 1
        except Exception as e:
            logger.debug("获取实体类型分布失败 %s: %s", region_name, e)
        return type_counts

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    def _build_entity_summaries(
        self,
        members: list[str],
        entity_types: dict[str, int],
        entity_name_to_type: dict[str, str] | None = None,
    ) -> list[str]:
        """Build entity summary strings from member names and type counts.

        Uses entity_name_to_type mapping for accurate type labels instead of
        positional assignment from a flat type queue.

        Args:
            members: Entity names in the community
            entity_types: entity_type -> count mapping
            entity_name_to_type: Optional entity_name -> entity_type mapping
                for accurate per-entity type lookup

        Returns:
            List of summary strings like ["Python(skill)", "Django(framework)", ...]
        """
        summaries: list[str] = []
        type_fallback_queue: list[str] = []

        # Build fallback queue from type counts for entities without a mapping
        sorted_types = sorted(
            (entity_types or {}).items(), key=lambda x: x[1], reverse=True
        )
        for etype, count in sorted_types:
            type_fallback_queue.extend([etype] * count)

        fallback_idx = 0
        for member in members:
            # Look up actual type from name-to-type mapping
            if entity_name_to_type and member in entity_name_to_type:
                etype = entity_name_to_type[member]
            elif fallback_idx < len(type_fallback_queue):
                etype = type_fallback_queue[fallback_idx]
                fallback_idx += 1
            else:
                etype = "unknown"
            summaries.append(f"{member}({etype})")

        return summaries

    def _generate_region_summary(self, entity_summaries: list[str]) -> str:
        """Generate region description from top entity names using <SEP> separator.

        Entity names are joined by <SEP> (LightRAG's GRAPH_FIELD_SEP) so that
        vector search can match individual entity names as semantic fragments.
        """
        if not entity_summaries:
            return ""

        entity_names: list[str] = []
        for summary in entity_summaries[:MAX_SUMMARY_ENTITIES]:
            match = re.match(r"([^(]+)\(([^)]+)\)", summary)
            if match:
                name = match.group(1).strip()
            else:
                name = summary.strip()
            # Sanitize: replace <SEP> and | to avoid breaking description parsing
            name = name.replace("<SEP>", "-").replace("|", "-")
            entity_names.append(name)

        return "<SEP>".join(entity_names)

    def _generate_region_label(
        self,
        entity_summaries: list[str],
        existing_regions: list[str],
    ) -> str:
        """Generate a semantic Chinese label for a brain region via LLM.

        Falls back to heuristic (entity_names[0]) on any LLM failure.
        """
        if not entity_summaries:
            return "unknown"

        # Extract entity names for prompt and fallback
        entity_names: list[str] = []
        entity_list_parts: list[str] = []
        for summary in entity_summaries:
            match = re.match(r"([^(]+)\(([^)]+)\)", summary)
            if match:
                name = match.group(1).strip()
                etype = match.group(2).strip()
                entity_names.append(name)
                entity_list_parts.append(f"{name}({etype})")
            else:
                entity_names.append(summary.strip())
                entity_list_parts.append(summary.strip())

        if not entity_names:
            return "unknown"

        fallback_label = entity_names[0].replace("<SEP>", "-").replace("|", "-")

        # Build prompt
        entity_list_str = ", ".join(entity_list_parts)
        existing_str = ", ".join(existing_regions) if existing_regions else "无"

        prompt = (
            "你是一个知识图谱分析师。根据以下社区内的实体列表，为这个社区生成一个简洁的中文标签名。\n\n"
            "要求：\n"
            "- 8个字以下\n"
            "- 概括这些实体的共同主题\n"
            "- 不要跟现有脑区重名或语义接近\n"
            "- 只能返回JSON格式：{\"label\": \"标签名\"}\n"
            "- 返回其他任何格式或内容将判定失败\n\n"
            f"现有脑区：{existing_str}\n\n"
            f"实体列表：{entity_list_str}"
        )

        # Token truncation check
        try:
            from agent.token_calculator import TokenCalculator
            calc = TokenCalculator.get()
            token_count = calc.count_text(prompt)
            context_window = _read_context_window_size()
            if token_count > context_window - 500:
                while entity_list_parts and token_count > context_window - 500:
                    entity_list_parts.pop()
                    entity_list_str = ", ".join(entity_list_parts)
                    prompt = (
                        "你是一个知识图谱分析师。根据以下社区内的实体列表，为这个社区生成一个简洁的中文标签名。\n\n"
                        "要求：\n"
                        "- 8个字以下\n"
                        "- 概括这些实体的共同主题\n"
                        "- 不要跟现有脑区重名或语义接近\n"
                        "- 只能返回JSON格式：{\"label\": \"标签名\"}\n"
                        "- 返回其他任何格式或内容将判定失败\n\n"
                        f"现有脑区：{existing_str}\n\n"
                        f"实体列表：{entity_list_str}"
                    )
                    token_count = calc.count_text(prompt)
        except Exception:
            pass  # Token counting failure should not block

        # Call LLM with retry
        label = self._parse_label_from_llm(prompt, fallback_label)

        # Truncate to 8 chars first
        if len(label) > 8:
            label = label[:8]

        # Check for duplicate names (suffix must fit in 8 chars)
        if label in existing_regions:
            base = label[:7]
            n = 2
            candidate = f"{base}{n}"
            while candidate in existing_regions and n < 10:
                n += 1
                candidate = f"{base}{n}"
            label = candidate

        return label

    def _parse_label_from_llm(self, prompt: str, fallback: str) -> str:
        """Call LLM and parse label with retry logic."""
        for attempt in range(2):
            try:
                content = self._call_llm_for_label(prompt)
                label = self._extract_label_from_content(content)
                if label:
                    if len(label) > 8:
                        label = label[:8]
                    return label
            except Exception as e:
                logger.debug("LLM label generation attempt %d failed: %s", attempt + 1, e)

        logger.warning("LLM label generation failed after retry, fallback to: %s", fallback)
        return fallback

    def _extract_label_from_content(self, content: str) -> str:
        """Extract label from LLM response content."""
        content = content.strip()

        # Try JSON parse
        try:
            data = json.loads(content)
            if isinstance(data, dict) and "label" in data:
                label = str(data["label"]).strip()
                if label:
                    return label
        except (json.JSONDecodeError, ValueError):
            pass

        # Try regex extraction
        match = re.search(r'"label"\s*:\s*"([^"]+)"', content)
        if match:
            label = match.group(1).strip()
            if label:
                return label

        return ""

    def _call_llm_for_label(self, prompt: str) -> str:
        """Call LLM via LiteLLMSession to generate a label.

        Consumes the streaming generator and returns the full text content.
        30-second timeout via thread-based mechanism.
        """
        from niu_api.internal.lightrag_manager import _get_litellm_session
        from niu_api.llm_proxy import get_llm_config

        config = get_llm_config()  # 主 Agent 同款模型
        session = _get_litellm_session(config)
        gen = session.chat(messages=[{"role": "user", "content": prompt}])

        # Consume generator with 30s timeout
        chunks: list[str] = []
        try:
            import threading

            result_holder: list = [None, None]  # [content, exception]

            def _consume():
                try:
                    while True:
                        chunk = next(gen)
                        if isinstance(chunk, str):
                            chunks.append(chunk)
                except StopIteration:
                    pass
                except Exception as e:
                    result_holder[1] = e

            thread = threading.Thread(target=_consume, daemon=True)
            thread.start()
            thread.join(timeout=30)

            if thread.is_alive():
                logger.warning("LLM label generation timed out after 30s, using partial result")
            if result_holder[1]:
                raise result_holder[1]

        except Exception as e:
            if not chunks:
                raise
            logger.warning("LLM label generation error: %s, using partial result", e)

        return "".join(chunks)

    def _generate_labels(
        self,
        entity_summaries_list: list[list[str]],
        existing_regions: list[str],
    ) -> list[str]:
        """Generate labels for multiple regions, using batch or individual calls.

        Uses batch LLM call for 3+ regions, individual for fewer.
        """
        if len(entity_summaries_list) >= 3:
            try:
                batch_result = self._generate_region_labels_batch(
                    entity_summaries_list, existing_regions
                )
                # Check if batch returned all labels
                labels = []
                missing_indices = []
                for i in range(len(entity_summaries_list)):
                    if i in batch_result:
                        labels.append(batch_result[i])
                    else:
                        labels.append(None)
                        missing_indices.append(i)

                # Fallback to individual for missing
                extended_existing = list(existing_regions) + [labels[j] for j in range(len(labels)) if labels[j] is not None and j not in missing_indices]
                for i in missing_indices:
                    try:
                        label = self._generate_region_label(
                            entity_summaries_list[i], extended_existing
                        )
                        labels[i] = label
                        extended_existing.append(label)
                    except Exception:
                        fallback = entity_summaries_list[i][0].split("(")[0] if entity_summaries_list[i] else "unknown"
                        labels[i] = fallback
                        extended_existing.append(fallback)

                # De-duplicate: if batch LLM returned same label for multiple regions
                seen_labels = set(existing_regions)
                for i, label in enumerate(labels):
                    if label is not None and label in seen_labels:
                        base = label[:7]
                        n = 2
                        candidate = f"{base}{n}"
                        while candidate in seen_labels and n < 10:
                            n += 1
                            candidate = f"{base}{n}"
                        labels[i] = candidate
                    if label is not None:
                        seen_labels.add(labels[i])

                # Final truncation to 8 chars (safety net)
                for i, label in enumerate(labels):
                    if label is not None and len(label) > 8:
                        labels[i] = label[:8]

                return labels
            except Exception as e:
                logger.warning("Batch label generation failed: %s, falling back to individual", e)

        # Individual calls for < 3 regions or batch failure
        labels = []
        for entity_summaries in entity_summaries_list:
            label = self._generate_region_label(entity_summaries, existing_regions)
            labels.append(label)
            existing_regions = existing_regions + [label]  # Avoid in-place mutation

        return labels

    def _generate_region_labels_batch(
        self,
        entity_summaries_list: list[list[str]],
        existing_regions: list[str],
    ) -> dict[int, str]:
        """Generate labels for all regions in a single LLM call.

        Returns dict of {index: label} for successfully parsed regions.
        """
        # Build batch prompt
        community_lines = []
        for i, entity_summaries in enumerate(entity_summaries_list):
            entity_parts = []
            for s in entity_summaries[:20]:
                entity_parts.append(s)
            community_lines.append(f"社区{i}实体：{', '.join(entity_parts)}")

        existing_str = ", ".join(existing_regions) if existing_regions else "无"
        communities_str = "\n".join(community_lines)

        prompt = (
            "你是一个知识图谱分析师。根据以下社区内的实体列表，为每个社区生成一个简洁的中文标签名。\n\n"
            "要求：\n"
            "- 每个标签8个字以下\n"
            "- 概括该社区实体的共同主题\n"
            "- 不要跟现有脑区重名或语义接近\n"
            "- 只能返回JSON格式：{\"regions\": [{\"id\": 0, \"label\": \"标签1\"}, ...]}\n"
            "- 返回其他任何格式或内容将判定失败\n\n"
            f"现有脑区：{existing_str}\n\n"
            f"{communities_str}"
        )

        # Token truncation
        try:
            from agent.token_calculator import TokenCalculator
            calc = TokenCalculator.get()
            token_count = calc.count_text(prompt)
            context_window = _read_context_window_size()
            if token_count > context_window - 500:
                while len(community_lines) > 1 and token_count > context_window - 500:
                    community_lines.pop()
                    communities_str = "\n".join(community_lines)
                    prompt = (
                        "你是一个知识图谱分析师。根据以下社区内的实体列表，为每个社区生成一个简洁的中文标签名。\n\n"
                        "要求：\n"
                        "- 每个标签8个字以下\n"
                        "- 概括该社区实体的共同主题\n"
                        "- 不要跟现有脑区重名或语义接近\n"
                        "- 只能返回JSON格式：{\"regions\": [{\"id\": 0, \"label\": \"标签1\"}, ...]}\n"
                        "- 返回其他任何格式或内容将判定失败\n\n"
                        f"现有脑区：{existing_str}\n\n"
                        f"{communities_str}"
                    )
                    token_count = calc.count_text(prompt)
        except Exception:
            pass

        # Call LLM
        content = self._call_llm_for_label(prompt)

        # Parse batch response
        try:
            data = json.loads(content.strip())
            if isinstance(data, dict) and "regions" in data:
                result = {}
                for item in data["regions"]:
                    idx = item.get("id")
                    label = str(item.get("label", "")).strip()
                    if idx is not None and label and len(label) <= 8:
                        result[int(idx)] = label
                return result
        except (json.JSONDecodeError, ValueError):
            pass

        # Try regex fallback for batch
        result = {}
        for match in re.finditer(r'"id"\s*:\s*(\d+)\s*,\s*"label"\s*:\s*"([^"]+)"', content):
            idx = int(match.group(1))
            label = match.group(2).strip()
            if label and len(label) <= 8:
                result[idx] = label

        return result


    # ------------------------------------------------------------------
    # Incremental update + edge decay
    # ------------------------------------------------------------------

    def incremental_update(self) -> dict:
        """Incremental update: detect communities → two-phase cleanup/create → update summaries → decay edges.

        Uses dry_run two-phase pattern to solve D-13 non-atomicity.

        Returns:
            dict with keys: regions_created, regions_removed, regions_drifted, regions_updated, edges_disconnected
        """
        try:
            from niu_api.internal.region_detector import CommunityDetector

            from agent.injector.region_sync import REGION_CONFIG_DEFAULTS

            detector = CommunityDetector(self._adapter)
            partition = detector.detect_communities(
                resolution=REGION_CONFIG_DEFAULTS.get("resolution", 1.0),
                min_graph_size=REGION_CONFIG_DEFAULTS.get("min_graph_size", 50),
                min_community_size=REGION_CONFIG_DEFAULTS.get("min_community_size", 100),
            )
            if partition is None or partition.total_regions < 1:
                return {"regions_created": 0, "regions_removed": 0, "regions_drifted": 0,
                        "regions_updated": 0, "edges_disconnected": 0}

            # Two-phase: detect then execute (solves D-13 non-atomicity)
            cleanup_ok = True
            try:
                removed, drifted, drifted_cids = self.cleanup_stale_regions(partition, dry_run=True)
            except Exception as e:
                logger.warning("incremental_update cleanup detection failed: %s", e)
                removed, drifted, drifted_cids = [], [], set()
                cleanup_ok = False

            # Create new regions (skip drifted community partitions)
            created: list[str] = []
            create_ok = True
            try:
                created = self.create_region_nodes(partition, skip_community_ids=drifted_cids)
            except Exception as e:
                logger.warning("incremental_update create_region_nodes failed: %s", e)
                create_ok = False

            # Execute cleanup only if create didn't throw and dry_run succeeded
            # create_ok=True but created=[] is normal (all regions exist), still run cleanup
            actual_removed, actual_drifted = [], []
            if (create_ok or not partition.partitions) and cleanup_ok:
                try:
                    actual_removed, actual_drifted, _ = self.cleanup_stale_regions(partition, dry_run=False)
                except Exception as e:
                    logger.warning("incremental_update cleanup execution failed: %s", e)
            elif not cleanup_ok:
                logger.warning("incremental_update dry_run 失败，跳过 cleanup 执行")
            else:
                logger.warning("incremental_update create_region_nodes 异常，保留旧脑区")

            # Update summaries for stable regions (exclude created and drifted)
            all_regions = self.get_all_regions()
            existing_region_names = []
            try:
                created_set = set(created)
                drifted_set = set(actual_drifted) if cleanup_ok else set()
                existing_region_names = [r.name for r in all_regions
                                         if r.name not in created_set and r.name not in drifted_set]
                self.update_region_summaries(existing_region_names)
            except Exception as e:
                logger.debug("incremental_update update_region_summaries skipped: %s", e)

            # Decay structural edges
            decay_result = self.decay_structural_edges()
            disconnected = decay_result.get("deleted", 0)

            return {
                "regions_created": len(created),
                "regions_removed": len(actual_removed),
                "regions_drifted": len(actual_drifted),
                "regions_updated": len(existing_region_names),
                "edges_disconnected": disconnected,
            }
        except Exception as e:
            logger.warning("incremental_update failed: %s", e)
            return {"regions_created": 0, "regions_removed": 0, "regions_drifted": 0,
                    "regions_updated": 0, "edges_disconnected": 0}

    def decay_structural_edges(self) -> dict:
        """Decay brain region edges — half-life model with floor protection.

        Only decays entity→brainregion attribution edges.
        Knowledge edges (entity→entity) are not affected.
        Anchor edges (brainregion→brainregion) are skipped.
        """
        try:
            from niu_api.internal.lightrag_manager import graph_write_lock

            rag = self._adapter._get_rag()
            if rag is None:
                return {"decayed": 0, "deleted": 0, "protected": 0, "skipped_anchor": 0}

            kg = rag.chunk_entity_relation_graph
            if kg is None:
                return {"decayed": 0, "deleted": 0, "protected": 0, "skipped_anchor": 0}

            nx_graph = kg._graph if hasattr(kg, "_graph") else kg
            if nx_graph is None:
                return {"decayed": 0, "deleted": 0, "protected": 0, "skipped_anchor": 0}

            with graph_write_lock():
                result = _decay_brain_region_edges(nx_graph)

        except Exception as e:
            logger.warning("Edge decay failed: %s", e)
            result = {"decayed": 0, "deleted": 0, "protected": 0, "skipped_anchor": 0}

        logger.info(
            f"[Decay] brain region edges: decayed={result['decayed']}, deleted={result['deleted']}, "
            f"protected={result['protected']}, skipped_anchor={result['skipped_anchor']}"
        )
        return result


def get_default_regions_config() -> list[dict]:
    """Read default brain region definitions from preferences.json.

    Returns list of dicts with keys: label, description, priority.
    Falls back to hardcoded defaults ONLY when preferences.json has no
    brain_regions section at all. If the section exists (even with empty
    defaults list), that configuration is respected.
    """
    try:
        prefs_path = os.path.expanduser("~/.niu/preferences.json")
        with open(prefs_path, "r", encoding="utf-8") as f:
            prefs = json.load(f)
        # Respect explicit configuration — even empty defaults list
        if "brain_regions" in prefs:
            return prefs["brain_regions"].get("defaults", [])
    except Exception:
        pass
    # Fallback ONLY when preferences.json has no brain_regions section
    return [
        {"label": "聊天历史", "description": "日常对话中提炼的偏好、技能和经验记忆", "priority": "medium", "keywords": ["偏好", "习惯", "设置", "配置", "喜欢", "想要"]},
        {"label": "文档库", "description": "用户导入的文档和资料，经解析后入库的知识", "priority": "permanent", "keywords": ["文档", "文件", "PDF", "Word", "Markdown", "笔记"]},
        {"label": "知识体系", "description": "系统化组织的概念、关系和理论体系", "priority": "long", "keywords": ["概念", "理论", "方法", "原理", "定义", "技术"]},
        {"label": "人际关系", "description": "人物实体、关系网络、社交图谱", "priority": "permanent", "keywords": ["人物", "家人", "朋友", "同事", "联系人", "人名"]},
        {"label": "工作事务", "description": "工作相关的项目、任务、决策记录", "priority": "medium", "keywords": ["项目", "任务", "会议", "决策", "工作", "进度"]},
        {"label": "生活事务", "description": "日常生活相关的日程、健康、财务", "priority": "short", "keywords": ["日程", "健康", "财务", "旅行", "生活", "日常"]},
        {"label": "组织机构", "description": "公司、部门、机构等组织实体和关系网络", "priority": "permanent", "keywords": ["公司", "部门", "机构", "组织", "团队", "单位"]},
    ]


def is_default_region(region_name: str) -> bool:
    """Check if a region name is a default region defined in preferences.

    Uses the configured default regions list, not community_id.
    """
    defaults = get_default_regions_config()
    for d in defaults:
        if region_name == f"{d['label']}{REGION_SUFFIX}":
            return True
    return False



def create_default_regions(
    adapter: Any,
    ingester: Any,
    include_category: bool = True,
) -> dict:
    """Create default brain region master nodes.

    If a region already exists, skip it. Each region is linked to
    Niu via 脑区锚点 relation.

    Args:
        adapter: LightRAGAdapter instance.
        ingester: LightRAGIngester instance.
        include_category: Whether to create category regions (default True).

    Returns:
        Dict with created and existing counts.
    """
    from niu_api.internal.lightrag_manager import get_brain_regions

    all_entities: list[dict] = []
    all_relationships: list[dict] = []
    created = 0
    existing = 0

    # Get existing brain regions directly from graph (no LLM call)
    existing_regions = get_brain_regions()

    for region_def in get_default_regions_config():
        region_label = region_def["label"]
        # Skip category regions unless explicitly requested
        if region_def.get("priority") in ("short", "medium") and not include_category:
            continue

        region_name = f"{region_label}{REGION_SUFFIX}"

        # Check if region already exists (direct graph read, no LLM)
        if region_name in existing_regions:
            existing += 1
            continue

        # Collect region entity and anchor relation for batch inject
        description = _encode_description(
            summary=region_def["description"],
            region_id=f"default_{region_label}",
            size=0,
            representative="",
            updated_at=time.time(),
            priority=region_def.get("priority", DEFAULT_PRIORITY),
        )
        all_entities.append({
            "entity_name": region_name,
            "entity_type": REGION_ENTITY_TYPE,
            "description": description,
        })
        all_relationships.append({
            "src_id": NIU_ENTITY,
            "tgt_id": region_name,
            "keywords": ANCHOR_RELATION,
            "description": f"缺省脑区锚点: {region_label}",
            "weight": INITIAL_WEIGHT,
            "source_id": REGION_SOURCE_ID,
            "file_path": REGION_FILE_PATH,
        })
        created += 1

    # Batch inject all default regions in one call
    if all_entities or all_relationships:
        try:
            result = ingester.inject_custom_kg(
                entities=all_entities,
                relationships=all_relationships,
                chunks=[],
                source_id=REGION_SOURCE_ID,
            )
            if isinstance(result, dict) and result.get("status") == "error":
                logger.warning(
                    "批量注入默认脑区失败: %s",
                    result.get("message", "unknown"),
                )
                return {"created": 0, "existing": existing}
            logger.info(
                "批量注入 %d 个默认脑区, %d 条锚点关系",
                len(all_entities),
                len(all_relationships),
            )
        except Exception as e:
            logger.warning(f"批量注入默认脑区失败: {e}")
            return {"created": 0, "existing": existing}

    return {"created": created, "existing": existing}


def assign_entities_to_default_regions(
    adapter: Any,
    entity_keywords: dict[str, list[str]] | None = None,
) -> dict:
    """Assign existing entities to default brain regions based on keywords.

    This is a one-time operation to populate default regions with existing
    entities. After this, entities will naturally accumulate in regions
    through normal knowledge graph operations.

    Args:
        adapter: LightRAGAdapter instance.
        entity_keywords: Optional mapping of entity_name -> keywords for
            precise matching. If not provided, uses heuristic keyword matching.

    Returns:
        Dict with assigned counts per region.
    """
    from niu_api.internal.lightrag_manager import get_brain_regions

    existing_regions = get_brain_regions()
    if not existing_regions:
        return {"assigned": 0, "regions": 0}

    rag = adapter._get_rag()
    if rag is None:
        return {"assigned": 0, "regions": 0}

    kg = rag.chunk_entity_relation_graph
    if kg is None:
        return {"assigned": 0, "regions": 0}

    # Dynamic keyword mapping from config (replaces hardcoded REGION_KEYWORDS)
    _DEFAULT_KEYWORDS_FALLBACK: dict[str, list[str]] = {
        "聊天历史脑区": ["偏好", "习惯", "设置", "配置", "喜欢", "想要"],
        "文档库脑区": ["文档", "文件", "PDF", "Word", "Markdown", "笔记"],
        "知识体系脑区": ["概念", "理论", "方法", "原理", "定义", "技术"],
        "人际关系脑区": ["人物", "家人", "朋友", "同事", "联系人", "人名"],
        "工作事务脑区": ["项目", "任务", "会议", "决策", "工作", "进度"],
        "生活事务脑区": ["日程", "健康", "财务", "旅行", "生活", "日常"],
        "组织机构脑区": ["公司", "部门", "机构", "组织", "团队", "单位"],
    }
    REGION_KEYWORDS: dict[str, list[str]] = {}
    for region_def in get_default_regions_config():
        region_name = f"{region_def['label']}{REGION_SUFFIX}"
        keywords = region_def.get("keywords", [])
        if not keywords:
            keywords = _DEFAULT_KEYWORDS_FALLBACK.get(region_name, [])
        if keywords:
            REGION_KEYWORDS[region_name] = keywords

    assigned_counts: dict[str, int] = {}
    all_relationships: list[dict] = []

    # Take a snapshot under read lock to prevent RuntimeError from concurrent writes
    from niu_api.internal.lightrag_manager import graph_read_lock
    with graph_read_lock():
        snapshot = kg._graph.copy()

    # Iterate all entity nodes
    for node_id, node_data in snapshot.nodes(data=True):
        if not isinstance(node_data, dict):
            continue
        entity_name = node_data.get("entity_name", node_id)
        entity_desc = node_data.get("description", "")

        # Skip region nodes themselves
        if entity_name.endswith("脑区"):
            continue

        # Matching logic: keyword matching + description similarity
        best_region = None
        best_score = 0.0

        for region_name, keywords in REGION_KEYWORDS.items():
            if region_name not in existing_regions:
                continue

            # Keyword matching
            score = 0.0
            for kw in keywords:
                if kw in entity_name or kw in entity_desc:
                    score += 1.0

            if score > best_score:
                best_score = score
                best_region = region_name

        # If matched, create belongs_to relation
        if best_region and best_score > 0:
            all_relationships.append({
                "src_id": best_region,
                "tgt_id": entity_name,
                "keywords": BELONGS_TO_RELATION,
                "description": f"{entity_name} 属于 {best_region}",
                "weight": INITIAL_WEIGHT,
                "source_id": REGION_SOURCE_ID,
                "file_path": REGION_FILE_PATH,
            })
            assigned_counts[best_region] = assigned_counts.get(best_region, 0) + 1

    # Batch inject relations
    if all_relationships:
        try:
            from niu_api.internal.lightrag_adapter import LightRAGIngester
            ingester = LightRAGIngester()
            ingester.inject_custom_kg(
                entities=[],
                relationships=all_relationships,
                chunks=[],
                source_id=REGION_SOURCE_ID,
            )
            logger.info(
                "批量注入实体-脑区关系: %d 条",
                len(all_relationships),
            )
        except Exception as e:
            logger.warning(f"批量注入实体-脑区关系失败: {e}")

    # Update size metadata for default regions that got new assignments
    if assigned_counts:
        try:
            from niu_api.internal.lightrag_adapter import LightRAGIngester
            ingester = LightRAGIngester()

            # Get current descriptions for all default regions
            list_result = adapter.list_entities(
                list_type="entities", entity_type=REGION_ENTITY_TYPE, limit=1000
            )
            if isinstance(list_result, dict) and list_result.get("status") == "ok":
                update_entities = []
                for entity in list_result.get("data", []):
                    name = entity.get("id") or entity.get("entity_name", "")
                    if name in assigned_counts:
                        desc = entity.get("description", "")
                        parsed = _parse_description(desc)
                        # D-15 fix: Use actual member count instead of cumulative size
                        from niu_api.internal.lightrag_manager import get_region_members as lightrag_get_region_members
                        actual_members = lightrag_get_region_members(name)
                        priority = parse_priority_from_description(desc)
                        updated_desc = _encode_description(
                            summary=parsed.get("summary", ""),
                            region_id=parsed.get("region_id", ""),
                            size=len(actual_members),
                            representative=parsed.get("representative", ""),
                            updated_at=time.time(),
                            priority=priority,
                        )
                        update_entities.append({
                            "entity_name": name,
                            "entity_type": REGION_ENTITY_TYPE,
                            "description": updated_desc,
                        })
                if update_entities:
                    ingester.inject_custom_kg(
                        entities=update_entities,
                        relationships=[],
                        chunks=[],
                        source_id=REGION_SOURCE_ID,
                    )
                    logger.info(
                        "更新 %d 个默认脑区的 size 元数据",
                        len(update_entities),
                    )
        except Exception as e:
            logger.warning("更新默认脑区 size 失败: %s", e)

    return {"assigned": sum(assigned_counts.values()), "regions": len(assigned_counts)}
