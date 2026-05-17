"""
社区检测引擎 — 基于 Leiden 算法检测知识图谱中的脑区（社区）

将 LightRAG 知识图谱中的实体和关系转换为 igraph 图，
使用 Leiden 算法检测社区结构，每个社区对应一个"脑区"。

M1 模块：社区检测，LLM 命名在 M2 中实现。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# 尝试导入 leidenalg 和 igraph，缺失时优雅降级
try:
    import igraph
    import leidenalg

    _HAS_LEIDEN = True
except ImportError:
    _HAS_LEIDEN = False
    logger.warning(
        "leidenalg 或 python-igraph 未安装，社区检测将不可用。"
        "请运行: pip install python-igraph leidenalg"
    )


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class RegionPartition:
    """一个检测到的社区/脑区"""

    region_id: int  # Leiden 社区 ID
    region_name: str  # 人类可读名称（当前为占位符，M2 中由 LLM 生成）
    entity_names: list[str]  # 该社区内的实体名称列表
    entity_types: dict[str, int]  # entity_type → 数量
    edge_count: int  # 社区内部边数
    modularity_score: float  # 局部模块度贡献
    entity_name_to_type: dict[str, str] | None = None  # entity_name → entity_type mapping


@dataclass
class CommunityDetectionResult:
    """社区检测的完整结果"""

    partitions: list[RegionPartition]
    total_nodes: int
    total_edges: int
    total_regions: int
    modularity: float  # 全局模块度分数
    timestamp: str  # ISO 格式时间戳


# ---------------------------------------------------------------------------
# 空结果工厂
# ---------------------------------------------------------------------------

def _empty_result() -> CommunityDetectionResult:
    """图节点不足时返回空结果"""
    return CommunityDetectionResult(
        partitions=[],
        total_nodes=0,
        total_edges=0,
        total_regions=0,
        modularity=0.0,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# 社区检测器
# ---------------------------------------------------------------------------

class CommunityDetector:
    """基于 Leiden 算法的知识图谱社区检测器

    用法::

        detector = CommunityDetector(lightrag_adapter)
        result = detector.detect_communities(resolution=1.0)
        for partition in result.partitions:
            print(f"{partition.region_name}: {len(partition.entity_names)} entities")
"""

    def __init__(self, lightrag_adapter: Any) -> None:
        self._adapter = lightrag_adapter

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def detect_communities(
        self, resolution: float = 1.0, min_graph_size: int = 50,
        min_community_size: int = 100,  # Threshold raised from 10 to 100
    ) -> CommunityDetectionResult:
        """对 LightRAG 知识图谱运行 Leiden 社区检测

        步骤:
        1. 通过 adapter 获取图快照
        2. 检查图谱大小是否达到 min_graph_size
        3. resolution 自适应：节点 < 200 用 0.5，>= 200 用传入值
        4. 将 NetworkX 风格数据转换为 igraph Graph
        5. 运行 Leiden 算法
        6. 过滤小社区（成员 < min_community_size）
        7. 返回 CommunityDetectionResult

        Args:
            resolution: Leiden 分辨率参数，值越大社区越小、越多
                （小图谱会被自动降低为 0.5）
            min_graph_size: 图谱最小节点数，低于此值返回空结果（默认 50）
            min_community_size: 社区最小成员数，低于此值的社区被过滤（默认 100）
                Raised from 10 to reduce noise from small communities.

        Returns:
            CommunityDetectionResult 包含所有检测结果
        """
        # 1. 获取图快照
        snapshot = self._adapter.get_graph_snapshot()
        if snapshot is None:
            logger.warning("图快照为空，跳过社区检测")
            return _empty_result()

        nodes: list[dict] = snapshot.get("nodes", [])
        edges: list[dict] = snapshot.get("edges", [])

        # 2. 检查图谱大小
        if len(nodes) < min_graph_size:
            logger.info(
                "图谱节点数 %d < min_graph_size %d，跳过社区检测",
                len(nodes), min_graph_size,
            )
            return _empty_result()

        # 3. resolution 自适应：小图谱倾向更大更少的社区
        effective_resolution = resolution
        if len(nodes) < 200:
            effective_resolution = 0.5
            logger.info(
                "小图谱 (%d 节点)，resolution 从 %.1f 降至 %.1f",
                len(nodes), resolution, effective_resolution,
            )

        # 4. 检查 leidenalg 是否可用
        if not _HAS_LEIDEN:
            logger.error("leidenalg/python-igraph 未安装，无法执行社区检测")
            return _empty_result()

        # 5. 构建 igraph
        graph = self._build_igraph(nodes, edges)

        # 6. 运行 Leiden
        try:
            if effective_resolution != 1.0:
                partition = leidenalg.find_partition(
                    graph,
                    leidenalg.RBConfigurationVertexPartition,
                    resolution_parameter=effective_resolution,
                )
            else:
                partition = leidenalg.find_partition(
                    graph,
                    leidenalg.ModularityVertexPartition,
                )
        except Exception:
            logger.exception("Leiden 社区检测失败")
            return _empty_result()

        # 7. 构建分区结果 + 过滤小社区
        partitions = self._build_partitions(graph, partition, min_community_size)

        # 8. 返回完整结果
        return CommunityDetectionResult(
            partitions=partitions,
            total_nodes=graph.vcount(),
            total_edges=graph.ecount(),
            total_regions=len(partitions),
            modularity=partition.q,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _build_igraph(
        self, nodes: list[dict], edges: list[dict]
    ) -> "igraph.Graph":
        """将 LightRAG 快照转换为 igraph Graph，保留属性

        - 顶点属性: name (实体名称), entity_type
        - 边属性: relation, description, weight
        - 孤立节点仍保留在图中
        """
        # 构建节点名称 → 索引映射
        node_names: list[str] = []
        node_types: list[str] = []
        name_to_idx: dict[str, int] = {}

        for i, node in enumerate(nodes):
            name = node.get("name", node.get("id", f"node_{i}"))
            entity_type = node.get("type", node.get("entity_type", "unknown"))
            node_names.append(name)
            node_types.append(entity_type)
            name_to_idx[name] = i

        # Create undirected igraph for Leiden (directed graphs can produce
        # spurious single-node communities and Leiden works best undirected)
        g = igraph.Graph(len(nodes), directed=False)
        g.vs["name"] = node_names
        g.vs["entity_type"] = node_types

        # 添加边
        edge_srcs: list[int] = []
        edge_dsts: list[int] = []
        edge_relations: list[str] = []
        edge_descriptions: list[str] = []
        edge_weights: list[float] = []

        for edge in edges:
            src_name = edge.get("source", "")
            dst_name = edge.get("target", "")
            src_idx = name_to_idx.get(src_name)
            dst_idx = name_to_idx.get(dst_name)

            if src_idx is None or dst_idx is None:
                logger.debug(
                    "跳过边（端点不在节点列表中）: %s → %s",
                    src_name,
                    dst_name,
                )
                continue

            edge_srcs.append(src_idx)
            edge_dsts.append(dst_idx)
            edge_relations.append(edge.get("relation", ""))
            edge_descriptions.append(edge.get("description", ""))
            edge_weights.append(float(edge.get("weight", 1.0)))

        g.add_edges(zip(edge_srcs, edge_dsts))
        if g.ecount() > 0:
            g.es["relation"] = edge_relations
            g.es["description"] = edge_descriptions
            g.es["weight"] = edge_weights

        return g

    def _build_partitions(
        self, graph: "igraph.Graph", partition: Any,
        min_community_size: int = 100,
    ) -> list[RegionPartition]:
        """将 Leiden 分区结果转换为 RegionPartition 列表

        Args:
            graph: igraph Graph 对象
            partition: leidenalg 分区结果
            min_community_size: 社区最小成员数，低于此值的社区被过滤

        Returns:
            按 region_id 排序的 RegionPartition 列表
        """
        result: list[RegionPartition] = []
        filtered_count = 0

        # 计算全局模块度（用于按比例分配给各社区）
        global_modularity = partition.q if hasattr(partition, "q") else 0.0

        for community_idx, member_indices in enumerate(partition):
            if not member_indices:
                continue

            # 过滤小社区：成员 < min_community_size 的不返回
            if len(member_indices) < min_community_size:
                filtered_count += 1
                continue

            # 收集实体名称和类型
            entity_names: list[str] = []
            entity_type_counts: dict[str, int] = {}
            entity_name_to_type: dict[str, str] = {}

            for vidx in member_indices:
                v = graph.vs[vidx]
                name = v["name"] if "name" in v.attributes() else f"entity_{vidx}"
                entity_names.append(name)

                etype = v["entity_type"] if "entity_type" in v.attributes() else "unknown"
                entity_type_counts[etype] = entity_type_counts.get(etype, 0) + 1
                entity_name_to_type[name] = etype

            # 计算社区内部边数
            internal_edges = self._count_internal_edges(graph, member_indices)

            # 计算局部模块度贡献（按社区大小占比分配）
            total_nodes = graph.vcount()
            if total_nodes > 0 and global_modularity != 0.0:
                local_mod = global_modularity * (len(member_indices) / total_nodes)
            else:
                local_mod = 0.0

            result.append(
                RegionPartition(
                    region_id=community_idx,
                    region_name=f"region_{community_idx}",
                    entity_names=entity_names,
                    entity_types=entity_type_counts,
                    edge_count=internal_edges,
                    modularity_score=round(local_mod, 6),
                    entity_name_to_type=entity_name_to_type,
                )
            )

        if filtered_count > 0:
            logger.info(
                "过滤 %d 个小社区（成员 < %d）",
                filtered_count, min_community_size,
            )

        # 按 region_id 排序
        result.sort(key=lambda r: r.region_id)
        return result

    def _count_internal_edges(
        self, graph: "igraph.Graph", member_indices: list[int]
    ) -> int:
        """计算社区内部边数（两端均在社区内的边）

        Iterates over edges rather than neighbors to correctly handle
        parallel edges. In igraph, undirected graphs store each edge
        once in the edge sequence, so no division is needed.
        """
        member_set = set(member_indices)
        count = 0
        for edge in graph.es:
            if edge.source in member_set and edge.target in member_set:
                count += 1
        return count

    def _handle_small_graph(
        self, nodes: list[dict], _edges: list[dict]
    ) -> CommunityDetectionResult:
        """处理节点数 < 2 的情况：单节点为一个社区，空图返回空结果"""
        now = datetime.now(timezone.utc).isoformat()

        if not nodes:
            return _empty_result()

        # 单节点情况
        node = nodes[0]
        name = node.get("name", node.get("id", "entity_0"))
        etype = node.get("type", node.get("entity_type", "unknown"))

        return CommunityDetectionResult(
            partitions=[
                RegionPartition(
                    region_id=0,
                    region_name="region_0",
                    entity_names=[name],
                    entity_types={etype: 1},
                    edge_count=0,
                    modularity_score=0.0,
                )
            ],
            total_nodes=1,
            total_edges=0,
            total_regions=1,
            modularity=0.0,
            timestamp=now,
        )
