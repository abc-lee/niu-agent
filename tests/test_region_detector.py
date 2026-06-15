"""
Tests for niu_api/internal/region_detector.py

社区检测引擎测试 — 验证 Leiden 社区检测在 LightRAG 知识图谱上的正确性。
"""

import pytest
from unittest.mock import MagicMock, patch

from niu_api.internal.region_detector import (
    CommunityDetectionResult,
    CommunityDetector,
    RegionPartition,
    _HAS_LEIDEN,
)


# ============== 辅助函数 ==============


def _make_mock_adapter(nodes: list[dict], edges: list[dict]) -> MagicMock:
    """创建一个 mock LightRAGAdapter，其 get_graph_snapshot 返回指定数据"""
    adapter = MagicMock()
    adapter.get_graph_snapshot = MagicMock(
        return_value={"nodes": nodes, "edges": edges}
    )
    return adapter


def _make_3_community_graph() -> tuple[list[dict], list[dict]]:
    """构建包含 3 个社区的测试图

    社区 0: Python, Django, FastAPI (Web 开发)
    社区 1: React, Vue, Angular (前端框架)
    社区 2: Docker, Kubernetes, Terraform (基础设施)
    跨社区边: Python → Docker (1 条弱跨社区连接)
    """
    nodes = [
        {"id": "Python", "name": "Python", "type": "language"},
        {"id": "Django", "name": "Django", "type": "framework"},
        {"id": "FastAPI", "name": "FastAPI", "type": "framework"},
        {"id": "React", "name": "React", "type": "framework"},
        {"id": "Vue", "name": "Vue", "type": "framework"},
        {"id": "Angular", "name": "Angular", "type": "framework"},
        {"id": "Docker", "name": "Docker", "type": "tool"},
        {"id": "Kubernetes", "name": "Kubernetes", "type": "tool"},
        {"id": "Terraform", "name": "Terraform", "type": "tool"},
    ]
    edges = [
        # 社区 0 内部边
        {"source": "Python", "target": "Django", "relation": "has_framework", "weight": 1.0},
        {"source": "Python", "target": "FastAPI", "relation": "has_framework", "weight": 1.0},
        {"source": "Django", "target": "FastAPI", "relation": "related_to", "weight": 0.5},
        # 社区 1 内部边
        {"source": "React", "target": "Vue", "relation": "related_to", "weight": 0.5},
        {"source": "React", "target": "Angular", "relation": "related_to", "weight": 0.5},
        {"source": "Vue", "target": "Angular", "relation": "related_to", "weight": 0.5},
        # 社区 2 内部边
        {"source": "Docker", "target": "Kubernetes", "relation": "related_to", "weight": 1.0},
        {"source": "Docker", "target": "Terraform", "relation": "related_to", "weight": 0.5},
        {"source": "Kubernetes", "target": "Terraform", "relation": "related_to", "weight": 0.5},
        # 跨社区边（弱）
        {"source": "Python", "target": "Docker", "relation": "deployed_with", "weight": 0.3},
    ]
    return nodes, edges


# ============== Test 1: 基本社区检测 ==============


class TestDetectCommunitiesBasic:
    """test_detect_communities_basic — Mock 图快照含 3 个社区，验证分区数量"""

    @pytest.mark.skipif(not _HAS_LEIDEN, reason="leidenalg/igraph 未安装")
    def test_detects_3_communities(self):
        nodes, edges = _make_3_community_graph()
        adapter = _make_mock_adapter(nodes, edges)
        detector = CommunityDetector(adapter)

        result = detector.detect_communities(resolution=1.0, min_graph_size=5, min_community_size=2)

        assert isinstance(result, CommunityDetectionResult)
        assert result.total_nodes == 9
        assert result.total_edges == 10
        assert result.total_regions >= 2  # 至少 2 个社区（弱跨社区边可能导致合并）
        assert len(result.partitions) == result.total_regions
        assert result.modularity != 0.0  # 非平凡分区应有非零模块度
        assert result.timestamp  # ISO 时间戳非空

    @pytest.mark.skipif(not _HAS_LEIDEN, reason="leidenalg/igraph 未安装")
    def test_partitions_have_correct_fields(self):
        nodes, edges = _make_3_community_graph()
        adapter = _make_mock_adapter(nodes, edges)
        detector = CommunityDetector(adapter)

        result = detector.detect_communities(min_graph_size=5, min_community_size=2)

        for partition in result.partitions:
            assert isinstance(partition, RegionPartition)
            assert partition.region_id >= 0
            assert partition.region_name.startswith("region_")
            assert len(partition.entity_names) > 0
            assert len(partition.entity_types) > 0
            assert partition.edge_count >= 0
            assert isinstance(partition.modularity_score, float)

    @pytest.mark.skipif(not _HAS_LEIDEN, reason="leidenalg/igraph 未安装")
    def test_all_entities_assigned(self):
        """所有实体都应被分配到某个社区"""
        nodes, edges = _make_3_community_graph()
        adapter = _make_mock_adapter(nodes, edges)
        detector = CommunityDetector(adapter)

        result = detector.detect_communities(min_graph_size=5, min_community_size=2)

        all_assigned = set()
        for p in result.partitions:
            all_assigned.update(p.entity_names)

        expected = {n["name"] for n in nodes}
        assert all_assigned == expected


# ============== Test 2: 空图 ==============


class TestDetectCommunitiesEmptyGraph:
    """test_detect_communities_empty_graph — 空图返回空结果"""

    def test_empty_graph_returns_empty_result(self):
        adapter = _make_mock_adapter([], [])
        detector = CommunityDetector(adapter)

        result = detector.detect_communities()

        assert result.total_nodes == 0
        assert result.total_edges == 0
        assert result.total_regions == 0
        assert result.partitions == []
        assert result.modularity == 0.0

    def test_none_snapshot_returns_empty_result(self):
        adapter = MagicMock()
        adapter.get_graph_snapshot = MagicMock(return_value=None)
        detector = CommunityDetector(adapter)

        result = detector.detect_communities()

        assert result.partitions == []
        assert result.total_nodes == 0


# ============== Test 3: 单节点图 ==============


class TestDetectCommunitiesSingleNode:
    """test_detect_communities_single_node — 单节点返回 1 个区域含 1 个实体"""

    def test_single_node_returns_one_region(self):
        nodes = [{"id": "Python", "name": "Python", "type": "language"}]
        adapter = _make_mock_adapter(nodes, [])
        detector = CommunityDetector(adapter)

        result = detector.detect_communities(min_graph_size=1, min_community_size=1)

        assert result.total_regions == 1
        assert result.total_nodes == 1
        assert result.total_edges == 0
        assert len(result.partitions) == 1

        p = result.partitions[0]
        assert p.region_id == 0
        assert p.region_name == "region_0"
        assert p.entity_names == ["Python"]
        assert p.entity_types == {"language": 1}
        assert p.edge_count == 0

    def test_single_node_with_type_fallback(self):
        """节点缺少 type 字段时回退到 unknown"""
        nodes = [{"id": "Lonely", "name": "Lonely"}]
        adapter = _make_mock_adapter(nodes, [])
        detector = CommunityDetector(adapter)

        result = detector.detect_communities(min_graph_size=1, min_community_size=1)

        assert result.partitions[0].entity_types == {"unknown": 1}


# ============== Test 4: igraph 属性保留 ==============


class TestBuildIgraphPreservesAttributes:
    """test_build_igraph_preserves_attributes — 验证 entity_name 和 entity_type 保留在 igraph 中"""

    @pytest.mark.skipif(not _HAS_LEIDEN, reason="leidenalg/igraph 未安装")
    def test_vertex_attributes_preserved(self):
        nodes = [
            {"id": "Python", "name": "Python", "type": "language"},
            {"id": "Django", "name": "Django", "type": "framework"},
            {"id": "React", "name": "React", "type": "library"},
        ]
        edges = [
            {"source": "Python", "target": "Django", "relation": "has_framework", "weight": 1.0},
        ]
        adapter = _make_mock_adapter(nodes, edges)
        detector = CommunityDetector(adapter)

        graph = detector._build_igraph(nodes, edges)

        # 验证顶点数量
        assert graph.vcount() == 3

        # 验证 name 属性
        names = graph.vs["name"]
        assert "Python" in names
        assert "Django" in names
        assert "React" in names

        # 验证 entity_type 属性
        types = graph.vs["entity_type"]
        assert "language" in types
        assert "framework" in types
        assert "library" in types

    @pytest.mark.skipif(not _HAS_LEIDEN, reason="leidenalg/igraph 未安装")
    def test_edge_attributes_preserved(self):
        nodes = [
            {"id": "A", "name": "A", "type": "x"},
            {"id": "B", "name": "B", "type": "y"},
        ]
        edges = [
            {"source": "A", "target": "B", "relation": "connects", "description": "A-B link", "weight": 0.8},
        ]
        adapter = _make_mock_adapter(nodes, edges)
        detector = CommunityDetector(adapter)

        graph = detector._build_igraph(nodes, edges)

        assert graph.ecount() == 1
        assert graph.es[0]["relation"] == "connects"
        assert graph.es[0]["description"] == "A-B link"
        assert graph.es[0]["weight"] == pytest.approx(0.8)

    @pytest.mark.skipif(not _HAS_LEIDEN, reason="leidenalg/igraph 未安装")
    def test_isolated_nodes_preserved(self):
        """孤立节点（无边）仍保留在 igraph 中"""
        nodes = [
            {"id": "A", "name": "A", "type": "x"},
            {"id": "B", "name": "B", "type": "y"},
            {"id": "Isolated", "name": "Isolated", "type": "z"},
        ]
        edges = [
            {"source": "A", "target": "B", "relation": "related_to", "weight": 1.0},
        ]
        adapter = _make_mock_adapter(nodes, edges)
        detector = CommunityDetector(adapter)

        graph = detector._build_igraph(nodes, edges)

        assert graph.vcount() == 3  # 包含孤立节点
        assert graph.ecount() == 1
        assert "Isolated" in graph.vs["name"]


# ============== Test 5: RegionPartition 数据类 ==============


class TestRegionPartitionDataclass:
    """test_region_partition_dataclass — 验证 RegionPartition 字段和默认值"""

    def test_create_with_all_fields(self):
        p = RegionPartition(
            region_id=0,
            region_name="region_0",
            entity_names=["Python", "Django"],
            entity_types={"language": 1, "framework": 1},
            edge_count=1,
            modularity_score=0.35,
        )
        assert p.region_id == 0
        assert p.region_name == "region_0"
        assert p.entity_names == ["Python", "Django"]
        assert p.entity_types == {"language": 1, "framework": 1}
        assert p.edge_count == 1
        assert p.modularity_score == pytest.approx(0.35)

    def test_equality(self):
        p1 = RegionPartition(
            region_id=1,
            region_name="region_1",
            entity_names=["A"],
            entity_types={"x": 1},
            edge_count=0,
            modularity_score=0.0,
        )
        p2 = RegionPartition(
            region_id=1,
            region_name="region_1",
            entity_names=["A"],
            entity_types={"x": 1},
            edge_count=0,
            modularity_score=0.0,
        )
        assert p1 == p2

    def test_inequality(self):
        p1 = RegionPartition(
            region_id=0, region_name="r0", entity_names=["A"],
            entity_types={"x": 1}, edge_count=0, modularity_score=0.0,
        )
        p2 = RegionPartition(
            region_id=1, region_name="r1", entity_names=["B"],
            entity_types={"y": 1}, edge_count=0, modularity_score=0.0,
        )
        assert p1 != p2


# ============== CommunityDetectionResult 数据类测试 ==============


class TestCommunityDetectionResultDataclass:
    """验证 CommunityDetectionResult 字段"""

    def test_create_with_all_fields(self):
        r = CommunityDetectionResult(
            partitions=[],
            total_nodes=10,
            total_edges=20,
            total_regions=0,
            modularity=0.42,
            timestamp="2026-04-24T12:00:00+00:00",
        )
        assert r.total_nodes == 10
        assert r.total_edges == 20
        assert r.total_regions == 0
        assert r.modularity == pytest.approx(0.42)
        assert r.timestamp == "2026-04-24T12:00:00+00:00"


# ============== 优雅降级测试 ==============


class TestGracefulDegradation:
    """leidenalg 未安装时的优雅降级"""

    def test_returns_empty_when_leiden_not_installed(self):
        """leidenalg 未安装时返回空结果而非抛异常（需 ≥2 节点才触发 Leiden 路径）"""
        nodes = [
            {"id": "A", "name": "A", "type": "x"},
            {"id": "B", "name": "B", "type": "y"},
        ]
        edges = [{"source": "A", "target": "B", "relation": "r", "weight": 1.0}]
        adapter = _make_mock_adapter(nodes, edges)
        detector = CommunityDetector(adapter)

        # 模拟 leidenalg 未安装
        with patch("niu_api.internal.region_detector._HAS_LEIDEN", False):
            result = detector.detect_communities(min_graph_size=1, min_community_size=1)

        assert result.partitions == []
        assert result.total_nodes == 0


# ============== 社区内度数排序测试 ==============


class TestBuildPartitionsDegreeSort:
    """Test that _build_partitions sorts entity_names by in-community degree."""

    def test_entity_names_sorted_by_degree(self):
        """entity_names should be ordered by in-community degree (descending)."""
        import igraph as ig

        # Build a graph with clear degree differences in community [0, 1, 2]
        # B(1) connects to A, C, and D(3) — degree 2 within community
        # A(0) connects to B — degree 1 within community
        # C(2) connects to B — degree 1 within community
        # D(3) is in its own community, edge B-D is cross-community
        g = ig.Graph()
        g.add_vertices(4)
        g.vs["name"] = ["A", "B", "C", "D"]
        g.vs["entity_type"] = ["skill", "person", "org", "skill"]
        # Community [0,1,2] edges: A-B, B-C (B has degree 2, A and C have degree 1)
        # Cross-community edge: B-D
        g.add_edges([(0, 1), (1, 2), (1, 3)])  # A-B, B-C, B-D

        from niu_api.internal.region_detector import CommunityDetector
        detector = CommunityDetector.__new__(CommunityDetector)

        # Create a mock partition where community 0 = [0, 1, 2]
        class MockPartition:
            q = 0.5
            def __iter__(self):
                yield [0, 1, 2]
                yield [3]

        partitions = detector._build_partitions(g, MockPartition(), min_community_size=1)

        # First partition: subgraph of [0,1,2] has edges A-B and B-C
        # B(1) degree=2, A(0) degree=1, C(2) degree=1
        # Sorted by degree descending: B(2), then A(1) and C(1) in original order
        assert len(partitions) == 2
        p0 = partitions[0]
        assert p0.entity_names[0] == "B"  # Highest degree first
        assert set(p0.entity_names) == {"A", "B", "C"}


# ============== 脑区节点过滤测试 ==============


class TestBrainRegionFiltering:
    """Test that brainregion nodes are filtered from Leiden input."""

    def test_brainregion_nodes_filtered(self):
        """Brainregion nodes and their edges should be excluded from Leiden input."""
        nodes = [
            {"id": "Python", "name": "Python", "type": "language"},
            {"id": "Django", "name": "Django", "type": "framework"},
            {"id": "社交实体脑区", "name": "社交实体脑区", "type": "brainregion"},
        ]
        edges = [
            {"source": "Python", "target": "Django", "relation": "related_to", "weight": 1.0},
            {"source": "社交实体脑区", "target": "Python", "relation": "包含", "weight": 0.5},
            {"source": "社交实体脑区", "target": "Django", "relation": "包含", "weight": 0.5},
        ]
        adapter = _make_mock_adapter(nodes, edges)
        detector = CommunityDetector(adapter)

        result = detector.detect_communities(min_graph_size=1, min_community_size=1)

        # Brain region node should not appear in any partition's entity_names
        for p in result.partitions:
            assert "社交实体脑区" not in p.entity_names

    def test_brainregion_by_suffix_filtered(self):
        """Nodes with name ending in 脑区 should be filtered even without brainregion type."""
        nodes = [
            {"id": "Python", "name": "Python", "type": "language"},
            {"id": "自定义脑区", "name": "自定义脑区", "type": "other"},
        ]
        edges = [
            {"source": "自定义脑区", "target": "Python", "relation": "包含", "weight": 0.5},
        ]
        adapter = _make_mock_adapter(nodes, edges)
        detector = CommunityDetector(adapter)

        result = detector.detect_communities(min_graph_size=1, min_community_size=1)

        for p in result.partitions:
            assert "自定义脑区" not in p.entity_names