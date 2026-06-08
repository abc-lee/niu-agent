"""
Brain Region Master Node Manager

Creates and manages brain region entities in the LightRAG knowledge graph
for each Leiden community. Each region master node serves as:
- Semantic pointer for search
- Search entry via brain_region_anchor relation from Niu
- Metadata container (brain_meta_* attributes in description)

Entity names use natural language format (e.g., "编程开发脑区") instead of
colon-prefix format (e.g., "brain:region:编程开发") — DEPRECATED, kept only for
reading pre-migration data.

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

from niu_api.internal.region_detector import CommunityDetectionResult

logger = logging.getLogger(__name__)

# ============== Constants ==============

# Region entity name format: "{label}脑区" (natural language)
# e.g., "编程开发脑区", "聊天历史脑区"
REGION_SUFFIX = "脑区"

# DEPRECATED: Legacy prefix for backward compat when reading existing graph data.
# New code must use natural language format ("XXX脑区") only.
# This constant exists solely to read pre-migration data from the graph.
REGION_PREFIX = "brain:region:"

# Entity type for brain region master nodes
REGION_ENTITY_TYPE = "brainregion"

# Relation keywords
ANCHOR_RELATION = "brain_region_anchor"
BELONGS_TO_RELATION = "_region:contains"
# Legacy relation keyword (pre-unification). Kept for backward compat
# when reading edges from existing graph data.
_LEGACY_BELONGS_TO = "belongs_to"

# Source identifiers for injected data
REGION_SOURCE_ID = "brain"
REGION_FILE_PATH = "brain://region"

# Self entity name (natural language, no prefix)
NIU_ENTITY = "Niu"

# Maximum number of entity descriptions to include in region summary
MAX_SUMMARY_ENTITIES = 5

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
    ]
    return "<SEP>".join(parts)


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
        match = re.match(r"brain_meta_(\w+):(.+)", part)
        if match:
            key = match.group(1)
            value = match.group(2)
            result[key] = value
        else:
            summary_parts.append(part)

    result["summary"] = "<SEP>".join(summary_parts)
    return result


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
    ) -> list[str]:
        """Create master nodes + relationships for each community

        Uses batch injection: collects all entities and relationships first,
        then calls inject_custom_kg once. This avoids serially blocking
        the LightRAG event loop with N individual calls per community.

        Args:
            partition_result: Community detection result from M1

        Returns:
            List of created region names (e.g. ["Python脑区", ...])
        """
        all_entities: list[dict] = []
        all_relationships: list[dict] = []
        created_regions: list[str] = []

        for partition in partition_result.partitions:
            # Step 1: Filter out existing region nodes (both natural language and legacy format)
            members = [
                name
                for name in partition.entity_names
                if not (name.endswith(REGION_SUFFIX) or name.startswith(REGION_PREFIX))
            ]

            if not members or len(members) < MIN_COMMUNITY_SIZE:
                logger.debug(
                    "社区 %d 成员数 %d < %d，跳过",
                    partition.region_id,
                    len(members),
                    MIN_COMMUNITY_SIZE,
                )
                continue

            # Build entity summaries for region naming
            entity_summaries = self._build_entity_summaries(
                members, partition.entity_types, partition.entity_name_to_type
            )

            # Step 2: Generate region name + summary
            region_label, region_summary = self._summarize_region(entity_summaries)

            # Pick representative: first entity name (highest-degree in community)
            # Sanitize: replace <SEP> and | with - to avoid breaking description parsing
            representative = members[0].replace("<SEP>", "-").replace("|", "-") if members else ""
            community_id = f"community_{partition.region_id}"
            now = time.time()

            # Full region entity name
            region_name = f"{region_label}{REGION_SUFFIX}"

            # Step 3: Collect region master node entity
            description = _encode_description(
                summary=region_summary,
                region_id=community_id,
                size=len(members),
                representative=representative,
                updated_at=now,
            )

            all_entities.append({
                "entity_name": region_name,
                "entity_type": REGION_ENTITY_TYPE,
                "description": description,
            })

            # Step 4: Collect anchor relation from Niu to region
            all_relationships.append({
                "src_id": NIU_ENTITY,
                "tgt_id": region_name,
                "keywords": ANCHOR_RELATION,
                "description": f"Brain region anchor: {region_label}",
                "weight": 0.5,  # Unified initial weight (was 1.0)
                "source_id": REGION_SOURCE_ID,
                "file_path": REGION_FILE_PATH,
            })

            # Step 5: Collect belongs_to relations from region to each member
            for member in members:
                all_relationships.append({
                    "src_id": region_name,
                    "tgt_id": member,
                    "keywords": BELONGS_TO_RELATION,
                    "description": f"{member} belongs to region {region_label}",
                    "weight": 0.5,  # Unified initial weight (was 0.8)
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
                chunks=[],
                source_id=REGION_SOURCE_ID,
            )
            if isinstance(result, dict) and result.get("status") == "error":
                logger.warning(
                    "批量注入脑区实体失败: %s (collected %d regions)",
                    result.get("message", "unknown"),
                    len(created_regions),
                )
                return []
            logger.info(
                "批量注入 %d 个脑区实体, %d 条关系",
                len(all_entities),
                len(all_relationships),
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
        2. Re-generate summary via _summarize_region()
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
            representative = members[0] if members else ""

            # Preserve dynamic metadata keys (e.g. shrink_count) that
            # _encode_description does not include in its standard 5 fields
            STANDARD_KEYS = {"summary", "region_id", "size", "representative", "updated_at"}
            extra_meta = {
                k: v for k, v in parsed.items()
                if k not in STANDARD_KEYS and v
            }

            # Build entity summaries with type labels from graph
            entity_summaries = self._build_entity_summaries(members, set(), {})
            _, region_summary = self._summarize_region(entity_summaries)

            now = time.time()
            description = _encode_description(
                summary=region_summary,
                region_id=community_id,
                size=len(members),
                representative=representative,
                updated_at=now,
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

            # Extract label from entity name: "{label}脑区" or legacy "brain:region:{label}"
            label = entity_name
            if entity_name.endswith(REGION_SUFFIX):
                label = entity_name[: -len(REGION_SUFFIX)]
            elif entity_name.startswith(REGION_PREFIX):
                label = entity_name[len(REGION_PREFIX):]

            regions.append(
                BrainRegionInfo(
                    name=entity_name,
                    label=label,
                    community_id=parsed.get("region_id", ""),
                    description=parsed.get("summary", ""),
                    size=int(parsed.get("size", "0") or "0"),
                    representative=parsed.get("representative", ""),
                    members=[],  # Members not included in list_entities result
                    updated_at=float(parsed.get("updated_at", "0") or "0"),
                )
            )

        return regions

    def get_region_members(self, region_name: str) -> list[str]:
        """Get members by reading _region:contains edges from NetworkX graph.

        Delegates to lightrag_manager.get_region_members() which directly
        reads the in-memory graph — more reliable than explore_node.
        """
        from niu_api.internal.lightrag_manager import get_region_members as lightrag_get_region_members
        return lightrag_get_region_members(region_name)

    def cleanup_stale_regions(
        self,
        current_partition: CommunityDetectionResult,
    ) -> list[str]:
        """Remove region master nodes that no longer exist in current partition

        No longer async — see get_all_regions for rationale.

        Compare current_partition community_ids with existing BrainRegion entities.
        Delete stale nodes and their belongs_to relationships.

        Args:
            current_partition: Current community detection result

        Returns:
            List of removed region entity names
        """
        # Get current community IDs from partition
        current_community_ids: set[str] = set()
        for partition in current_partition.partitions:
            current_community_ids.add(f"community_{partition.region_id}")

        # Get all existing regions
        existing_regions = self.get_all_regions()

        removed: list[str] = []
        # NOTE: delete_entity calls call_async individually. This is acceptable
        # because stale regions are typically very few (0-3), and delete is fast
        # (just node/edge removal, no embedding computation). If this becomes a
        # bottleneck, a batch delete API would be needed in LightRAG.
        for region in existing_regions:
            # Protect default regions (defined in preferences.json)
            if is_default_region(region.name):
                logger.debug("保护默认脑区: %s", region.name)
                continue
            if region.community_id not in current_community_ids:
                # Delete stale region
                delete_result = self._adapter.delete_entity(region.name)
                if isinstance(delete_result, dict) and delete_result.get("status") == "ok":
                    removed.append(region.name)
                    logger.info(
                        "删除过时脑区: %s (community_id=%s)",
                        region.name,
                        region.community_id,
                    )
                else:
                    logger.warning(
                        "删除过时脑区失败: %s — %s",
                        region.name,
                        delete_result.get("message", "unknown") if isinstance(delete_result, dict) else "error",
                    )

        if removed:
            logger.info("共清理 %d 个过时脑区节点", len(removed))
        return removed

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
                            "weight": 0.5,  # Unified initial weight
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
                updated_desc = _encode_description(
                    summary=parsed.get("summary", ""),
                    region_id=region.community_id,
                    size=current_size,
                    representative=region.representative,
                    updated_at=now,
                )
                # Append shrink_count + preserve other dynamic metadata
                updated_desc += f"<SEP>brain_meta_shrink_count:{shrink_count}"
                STANDARD_KEYS = {"summary", "region_id", "size", "representative", "updated_at", "shrink_count"}
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

    def _summarize_region(
        self,
        entity_summaries: list[str],
    ) -> tuple[str, str]:
        """Generate region name and summary from entity descriptions

        Uses a heuristic approach (no LLM call in M2):
        1. Count entity types in the community
        2. Pick the most common type as the region category
        3. Use the representative entity name as the region label
        4. Build summary from top entity names

        Args:
            entity_summaries: ["Python(skill): Python编程语言...", ...]

        Returns:
            (region_name, region_summary)
            Example: ("Python", "Python(skill)、Django(framework)、FastAPI(framework)")
        """
        if not entity_summaries:
            return ("unknown", "空区域")

        # Parse types from summaries: "Name(type)" format
        type_counts: dict[str, int] = {}
        entity_names: list[str] = []

        for summary in entity_summaries:
            # Extract name and type from "Name(type)" format
            match = re.match(r"([^(]+)\(([^)]+)\)", summary)
            if match:
                name = match.group(1).strip()
                etype = match.group(2).strip()
                type_counts[etype] = type_counts.get(etype, 0) + 1
                entity_names.append(name)
            else:
                # Fallback: treat whole string as name
                entity_names.append(summary.strip())
                type_counts["unknown"] = type_counts.get("unknown", 0) + 1

        if not entity_names:
            return ("unknown", "空区域")

        # Heuristic 1: Use the first entity (representative) as region label
        region_label = entity_names[0]

        # Sanitize: replace <SEP> and | with - to avoid breaking description parsing
        region_label = region_label.replace("<SEP>", "-").replace("|", "-")

        # Heuristic 2: Build summary from top MAX_SUMMARY_ENTITIES entities
        top_summaries = entity_summaries[:MAX_SUMMARY_ENTITIES]
        summary_parts: list[str] = []
        for s in top_summaries:
            # Extract just the name(type) portion for the summary
            match = re.match(r"([^(]+\([^)]+\))", s)
            if match:
                summary_parts.append(match.group(1))
            else:
                summary_parts.append(s)

        region_summary = "、".join(summary_parts)

        # Add ellipsis if there are more entities
        if len(entity_summaries) > MAX_SUMMARY_ENTITIES:
            region_summary += f"等{len(entity_summaries)}个实体"

        return (region_label, region_summary)

    # ------------------------------------------------------------------
    # Incremental update + edge decay
    # ------------------------------------------------------------------

    def incremental_update(self) -> dict:
        """Incremental update: detect new communities → create regions → update summaries → decay edges.

        Returns:
            dict with keys: regions_created, regions_removed, regions_updated, edges_disconnected
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
                return {"regions_created": 0, "regions_removed": 0, "regions_updated": 0, "edges_disconnected": 0}

            # Create new region nodes
            created = self.create_region_nodes(partition)

            # Cleanup stale regions
            removed = self.cleanup_stale_regions(partition)

            # Update summaries for all current regions
            all_regions = self.get_all_regions()
            region_names = [r.name for r in all_regions]
            self.update_region_summaries(region_names)

            # Decay structural edges
            disconnected = self._decay_structural_edges(all_regions)

            return {
                "regions_created": len(created),
                "regions_removed": len(removed),
                "regions_updated": len(all_regions),
                "edges_disconnected": disconnected,
            }
        except Exception as e:
            logger.warning("incremental_update failed: %s", e)
            return {"regions_created": 0, "regions_removed": 0, "regions_updated": 0, "edges_disconnected": 0}

    def _decay_structural_edges(
        self,
        regions: list[BrainRegionInfo],
        decay_factor: float = 0.5,
        threshold: float = 0.1,
    ) -> int:
        """Decay and disconnect low-weight structural edges.

        Handles all brain region related edges:
        - _region:contains (region contains members)
        - _session:* (session related)
        - brain_region_anchor (region anchor)

        Args:
            regions: List of BrainRegionInfo to process.
            decay_factor: Weight multiplier per decay cycle (default 0.5).
            threshold: Minimum weight before disconnect (default 0.1).

        Returns:
            Number of disconnected edges.
        """
        disconnected = 0
        try:
            from niu_api.internal.lightrag_manager import graph_write_lock

            rag = self._adapter._get_rag()
            if rag is None:
                return 0

            kg = rag.chunk_entity_relation_graph
            if kg is None:
                return 0

            # Brain region related edge keyword prefixes
            REGION_EDGE_PREFIXES = ("_region:", "_session:", "brain_region_")

            # NOTE: graph_write_lock only synchronizes with graph_read_lock holders.
            # call_async-based writes (ainsert_custom_kg, adelete_by_entity, etc.)
            # do NOT acquire this lock — they run in the asyncio loop. This means
            # there is a theoretical race window where call_async modifies the
            # NetworkX graph while we iterate edges under graph_write_lock.
            # In practice this is safe because:
            # 1. _decay_structural_edges runs infrequently (every 6h sync cycle)
            # 2. call_async writes are serialized in the asyncio loop
            # 3. If RuntimeError occurs, the except block handles it gracefully
            with graph_write_lock():
                for region in regions:
                    try:
                        neighbors = kg.get_neighbors(region.name)
                    except AttributeError:
                        continue

                    if not neighbors:
                        continue

                    for neighbor_id, edge_data in list(neighbors.items()):
                        if not isinstance(edge_data, dict):
                            continue
                        keywords = edge_data.get("keywords", "")
                        # Process all brain region related edges
                        if any(keywords.startswith(prefix) for prefix in REGION_EDGE_PREFIXES):
                            old_weight = float(edge_data.get("weight", 0.5))
                            new_weight = old_weight * decay_factor
                            if new_weight < threshold:
                                try:
                                    kg.remove_edge(region.name, neighbor_id)
                                except Exception:
                                    pass
                                disconnected += 1
                            else:
                                edge_data["weight"] = new_weight
        except Exception as e:
            logger.warning("Edge decay failed: %s", e)

        return disconnected


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
        {"label": "聊天历史", "description": "日常对话中提炼的偏好、技能和经验记忆", "priority": "core"},
        {"label": "文档库", "description": "用户导入的文档和资料，经解析后入库的知识", "priority": "core"},
        {"label": "知识体系", "description": "系统化组织的概念、关系和理论体系", "priority": "core"},
        {"label": "人际关系", "description": "人物实体、关系网络、社交图谱", "priority": "category"},
        {"label": "工作事务", "description": "工作相关的项目、任务、决策记录", "priority": "category"},
        {"label": "生活事务", "description": "日常生活相关的日程、健康、财务", "priority": "category"},
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
    Niu via brain_region_anchor relation.

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
        if region_def.get("priority") == "category" and not include_category:
            continue

        region_name = f"{region_label}{REGION_SUFFIX}"

        # Check if region already exists (direct graph read, no LLM)
        if region_name in existing_regions:
            existing += 1
            continue

        # Collect region entity and anchor relation for batch inject
        all_entities.append({
            "entity_name": region_name,
            "entity_type": REGION_ENTITY_TYPE,
            "description": region_def["description"],
        })
        all_relationships.append({
            "src_id": NIU_ENTITY,
            "tgt_id": region_name,
            "keywords": ANCHOR_RELATION,
            "description": f"缺省脑区锚点: {region_label}",
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

    # Region keyword mapping for heuristic matching
    REGION_KEYWORDS = {
        "聊天历史脑区": ["偏好", "习惯", "设置", "配置", "喜欢", "想要"],
        "文档库脑区": ["文档", "文件", "PDF", "Word", "Markdown", "笔记"],
        "知识体系脑区": ["概念", "理论", "方法", "原理", "定义", "技术"],
        "人际关系脑区": ["人物", "家人", "朋友", "同事", "联系人", "人名"],
        "工作事务脑区": ["项目", "任务", "会议", "决策", "工作", "进度"],
        "生活事务脑区": ["日程", "健康", "财务", "旅行", "生活", "日常"],
    }

    assigned_counts: dict[str, int] = {}
    all_relationships: list[dict] = []

    # Iterate all entity nodes
    for node_id, node_data in kg._graph.nodes(data=True):
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
                "weight": 0.5,
                "source_id": REGION_SOURCE_ID,
                "file_path": REGION_FILE_PATH,
            })
            assigned_counts[best_region] = assigned_counts.get(best_region, 0) + 1

    # Batch inject relations
    if all_relationships:
        try:
            from niu_api.internal.lightrag_ingester import LightRAGIngester
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

    return {"assigned": sum(assigned_counts.values()), "regions": len(assigned_counts)}
