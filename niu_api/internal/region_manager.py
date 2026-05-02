"""
Brain Region Master Node Manager

Creates and manages brain:region:{name} entities in the LightRAG knowledge graph
for each Leiden community. Each region master node serves as:
- Semantic pointer for search
- Search entry via brain_region_anchor relation from brain:Niu
- Metadata container (brain_meta_* attributes in description)

M2 module: Region node lifecycle, M1 provides community detection.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from niu_api.internal.region_detector import CommunityDetectionResult

logger = logging.getLogger(__name__)

# ============== Constants ==============

# Namespace prefix for brain region entities
REGION_PREFIX = "brain:region:"

# Entity type for brain region master nodes
REGION_ENTITY_TYPE = "BrainRegion"

# Relation keywords
ANCHOR_RELATION = "brain_region_anchor"
BELONGS_TO_RELATION = "belongs_to"

# Source identifiers for injected data
REGION_SOURCE_ID = "brain"
REGION_FILE_PATH = "brain://region"

# Self entity name (anchor point for all regions)
NIU_ENTITY = "brain:Niu"

# Maximum number of entity descriptions to include in region summary
MAX_SUMMARY_ENTITIES = 5


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class BrainRegionInfo:
    """Brain region master node information"""

    name: str  # "brain:region:编程开发"
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
    """Encode region metadata into description using | separator.

    LightRAG stores custom attributes as flat text in the description field
    (GraphML limitation). The brain_meta_* attributes are embedded using
    | separators, following the existing pattern from brain_graph.py.
    """
    parts = [
        summary,
        f"brain_meta_region_id:{region_id}",
        f"brain_meta_size:{size}",
        f"brain_meta_representative:{representative}",
        f"brain_meta_updated_at:{int(updated_at)}",
    ]
    return " | ".join(parts)


def _parse_description(description: str) -> dict[str, str]:
    """Parse brain_meta_* attributes from flat description text.

    Returns:
        Dict with keys: summary, region_id, size, representative, updated_at
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

    parts = description.split(" | ")
    summary_parts: list[str] = []

    for part in parts:
        part = part.strip()
        match = re.match(r"brain_meta_(\w+):(.+)", part)
        if match:
            key = match.group(1)
            value = match.group(2)
            if key in result:
                result[key] = value
        else:
            summary_parts.append(part)

    result["summary"] = " | ".join(summary_parts)
    return result


# ---------------------------------------------------------------------------
# Region Manager
# ---------------------------------------------------------------------------


class RegionManager:
    """Brain region master node lifecycle management

    Creates brain:region:{name} entities for each Leiden community,
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

        For each community:
        1. Collect entity names+descriptions (skip brain:region:* nodes)
        2. Generate region name + summary via _summarize_region()
        3. inject_entity("brain:region:{name}", "BrainRegion", summary)
           with brain_meta_* attributes in description
        4. inject_relation("brain:Niu" -> "brain:region:{name}",
           keywords="brain_region_anchor", weight=1.0)
        5. For each member: inject_relation("brain:region:{name}" -> entity,
           keywords="belongs_to", weight=0.8)

        Args:
            partition_result: Community detection result from M1

        Returns:
            List of created region names (e.g. ["brain:region:Python", ...])
        """
        created_regions: list[str] = []

        for partition in partition_result.partitions:
            # Step 1: Filter out existing brain:region:* nodes
            members = [
                name
                for name in partition.entity_names
                if not name.startswith(REGION_PREFIX)
            ]

            if not members:
                logger.debug(
                    "社区 %d 无有效成员（全为 brain:region:* 节点），跳过",
                    partition.region_id,
                )
                continue

            # Build entity summaries for region naming
            entity_summaries = self._build_entity_summaries(
                members, partition.entity_types, partition.entity_name_to_type
            )

            # Step 2: Generate region name + summary
            region_label, region_summary = self._summarize_region(entity_summaries)

            # Pick representative: first entity name (highest-degree in community)
            # Sanitize: replace | with - to avoid breaking description parsing
            representative = members[0].replace("|", "-") if members else ""
            community_id = f"community_{partition.region_id}"
            now = time.time()

            # Full region entity name
            region_name = f"{REGION_PREFIX}{region_label}"

            # Step 3: Inject region master node
            description = _encode_description(
                summary=region_summary,
                region_id=community_id,
                size=len(members),
                representative=representative,
                updated_at=now,
            )

            entity_result = self._ingester.inject_custom_kg(
                entities=[{
                    "entity_name": region_name,
                    "entity_type": REGION_ENTITY_TYPE,
                    "description": description,
                }],
                relationships=[],
                chunks=[],
                source_id=REGION_SOURCE_ID,
            )

            if isinstance(entity_result, dict) and entity_result.get("status") == "error":
                logger.warning(
                    "注入脑区实体失败: %s — %s",
                    region_name,
                    entity_result.get("message", "unknown"),
                )
                continue

            # Step 4: Anchor relation from brain:Niu to region
            self._ingester.inject_custom_kg(
                entities=[],
                relationships=[
                    {
                        "src_id": NIU_ENTITY,
                        "tgt_id": region_name,
                        "keywords": ANCHOR_RELATION,
                        "description": f"Brain region anchor: {region_label}",
                        "weight": 1.0,
                        "source_id": REGION_SOURCE_ID,
                        "file_path": REGION_FILE_PATH,
                    }
                ],
                chunks=[],
                source_id=REGION_SOURCE_ID,
            )

            # Step 5: belongs_to relations from region to each member
            member_relations = []
            for member in members:
                member_relations.append(
                    {
                        "src_id": region_name,
                        "tgt_id": member,
                        "keywords": BELONGS_TO_RELATION,
                        "description": f"{member} belongs to region {region_label}",
                        "weight": 0.8,
                        "source_id": REGION_SOURCE_ID,
                        "file_path": REGION_FILE_PATH,
                    }
                )

            if member_relations:
                self._ingester.inject_custom_kg(
                    entities=[],
                    relationships=member_relations,
                    chunks=[],
                    source_id=REGION_SOURCE_ID,
                )

            created_regions.append(region_name)
            logger.info(
                "创建脑区节点: %s (社区 %d, %d 成员, 代表: %s)",
                region_name,
                partition.region_id,
                len(members),
                representative,
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
        for region_name in region_names:
            # Step 1: Get current members
            members = self.get_region_members(region_name)

            if not members:
                logger.debug("脑区 %s 无成员，跳过摘要更新", region_name)
                continue

            # Step 2: Get current region info to preserve metadata
            # Use list_entities with entity_type filter to find the region node
            # (explore_node(depth=0) may return no nodes for newly created regions)
            current_desc = ""
            list_result = self._adapter.list_entities(
                list_type="entities", entity_type=REGION_ENTITY_TYPE, limit=1000
            )
            if isinstance(list_result, dict) and list_result.get("status") == "ok":
                for entity in list_result.get("data", []):
                    if entity.get("id") == region_name or entity.get("entity_name") == region_name:
                        current_desc = entity.get("description", "")
                        break

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

            # Build entity summaries for re-generation
            entity_summaries = [f"{m}" for m in members]
            _, region_summary = self._summarize_region(entity_summaries)

            now = time.time()
            description = _encode_description(
                summary=region_summary,
                region_id=community_id,
                size=len(members),
                representative=representative,
                updated_at=now,
            )

            # Step 3: Update (overwrite) region master node
            self._ingester.inject_custom_kg(
                entities=[{
                    "entity_name": region_name,
                    "entity_type": REGION_ENTITY_TYPE,
                    "description": description,
                }],
                relationships=[],
                chunks=[],
                source_id=REGION_SOURCE_ID,
            )

            logger.info(
                "更新脑区摘要: %s (%d 成员)", region_name, len(members)
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

            # Extract label from entity name: brain:region:{label}
            label = entity_name
            if entity_name.startswith(REGION_PREFIX):
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
        """Get members by following belongs_to relationships from region node

        No longer async — see get_all_regions for rationale.

        Uses explore_node(region_name, depth=1) then filter belongs_to edges

        Args:
            region_name: Full region entity name (e.g. "brain:region:Python")

        Returns:
            List of entity names that belong to this region
        """
        explore_result = self._adapter.explore_node(region_name, depth=1)

        if not explore_result:
            return []

        members: list[str] = []
        edges = explore_result.get("edges", [])

        for edge in edges:
            # belongs_to edges go from region -> member
            source = edge.get("source", "")
            target = edge.get("target", "")
            relation = edge.get("relation", "")

            if relation == BELONGS_TO_RELATION:
                # region -> member: source is region, target is member
                if source == region_name:
                    members.append(target)
                # member -> region: reverse direction
                elif target == region_name:
                    members.append(source)

        return members

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
        for region in existing_regions:
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

        # Sanitize: replace | with - to avoid breaking description parsing
        # (| is the separator in _encode_description / _parse_description)
        region_label = region_label.replace("|", "-")

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

            detector = CommunityDetector(self._adapter)
            partition = detector.detect_communities(
                resolution=REGION_CONFIG_DEFAULTS.get("resolution", 1.0) if "REGION_CONFIG_DEFAULTS" in dir() else 1.0,
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
        """Decay and disconnect low-weight structural edges (_region: and _session: prefixes).

        Args:
            regions: List of BrainRegionInfo to process.
            decay_factor: Weight multiplier per decay cycle (default 0.5).
            threshold: Minimum weight before disconnect (default 0.1).

        Returns:
            Number of disconnected edges.
        """
        disconnected = 0
        try:
            rag = self._adapter._get_rag()
            if rag is None:
                return 0

            kg = rag.chunk_entity_relation_graph
            if kg is None:
                return 0

            for region in regions:
                try:
                    neighbors = kg.get_neighbors(region.name)
                except AttributeError:
                    return 0

                if not neighbors:
                    continue

                for neighbor_id, edge_data in list(neighbors.items()):
                    if not isinstance(edge_data, dict):
                        continue
                    keywords = edge_data.get("keywords", "")
                    if keywords.startswith("_region:") or keywords.startswith("_session:"):
                        old_weight = float(edge_data.get("weight", 1.0))
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


# ── Default Region Definitions ──────────────────────────────────

DEFAULT_REGIONS: dict[str, dict] = {
    "聊天历史": {
        "description": "日常对话中提炼的偏好、技能和经验记忆",
    },
    "文档库": {
        "description": "用户导入的文档和资料，经解析后入库的知识",
    },
    "知识体系": {
        "description": "系统化组织的概念、关系和理论体系",
    },
}


def create_default_regions(adapter: Any, ingester: Any) -> dict:
    """Create default brain region master nodes.

    If a region already exists, skip it. Each region is linked to
    brain:Niu via brain_region_anchor relation.

    Args:
        adapter: LightRAGAdapter instance.
        ingester: LightRAGIngester instance.

    Returns:
        Dict with created and existing counts.
    """
    created = 0
    existing = 0

    for region_label, config in DEFAULT_REGIONS.items():
        region_name = f"{REGION_PREFIX}{region_label}"

        # Check if region already exists
        try:
            search_result = adapter.query_data(
                query=region_label, mode="local", top_k=3, keywords=[region_label],
            )
            found = False
            if search_result and isinstance(search_result, dict):
                data = search_result.get("data", {})
                entities = data.get("entities", []) if isinstance(data, dict) else []
                found = any(e.get("entity_name", "") == region_name for e in entities)
            if found:
                existing += 1
                continue
        except Exception:
            pass  # Proceed to create

        # Create region entity
        try:
            ingester.inject_custom_kg(
                entities=[{
                    "entity_name": region_name,
                    "entity_type": REGION_ENTITY_TYPE,
                    "description": config["description"],
                }],
                relationships=[],
                chunks=[],
                source_id=REGION_SOURCE_ID,
            )
            # Link to brain:Niu via brain_region_anchor
            ingester.inject_custom_kg(
                entities=[],
                relationships=[{
                    "src_id": NIU_ENTITY,
                    "tgt_id": region_name,
                    "keywords": ANCHOR_RELATION,
                    "description": f"缺省脑区锚点: {region_label}",
                    "source_id": REGION_SOURCE_ID,
                    "file_path": REGION_FILE_PATH,
                }],
                chunks=[],
                source_id=REGION_SOURCE_ID,
            )
            created += 1
        except Exception as e:
            logger.warning(f"Failed to create default region {region_label}: {e}")

    return {"created": created, "existing": existing}
