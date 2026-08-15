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
        # 真实配置在 ~/.niu/config/user-config.json（niu_api.config.CONFIG_PATH），
        # 与 agent/subagent.py:117-120 的正确读法一致。旧路径 <项目根>/config/
        # user-config.json 被 .gitignore 排除、不存在，永远 fallback 200000——
        # 用户改的 contextWindowSize 对脑区逻辑从不生效（与 preload-assistant.js:8 同类 bug）。
        from niu_api.config import CONFIG_PATH
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as f:
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
    "permanent": 360,  # 永久脑区半衰期 360 天，与 long 一致；脑区节点本身不被 dissolve 删除
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

            if total_degree <= 1:
                # 孤立实体：保底保护（避免变孤岛）
                # 永久脑区与普通脑区一致——永久脑区只是脑区节点本身不删，
                # 实体归属边的衰减逻辑与普通脑区完全一致
                new_weight = max(new_weight, FLOOR_WEIGHT)
                nx_graph.edges[region_key, entity_key]["weight"] = new_weight
                decayed += 1
                protected += 1
            elif new_weight < FLOOR_WEIGHT:
                # 总边数>=2 + 低于保底 → 删除
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
                logger.info(
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


def _read_region_raw_descriptions(kg) -> dict[str, str]:
    """图快照直读所有脑区（entity_type=brainregion）的原始 description。

    R11 读清洗断裂修复（P2 拍板）：list_entities 经 `_clean_description`
    （lightrag_adapter L90-117）会剥掉 brain_meta_* 元数据 → region_id /
    priority / shrink_count / size 解析失败回默认 → shrink_count 恒 0 →
    dissolve 永不触发。本 helper 绕开清洗，从图快照直读原始描述。

    模式同 update_default_region_sizes（graph_read_lock + kg._graph.copy()
    + nodes.get(name.lower())）——节点键小写（LightRAG graph 节点 id 全部
    小写）——返回 dict 的键为小写节点键，调用方用 region_name.lower() 查找。

    全量枚举 entity_type==brainregion 节点（含配置外/幽灵脑区——防 dissolve
    对配置外脑区失效——helper 是 dissolve 的 shrink_count 真实值来源）。

    Args:
        kg: LightRAG chunk_entity_relation_graph（含 _graph 的 NetworkX 图）。

    Returns:
        小写节点键 -> 原始 description 的映射。图不可用/读取异常 → {}。
    """
    if kg is None:
        return {}
    nx_graph = kg._graph if hasattr(kg, "_graph") else kg
    if nx_graph is None:
        return {}

    # 方法内 import（同 _has_isolated_member——lightrag_manager 模块级
    # import region_manager，此处避免循环 import）
    from niu_api.internal.lightrag_manager import graph_read_lock

    try:
        with graph_read_lock():
            snapshot = nx_graph.copy()
        raw: dict[str, str] = {}
        for node_key, node_data in snapshot.nodes(data=True):
            etype = node_data.get("entity_type", "")
            if etype and str(etype).lower() == REGION_ENTITY_TYPE:
                desc = node_data.get("description", "")
                if desc:
                    raw[node_key] = desc
        return raw
    except Exception:
        logger.warning(
            "_read_region_raw_descriptions 图快照读取失败，返回空映射（元数据保真降级）",
            exc_info=True,
        )
        return {}


# ---------------------------------------------------------------------------
# R14 衰减门控（P13 用户拍板——算法调度改动）
# ---------------------------------------------------------------------------
# 背景（实证）：衰减频率 = 重启频率——startup gate 绕过 21.6h 门控每次重启
# 衰减（内存改动落盘依赖后续写——崩溃丢衰减/重启后双重衰减）。
# 门控放 decay_structural_edges（三链唯一公共入口：启动 gate/24h 后台
# RegionSync._run_decay / consolidate brain_region_api 直调）——距上次衰减
# < 21.6h（86400×0.9）→ 跳过衰减（caller 继续 refresh activation manager）。
# decay_at 独立字段（epoch 秒——不复用 last_sync——防污染同步语义），衰减
# 实际执行后立即自包含写回 ~/.niu/last_region_sync.json（合并保留现有字段
# ——含 region_sync 的 last_sync/stats）——consolidate 链不经过
# region_sync._save_status 也能记录（防门控失效双衰减复现——A5-P3）。
DECAY_GATE_INTERVAL = 86400 * 0.9  # 21.6h——稳态 24h 后台节奏 > 门控——正常节奏保持
REGION_SYNC_STATUS_FILE = "last_region_sync.json"  # 与 region_sync 共享的状态文件


def _region_sync_status_path() -> str:
    """~/.niu/last_region_sync.json——衰减门控与 RegionSync 共享状态文件路径。

    独立函数便于测试 patch（不污染真实用户文件）。
    """
    return os.path.join(os.path.expanduser("~"), ".niu", REGION_SYNC_STATUS_FILE)


def _load_region_sync_status() -> dict:
    """读 last_region_sync.json——文件缺失/损坏返回 {}（首次衰减语义）。

    只读——与 region_sync.RegionSync._load_status 等价但自包含
    （不依赖 RegionSync 实例——consolidate 链无实例）。
    """
    try:
        with open(_region_sync_status_path(), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _merge_save_region_sync_status(patch_fields: dict) -> None:
    """合并读改写 last_region_sync.json——保留现有字段——只更新 patch_fields。

    自包含（不依赖 region_sync._save_status）：consolidate 链
    （brain_region_api 直调 decay_structural_edges——不经过 region_sync）
    衰减后也能记录 decay_at——防"consolidate 衰减不记录 → 门控失效 →
    重启双重衰减"复现（A5-P3 要求）。
    """
    try:
        status = _load_region_sync_status()
        status.update(patch_fields)
        path = _region_sync_status_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
    except Exception as e:
        # 写失败只降级（本次衰减照常返回）——但下次门控可能不生效——warning 可发现
        logger.warning("[Decay] 写衰减状态失败（decay_at 未记录——下次门控可能不生效）: %s", e)


def _should_gate_decay() -> bool:
    """R14 衰减门控判定：距上次衰减 < 21.6h → True（跳过本次衰减）。

    - 无 status file / 无 decay_at 字段 → False（首次衰减——跑）
    - 文件损坏/格式异常 → False（宁可多衰减不可漏衰减）
    """
    try:
        status = _load_region_sync_status()
        decay_at = status.get("decay_at")
        if decay_at is None:
            return False
        elapsed = time.time() - float(decay_at)
        return elapsed < DECAY_GATE_INTERVAL
    except Exception:
        return False


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

        # Pre-fetch existing region labels, names, and descriptions for LLM dedup + skip logic + priority preservation
        existing_region_names: set[str] = set()
        existing_labels: list[str] = []
        existing_region_descriptions: dict[str, str] = {}
        # R11 读清洗断裂修复（P2 拍板）：get_all_regions().description 是格式化摘要
        # （展示契约——不含 brain_meta_*）→ parse_priority_from_description 恒 medium
        # → is_existing 脑区 priority 被重写为 medium。图快照直读原始描述恢复
        # priority 保真（is_existing 分支不被判 medium 重写）。
        region_raw_desc_map: dict[str, str] = {}
        try:
            rag = self._adapter._get_rag()
            if rag is not None:
                kg = rag.chunk_entity_relation_graph
                region_raw_desc_map = _read_region_raw_descriptions(kg)
        except Exception:
            pass
        try:
            for region in self.get_all_regions():
                existing_region_names.add(region.name.lower())
                existing_region_descriptions[region.name.lower()] = (
                    region_raw_desc_map.get(region.name.lower(), "") or (region.description or "")
                )
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

        # Pass 2: Generate all labels + descriptions (batch for 3+, individual for fewer)
        entity_summaries_list = [es for _, _, es in valid_communities]
        label_desc_pairs = self._generate_labels(entity_summaries_list, existing_labels)

        # Pass 3: Build entities, relationships, chunks using generated labels
        for (partition, members, entity_summaries), (region_label, region_llm_desc) in zip(valid_communities, label_desc_pairs, strict=False):
            # Use LLM description if available, otherwise fall back to entity name concatenation
            region_summary = region_llm_desc if region_llm_desc else self._generate_region_summary(entity_summaries)
            representative = members[0].replace("<SEP>", "-").replace("|", "-") if members else ""
            community_id = f"community_{partition.region_id}"
            now = time.time()
            region_name = f"{region_label}{REGION_SUFFIX}"
            is_existing = region_name.lower() in existing_region_names

            # R15b 社区劫持防御（P14 拍板）：社区标签（Leiden）撞默认脑区名
            # ——整分支跳过（描述不覆写/stale 不清理/成员边不注入）——默认脑区
            # 成员归属由 LLM 驱动建边为设计路径——防社区标签覆盖配置数据。
            # 守卫必须在 is_existing 分支顶部之前：下方 'Always upsert entity'
            # 无条件执行——放分支顶部则描述仍被覆写。
            if is_default_region(region_name):
                logger.debug("跳过默认脑区覆盖（社区标签撞名）: %s", region_name)
                continue

            # Preserve priority for existing regions, use DEFAULT_PRIORITY for new ones
            if is_existing:
                old_desc = existing_region_descriptions.get(region_name.lower(), "")
                priority = parse_priority_from_description(old_desc)
            else:
                priority = DEFAULT_PRIORITY

            description = _encode_description(
                summary=region_summary,
                region_id=community_id,
                size=len(members),
                representative=representative,
                updated_at=now,
                priority=priority,
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
        """Refresh metadata for specified regions (after membership changes)

        P4（description 仅建区生效——已建脑区描述不改）：summary 保留旧值，
        不重新从成员名生成覆盖。仅刷新 size（当前成员数）/updated_at，
        region_id/priority/shrink_count/extra_meta 从图快照原始描述保真透传
        （R11 读清洗断裂修复——list_entities 清洗会剥掉 brain_meta_*）。

        For each region:
        1. Get current members via get_region_members()
        2. Read raw description via graph snapshot (helper)
        3. Update master node via inject_entity (overwrite)

        Args:
            region_names: List of region entity names to update
        """
        all_entities: list[dict] = []

        # R11 读清洗断裂修复（P2 拍板）：list_entities 经 _clean_description 清洗
        # 会剥掉 brain_meta_* → region_id/priority/shrink_count 解析失败回默认。
        # 图快照直读原始描述恢复元数据保真（region_id/priority/shrink_count/extra_meta）。
        region_desc_map: dict[str, str] = {}
        try:
            rag = self._adapter._get_rag()
            if rag is not None:
                kg = rag.chunk_entity_relation_graph
                region_desc_map = _read_region_raw_descriptions(kg)
        except Exception:
            pass

        for region_name in region_names:
            if is_default_region(region_name):
                logger.debug("跳过默认脑区摘要更新: %s", region_name)
                continue

            # Step 1: Get current members
            members = self.get_region_members(region_name)

            if not members:
                logger.debug("脑区 %s 无成员，跳过摘要更新", region_name)
                continue

            # Step 2: Get current region description from raw desc map
            current_desc = region_desc_map.get(region_name.lower(), "")

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

            # P4：已建脑区描述不改——保留旧摘要（不重新从成员名生成覆盖）
            summary = parsed.get("summary", "")

            # Preserve dynamic metadata keys (e.g. shrink_count) that
            # _encode_description does not include in its standard 5 fields
            standard_keys = {"summary", "region_id", "size", "representative", "updated_at", "priority"}
            extra_meta = {
                k: v for k, v in parsed.items()
                if k not in standard_keys and v
            }

            now = time.time()
            priority = parse_priority_from_description(current_desc)
            description = _encode_description(
                summary=summary,
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

        # R11 读清洗断裂修复（P2 拍板）：list_entities 经 _clean_description 清洗
        # 会剥掉 brain_meta_* → region_id/size/representative/updated_at 解析失败
        # 回默认（size 恒 0）。图快照直读原始描述恢复元数据保真；
        # description 仍走展示契约（_format_summary_for_display 格式化摘要——
        # brain_meta_* 不泄漏到 /regions API 与前端面板）。
        raw_desc_map: dict[str, str] = {}
        try:
            rag = self._adapter._get_rag()
            if rag is not None:
                kg = rag.chunk_entity_relation_graph
                raw_desc_map = _read_region_raw_descriptions(kg)
        except Exception:
            pass

        regions: list[BrainRegionInfo] = []

        for entity in data:
            entity_name = entity.get("id", entity.get("entity_name", ""))
            description = entity.get("description", "")
            raw_desc = raw_desc_map.get(entity_name.lower(), "") or description

            parsed = _parse_description(raw_desc)

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

    def _refresh_activation_cache_after_delete(self, region_name: str) -> None:
        """Bug 1: 删除脑区后同步刷新 RegionActivationManager 缓存

        删除路径（cleanup_stale_regions / dissolve_shrunk_regions）只删图节点，
        不刷新 activation_mgr._regions 内存字典（24h 才全量刷新）。
        导致 LLM 立即查 brain_region_status 仍看到已删脑区，误以为没删成功，
        再删返回 not_found treated as ok，仍以为成功——死循环。

        通过懒 import agent.brain_tools.get_activation_mgr 拿到全局 mgr，
        调用 remove_region(region_name) 同步清理缓存。None 时跳过（守卫）。
        """
        try:
            from agent.brain_tools import get_activation_mgr
            mgr = get_activation_mgr()
            if mgr is None:
                return
            mgr.remove_region(region_name)
        except Exception as e:
            logger.warning(
                "刷新 activation_mgr 缓存失败 (region=%s): %s",
                region_name, e,
            )

    def get_region_members(self, region_name: str) -> list[str]:
        """Get members by reading 包含 edges from NetworkX graph.

        Delegates to lightrag_manager.get_region_members() which directly
        reads the in-memory graph — more reliable than explore_node.
        """
        from niu_api.internal.lightrag_manager import (
            get_region_members as lightrag_get_region_members,
        )
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
                # best_jaccard == 0：脑区成员跟所有社区都无交集
                # 三种情况：
                # 1. region.name 不在 region_member_map 里 → get_all_region_members 读取失败漏掉
                #    该脑区，跳过避免误删（保守）
                # 2. region.name 在 map 里且 current_members 为空 → 脑区真的没成员了，判 stale 删除
                # 3. region.name 在 map 里且 current_members 非空 → Task 1 排除已归属实体导致的
                #    天然无交集（脑区成员是已归属，社区里是游离），不删除不漂移
                #    过时脑区清理交给 dissolve_shrunk_regions（基于成员数持续 < 100）
                if region.name not in region_member_map:
                    # 读取失败漏掉该脑区，不判 stale 避免误删
                    logger.warning(
                        "脑区 %s 不在 get_all_region_members 返回结果中（读取失败？），跳过 stale 判定避免误删",
                        region.name,
                    )
                elif not current_members:
                    # 脑区在 map 里且成员确实为空，判 stale 删除
                    if not dry_run:
                        delete_result = self._adapter.delete_entity(region.name)
                        if isinstance(delete_result, dict) and delete_result.get("status") == "ok":
                            removed.append(region.name)
                            logger.info(
                                "删除空成员脑区: %s (无成员，Jaccard=0)",
                                region.name,
                            )
                            # Bug 1: 同步刷新 activation_mgr 缓存，避免 LLM 立即查
                            # brain_region_status 仍看到已删脑区（死循环）
                            self._refresh_activation_cache_after_delete(region.name)
                        else:
                            logger.warning(
                                "删除空成员脑区失败: %s — %s",
                                region.name,
                                delete_result.get("message", "unknown") if isinstance(delete_result, dict) else "error",
                            )
                    else:
                        logger.info(
                            "[dry_run] 将删除空成员脑区: %s (无成员，Jaccard=0)",
                            region.name,
                        )
                else:
                    # 脑区有成员但跟社区无交集（Task 1 排除导致），跳过
                    logger.debug(
                        "脑区 %s 有 %d 成员但跟当前社区无交集（Task 1 排除已归属实体），跳过 stale 判定",
                        region.name, len(current_members),
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

        # R11 读清洗断裂修复（P2 拍板）：explore_node 经 _clean_description 清洗
        # 会剥掉 brain_meta_* → priority 解析失败回 medium（漂移更新把高优脑区
        # 重写为 medium）。图快照直读原始描述恢复 priority 保真。
        region_raw_desc_map: dict[str, str] = {}
        try:
            rag = self._adapter._get_rag()
            if rag is not None:
                kg = rag.chunk_entity_relation_graph
                region_raw_desc_map = _read_region_raw_descriptions(kg)
        except Exception:
            pass

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
            # 稳定排序（new_members 是 set——list() 顺序不确定）
            representative = sorted(new_members)[0].replace("<SEP>", "-").replace("|", "-")
            # Preserve priority from existing region raw description
            old_desc = region_raw_desc_map.get(region_name.lower(), "")
            priority = parse_priority_from_description(old_desc)
            # Preserve dynamic metadata keys (e.g. shrink_count) that
            # _encode_description does not include in its standard 5 fields
            extra_meta: dict[str, str] = {}
            if old_desc:
                parsed = _parse_description(old_desc)
                standard_keys = {"summary", "region_id", "size", "representative", "updated_at", "priority"}
                extra_meta = {
                    k: v for k, v in parsed.items()
                    if k not in standard_keys and v
                }
            now = time.time()
            description = _encode_description(
                summary=summary, region_id=best_cid,
                size=len(new_members), representative=representative,
                updated_at=now,
                priority=priority,
            )
            for key, value in extra_meta.items():
                description += f"<SEP>brain_meta_{key}:{value}"
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
        shrink_threshold: int = 100,  # 成员数 < 100 才判萎缩（用户要求；4f03f10d 曾越权改成 10，已恢复）
        shrink_rounds: int = 3,
    ) -> list[str]:
        """Dissolve regions that have been shrinking for multiple sync cycles.

        A region is "shrunk" when its member count < shrink_threshold.
        After shrink_rounds consecutive sync cycles of being shrunk,
        the region is dissolved: members are reassigned to the most
        similar neighbor region, and the region node is deleted.

        **孤岛保护**（本次新增）：dissolve 执行前会检查所有成员的 total_degree，
        有任何一个成员 degree <= 1（删脑区会变孤岛）就取消本次 dissolve，
        shrink_count 继续按规则累加（current_size < threshold 就 +1），下轮重新扫。
        详见 `_has_isolated_member`。

        Shrink tracking is stored in the region description field
        as ``brain_meta_shrink_count:N``.

        Args:
            shrink_threshold: Minimum members before region is "shrunk" (default 100)
                用户明确要求 100（4f03f10d 曾越权改成 10 已恢复）。
            shrink_rounds: Consecutive shrunk cycles before dissolution (default 3)

        Returns:
            List of dissolved region entity names.
        """
        existing_regions = self.get_all_regions()
        dissolved: list[str] = []
        dissolved_names: set[str] = set()  # Track dissolved names for stale snapshot filtering

        # R11 读清洗断裂修复（P2 拍板）：list_entities 经 _clean_description 清洗
        # 会剥掉 brain_meta_shrink_count → shrink_count 恒 0 → dissolve 永不触发。
        # 图快照直读原始描述恢复 shrink_count 3 轮累计（dissolve 机制本体不动——
        # 阈值 100 + 3 轮 + is_default_region + _has_isolated_member 孤岛保护）。
        #
        # 幽灵死锁披露（B-P2）：R1（阈值 100）+ R11（shrink_count 读回真实值）后——
        # 现网幽灵『知识库脑区』（20 成员 <100 配置外）shrink_count 将开始累计——
        # 但其 3 个 degree-1 孤立成员触发孤岛保护 _has_isolated_member → dissolve
        # 永久取消——幽灵永不溶解（数据保留——可接受——与 P7 一致）——不执行一次性
        # 清理（数据不主动改）。
        region_raw_desc_map: dict[str, str] = {}
        try:
            rag = self._adapter._get_rag()
            if rag is not None:
                kg = rag.chunk_entity_relation_graph
                region_raw_desc_map = _read_region_raw_descriptions(kg)
        except Exception:
            pass

        # 批量读取所有脑区成员（避免循环内调单数 get_region_members 触发读取失败返回空）
        # 单数版本与复数版本读取逻辑一致，都有 try/except 返回空——
        # 循环内调单数会因锁竞争/读取失败返回空，导致 current_size=0 误判萎缩，
        # 累积 shrink_count 后误删有成员的脑区。批量读一次拿全图快照更可靠。
        from niu_api.internal.lightrag_manager import get_all_region_members
        region_member_map: dict[str, list[str]] = get_all_region_members()

        for region in existing_regions:
            # Protect default regions (defined in preferences.json)
            if is_default_region(region.name):
                continue

            members = region_member_map.get(region.name, [])
            current_size = len(members)

            # Parse shrink count from RAW KG description (not stripped summary)
            # helper 返回键为小写节点键（LightRAG 图节点 id 全部小写）
            raw_desc = region_raw_desc_map.get(region.name.lower(), "")
            # Fallback: try explore_node if graph snapshot didn't return this region
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

            # Check dissolution threshold
            # 注意：不能用 if/elif 结构——孤岛保护取消 dissolve 时仍需走持久化分支
            # Python 语义下 elif 挂在外层 if 上，进入外层 if 分支后不会 fall-through 到 elif
            # 所以用独立 if + continue 模式
            should_dissolve = shrink_count >= shrink_rounds and not self._has_isolated_member(members)
            should_skip_persist = False  # dissolve 成功后跳过持久化

            if shrink_count >= shrink_rounds and not should_dissolve:
                # 孤岛保护：shrink_count 达标但有成员 total_degree<=1（删脑区会变孤岛）
                # 取消本次 dissolve，shrink_count 继续按规则累加（已经在 L1082-1085 +1 过了），
                # 下轮重新扫。走下面的持久化分支写 shrink_count（累加后值）
                logger.info(
                    "脑区 %s 已萎缩 %d 轮，但有成员 total_degree<=1（删脑区会变孤岛），"
                    "取消本次 dissolve，shrink_count 持久化为 %d 等下轮重新扫",
                    region.name, shrink_count, shrink_count,
                )
                # 不设 should_skip_persist=True，让下面的持久化分支执行

            if should_dissolve:
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
                    # Bug 1: 同步刷新 activation_mgr 缓存，避免 LLM 立即查
                    # brain_region_status 仍看到已删脑区（死循环）
                    self._refresh_activation_cache_after_delete(region.name)

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
                    # dissolve 成功后跳过下面的持久化（已 dissolve 不需要写 shrink_count）
                    should_skip_persist = True
                else:
                    logger.warning("解散脑区失败: %s", region.name)
                    # dissolve 失败时脑区还在，不设 should_skip_persist，
                    # 让下面持久化分支写 shrink_count（持续累加反映失败次数）

            if not should_skip_persist and (shrink_count > 0 or parsed.get("shrink_count", "0") != "0"):
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
                standard_keys = {"summary", "region_id", "size", "representative", "updated_at", "shrink_count", "priority"}
                for key, value in parsed.items():
                    if key not in standard_keys and value:
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

    def _has_isolated_member(self, members: list[str]) -> bool:
        """检查成员列表里是否有任何一个成员 total_degree <= 1（删脑区会变孤岛）。

        用于 dissolve_shrunk_regions 执行前的安全检查：
        - 所有成员 degree >= 2 → 返回 False（安全，可解散）
        - 有任何一个成员 degree <= 1 → 返回 True（会变孤岛，阻止解散）
        - 成员不在图里 / RAG 拿不到 → 返回 True（保守，阻止解散）
        - 空成员列表 → 返回 False（脑区 0 成员，无孤岛风险）

        成员名小写查找：get_all_region_members 返回的成员名直接来自 nx_graph
        边数据（lightrag_manager.py L433-445），而 LightRAG graph 节点 id 全部
        小写（lightrag_manager.py L385 注释）。现有代码 region_manager.py L614-615
        也是 member.lower() 直接小写查找。本函数跟现有模式一致，直接小写。

        Args:
            members: 成员实体名列表（来自 get_all_region_members）

        Returns:
            True 表示有孤岛风险，应取消 dissolve；False 表示安全可解散
        """
        if not members:
            return False

        # 方法内 import（跟 region_manager.py L604 模式一致，避免循环 import）
        from niu_api.internal.lightrag_manager import graph_read_lock

        try:
            rag = self._adapter._get_rag()
            if rag is None:
                return True  # RAG 拿不到，保守阻止 dissolve

            kg = rag.chunk_entity_relation_graph
            nx_graph = kg._graph if hasattr(kg, "_graph") else kg
            if nx_graph is None:
                return True  # 图拿不到，保守阻止 dissolve

            with graph_read_lock():
                for member in members:
                    if not isinstance(member, str):
                        continue  # 防御性：跳过非字符串成员
                    # 直接小写查找（跟现有代码 region_manager.py L614-615 模式一致）
                    node_id = member.lower()
                    if node_id not in nx_graph:
                        # 成员不在图里（数据不一致），保守阻止 dissolve
                        return True
                    degree = nx_graph.degree(node_id)
                    if degree <= 1:
                        return True  # 找到孤岛风险成员

            return False  # 所有成员 degree >= 2

        except Exception as e:
            logger.warning("_has_isolated_member 检查失败，保守阻止 dissolve: %s", e)
            return True

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
    ) -> tuple[str, str]:
        """Generate a semantic Chinese label and description for a brain region via LLM.

        Falls back to heuristic (entity_names[0]) on any LLM failure.
        Returns (label, description) tuple.
        """
        if not entity_summaries:
            return ("unknown", "")

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
            return ("unknown", "")

        fallback_label = entity_names[0].replace("<SEP>", "-").replace("|", "-")

        # Build prompt
        entity_list_str = ", ".join(entity_list_parts)
        existing_str = ", ".join(existing_regions) if existing_regions else "无"

        prompt = (
            "你是一个知识图谱分析师。根据以下社区内的实体列表，为这个社区生成一个简洁的中文标签名和一句话描述。\n\n"
            "要求：\n"
            "- 标签8个字以下\n"
            "- 描述20个字以内，概括这些实体的共同主题或用途\n"
            "- 不要跟现有脑区重名或语义接近\n"
            "- 只能返回JSON格式：{\"label\": \"标签名\", \"description\": \"一句话描述\"}\n"
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
                        "你是一个知识图谱分析师。根据以下社区内的实体列表，为这个社区生成一个简洁的中文标签名和一句话描述。\n\n"
                        "要求：\n"
                        "- 标签8个字以下\n"
                        "- 描述20个字以内，概括这些实体的共同主题或用途\n"
                        "- 不要跟现有脑区重名或语义接近\n"
                        "- 只能返回JSON格式：{\"label\": \"标签名\", \"description\": \"一句话描述\"}\n"
                        "- 返回其他任何格式或内容将判定失败\n\n"
                        f"现有脑区：{existing_str}\n\n"
                        f"实体列表：{entity_list_str}"
                    )
                    token_count = calc.count_text(prompt)
        except Exception:
            pass  # Token counting failure should not block

        # Call LLM with retry
        label, llm_description = self._parse_label_from_llm(prompt, fallback_label)

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

        return label, llm_description[:20] if len(llm_description) > 20 else llm_description

    def _parse_label_from_llm(self, prompt: str, fallback: str) -> tuple[str, str]:
        """Call LLM and parse label + description with retry logic."""
        for attempt in range(2):
            try:
                content = self._call_llm_for_label(prompt)
                label, description = self._extract_label_from_content(content)
                if label:
                    if len(label) > 8:
                        label = label[:8]
                    return label, description
            except Exception as e:
                logger.debug("LLM label generation attempt %d failed: %s", attempt + 1, e)

        logger.warning("LLM label generation failed after retry, fallback to: %s", fallback)
        return fallback, ""

    def _extract_label_from_content(self, content: str) -> tuple[str, str]:
        """Extract label and description from LLM response content."""
        content = content.strip()

        # Try JSON parse
        try:
            data = json.loads(content)
            if isinstance(data, dict) and "label" in data:
                label = str(data["label"]).strip()
                description = str(data.get("description", "")).strip()
                if label:
                    return label, description
        except (json.JSONDecodeError, ValueError):
            pass

        # Try regex extraction
        match = re.search(r'"label"\s*:\s*"([^"]+)"', content)
        if match:
            label = match.group(1).strip()
            desc_match = re.search(r'"description"\s*:\s*"([^"]+)"', content)
            description = desc_match.group(1).strip() if desc_match else ""
            if label:
                return label, description

        return "", ""

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

            result_holder: list = [None, None]  # [mock_response, exception]

            def _consume():
                try:
                    while True:
                        chunk = next(gen)
                        if isinstance(chunk, str):
                            chunks.append(chunk)
                except StopIteration as e:
                    result_holder[0] = e.value
                except Exception as e:
                    result_holder[1] = e

            thread = threading.Thread(target=_consume, daemon=True)
            thread.start()
            thread.join(timeout=30)

            if thread.is_alive():
                logger.warning("LLM label generation timed out after 30s, using partial result")
            else:
                mock_resp = result_holder[0]
                if mock_resp and getattr(mock_resp, 'stream_error', False):
                    logger.warning(f"region labeling LLM error: {mock_resp.error_msg}")
                    return ""
                if result_holder[1]:
                    raise result_holder[1]

        except Exception as e:
            if not chunks:
                raise
            logger.warning("LLM label generation error: %s, using partial result", e)

        # 优先使用 MockResponse.content（正常完成路径），fallback 到 chunks（timeout/exception 路径）
        try:
            mock_resp = result_holder[0]
        except (NameError, IndexError):
            mock_resp = None
        if mock_resp and hasattr(mock_resp, 'content') and mock_resp.content:
            return mock_resp.content
        return "".join(chunks)

    def _generate_labels(
        self,
        entity_summaries_list: list[list[str]],
        existing_regions: list[str],
    ) -> list[tuple[str, str]]:
        """Generate labels and descriptions for multiple regions.

        Uses batch LLM call for 3+ regions, individual for fewer.
        Returns list of (label, description) tuples.
        """
        if len(entity_summaries_list) >= 3:
            try:
                batch_result = self._generate_region_labels_batch(
                    entity_summaries_list, existing_regions
                )
                labels = []
                missing_indices = []
                for i in range(len(entity_summaries_list)):
                    if i in batch_result:
                        labels.append(batch_result[i])
                    else:
                        labels.append(None)
                        missing_indices.append(i)

                extended_existing = list(existing_regions) + [labels[j][0] for j in range(len(labels)) if labels[j] is not None and j not in missing_indices]
                for i in missing_indices:
                    try:
                        label, desc = self._generate_region_label(
                            entity_summaries_list[i], extended_existing
                        )
                        labels[i] = (label, desc)
                        extended_existing.append(label)
                    except Exception:
                        fallback = entity_summaries_list[i][0].split("(")[0] if entity_summaries_list[i] else "unknown"
                        labels[i] = (fallback, "")
                        extended_existing.append(fallback)

                seen_labels = set(existing_regions)
                for i, item in enumerate(labels):
                    if item is not None and item[0] in seen_labels:
                        base = item[0][:7]
                        n = 2
                        candidate = f"{base}{n}"
                        while candidate in seen_labels and n < 10:
                            n += 1
                            candidate = f"{base}{n}"
                        labels[i] = (candidate, item[1])
                    if item is not None:
                        seen_labels.add(labels[i][0])

                for i, item in enumerate(labels):
                    if item is not None and len(item[0]) > 8:
                        labels[i] = (item[0][:8], item[1])

                return labels
            except Exception as e:
                logger.warning("Batch label generation failed: %s, falling back to individual", e)

        labels = []
        for entity_summaries in entity_summaries_list:
            label, desc = self._generate_region_label(entity_summaries, existing_regions)
            labels.append((label, desc))
            existing_regions = existing_regions + [label]

        return labels

    def _generate_region_labels_batch(
        self,
        entity_summaries_list: list[list[str]],
        existing_regions: list[str],
    ) -> dict[int, tuple[str, str]]:
        """Generate labels and descriptions for all regions in a single LLM call.

        Returns dict of {index: (label, description)} for successfully parsed regions.
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
            "你是一个知识图谱分析师。根据以下社区内的实体列表，为每个社区生成一个简洁的中文标签名和一句话描述。\n\n"
            "要求：\n"
            "- 每个标签8个字以下\n"
            "- 每个描述20个字以内，概括该社区实体的共同主题或用途\n"
            "- 不要跟现有脑区重名或语义接近\n"
            "- 只能返回JSON格式：{\"regions\": [{\"id\": 0, \"label\": \"标签1\", \"description\": \"描述1\"}, ...]}\n"
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
                        "你是一个知识图谱分析师。根据以下社区内的实体列表，为每个社区生成一个简洁的中文标签名和一句话描述。\n\n"
                        "要求：\n"
                        "- 每个标签8个字以下\n"
                        "- 每个描述20个字以内，概括该社区实体的共同主题或用途\n"
                        "- 不要跟现有脑区重名或语义接近\n"
                        "- 只能返回JSON格式：{\"regions\": [{\"id\": 0, \"label\": \"标签1\", \"description\": \"描述1\"}, ...]}\n"
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
                    description = str(item.get("description", "")).strip()
                    if idx is not None and label and len(label) <= 8:
                        result[int(idx)] = (label, description)
                return result
        except (json.JSONDecodeError, ValueError):
            pass

        # Try regex fallback for batch — flexible two-step approach
        result = {}
        for obj_match in re.finditer(r'\{[^}]+\}', content):
            obj_str = obj_match.group(0)
            id_match = re.search(r'"id"\s*:\s*(\d+)', obj_str)
            label_match = re.search(r'"label"\s*:\s*"([^"]+)"', obj_str)
            if id_match and label_match:
                idx = int(id_match.group(1))
                label = label_match.group(1).strip()
                if label and len(label) <= 8:
                    desc_match = re.search(r'"description"\s*:\s*"([^"]+)"', obj_str)
                    description = desc_match.group(1).strip() if desc_match else ""
                    result[idx] = (label, description)

        return result


    # ------------------------------------------------------------------
    # Edge decay
    # ------------------------------------------------------------------

    def decay_structural_edges(self) -> dict:
        """Decay brain region edges — half-life model with floor protection.

        Only decays entity→brainregion attribution edges.
        Knowledge edges (entity→entity) are not affected.
        Anchor edges (brainregion→brainregion) are skipped.

        R14 衰减门控（P13 用户拍板——算法调度改动）：三链唯一公共入口
        （启动 gate/24h 后台 RegionSync._run_decay / consolidate
        brain_region_api 直调）——距上次衰减 < 21.6h（86400×0.9）→
        跳过衰减（返回 gated 结果——caller 继续 refresh activation
        manager）。decay_at 独立字段（epoch 秒——不复用 last_sync——
        防污染同步语义）——衰减实际执行后立即自包含写回
        ~/.niu/last_region_sync.json（合并保留现有字段——含 region_sync
        的 last_sync/stats——consolidate 链不经过 _save_status 也能记录）。

        行为变化标注（B4-P3）：
        ① 门控只挡重启额外衰减——正常 24h 后台衰减节奏保持
           （稳态 24h > 21.6h——每次同步正常衰减）
        ② consolidate（手动触发）链同样被门控——距上次衰减 <21.6h 时
           手动 consolidate 不再衰减（功能行为变化）
        ③ 冷启动后 _sync_loop 首轮同步可能边际跳过（距上次 <21.6h）——
           稳态节奏保持——首个周期可能边际跳过

        无 status file / 无 decay_at 字段 → 跑衰减（首次）。
        """
        if _should_gate_decay():
            logger.info("[Decay] 距上次衰减 < 21.6h——门控跳过（R14——重启额外衰减被挡）")
            return {"decayed": 0, "deleted": 0, "protected": 0, "skipped_anchor": 0, "gated": True}

        executed = False
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
            executed = True

        except Exception as e:
            logger.warning("Edge decay failed: %s", e)
            result = {"decayed": 0, "deleted": 0, "protected": 0, "skipped_anchor": 0}

        # R14：衰减实际执行后立即记录 decay_at（跳过衰减/早退/异常不写——
        # 保持上次值——防漏衰减）。合并保留现有字段（含 region_sync 的
        # last_sync/stats）——consolidate 链不经过 _save_status 也能记录。
        if executed:
            _merge_save_region_sync_status({"decay_at": time.time()})

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
        with open(prefs_path, encoding="utf-8") as f:
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
        # 缺键防御：配置缺 label（或非字符串）时跳过，避免 KeyError 中断
        # consolidate 链路（R15b 新调用面——R14 时 update_default_region_sizes
        # 同类直接索引；此处只防 is_default_region 本函数）
        label = d.get("label")
        if not isinstance(label, str):
            continue
        if region_name == f"{label}{REGION_SUFFIX}":
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


def update_default_region_sizes(adapter) -> dict:
    """用当前实际成员数刷新 7 个默认脑区的 brain_meta_size 元数据。

    原全量分配函数的 size 更新职责提取（D-15 防膨胀口径：size = 实际成员数，
    不累加）。归属建边由 LLM 知识图谱操作完成，删除全量分配后保留此轻量更新，
    使脑区状态图/面板的成员计数保持准确。只更新 size/updated_at 字段；
    summary/region_id/representative 从旧 description 透传，priority 从配置读取
    （配置权威——配置写什么用什么；缺失回退 DEFAULT_PRIORITY）。

    Args:
        adapter: LightRAGAdapter instance.

    Returns:
        Dict with updated count: {"updated": n}.
    """
    rag = adapter._get_rag()
    if rag is None:
        return {"updated": 0}

    kg = rag.chunk_entity_relation_graph
    if kg is None:
        return {"updated": 0}

    if kg._graph is None:
        return {"updated": 0}

    from niu_api.internal.lightrag_manager import (
        get_all_region_members,
        get_brain_regions,
        graph_read_lock,
    )

    # 默认脑区列表（is_default_region 按配置 label + REGION_SUFFIX 过滤）
    default_regions = [r for r in get_brain_regions() if is_default_region(r)]
    if not default_regions:
        return {"updated": 0}

    # 图快照直读原始 description（不经 list_entities/_clean_description 清洗——
    # 清洗会剥掉 brain_meta_* 字段导致 priority/region_id/representative 被抹平）。
    # 描述缺失脑区跳过更新（防空描述覆盖清空 description + priority 掉 medium）。
    with graph_read_lock():
        snapshot = kg._graph.copy()
    desc_map: dict[str, str] = {}
    for region_name in default_regions:
        node_data = snapshot.nodes.get(region_name.lower())
        if node_data and node_data.get("description"):
            desc_map[region_name] = node_data["description"]

    # 成员数：一次图快照返回全部脑区成员（缺 key 脑区 = 无成员 → size=0）；
    # 整体返回空（读失败/图异常）→ 跳过更新
    members_map = get_all_region_members()
    if not members_map:
        return {"updated": 0}

    # 配置读取（与 assign 原关键词构建同源——label 无后缀，匹配脑区名）
    config_map: dict[str, dict] = {}
    for region_def in get_default_regions_config():
        config_map[f"{region_def['label']}{REGION_SUFFIX}"] = region_def

    update_entities: list[dict] = []
    for region_name, desc in desc_map.items():
        parsed = _parse_description(desc)
        # priority 从配置读取（配置权威——配置写什么用什么；缺失回退 DEFAULT_PRIORITY）
        priority = config_map.get(region_name, {}).get("priority", DEFAULT_PRIORITY)
        updated_desc = _encode_description(
            summary=parsed.get("summary", ""),
            region_id=parsed.get("region_id", ""),
            size=len(members_map.get(region_name, [])),
            representative=parsed.get("representative", ""),
            priority=priority,
            updated_at=time.time(),
        )
        update_entities.append({
            "entity_name": region_name,
            "entity_type": REGION_ENTITY_TYPE,
            "description": updated_desc,
        })

    # 只更新元数据——relationships/chunks 硬性空（绝不建边）；
    # inject 异常不吞——向上传播至调用方记录（防虚报 Updated N 成功日志）
    if update_entities:
        from niu_api.internal.lightrag_adapter import LightRAGIngester
        ingester = LightRAGIngester()
        ingester.inject_custom_kg(
            entities=update_entities,
            relationships=[],
            chunks=[],
            source_id=REGION_SOURCE_ID,
        )
    return {"updated": len(update_entities)}
