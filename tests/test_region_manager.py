"""
Tests for niu_api/internal/region_manager.py

Brain Region Node Management 测试 — 验证 RegionManager 对脑区主节点的
创建、查询、更新和清理操作。
"""

import pytest
from unittest.mock import MagicMock, call

from niu_api.internal.region_detector import (
    CommunityDetectionResult,
    RegionPartition,
)
from niu_api.internal.region_manager import (
    BrainRegionInfo,
    RegionManager,
    _encode_description,
    _parse_description,
    ANCHOR_RELATION,
    BELONGS_TO_RELATION,
    REGION_ENTITY_TYPE,
    REGION_SUFFIX,
)


# ============== 辅助函数 ==============


def _make_partition_result(
    partitions: list[RegionPartition] | None = None,
) -> CommunityDetectionResult:
    """创建一个 CommunityDetectionResult 用于测试"""
    if partitions is None:
        partitions = [
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=["Python", "Django", "FastAPI"],
                entity_types={"language": 1, "framework": 2},
                edge_count=2,
                modularity_score=0.15,
            ),
            RegionPartition(
                region_id=1,
                region_name="region_1",
                entity_names=["React", "Vue", "Angular"],
                entity_types={"framework": 3},
                edge_count=3,
                modularity_score=0.15,
            ),
        ]
    return CommunityDetectionResult(
        partitions=partitions,
        total_nodes=6,
        total_edges=5,
        total_regions=len(partitions),
        modularity=0.3,
        timestamp="2026-04-24T12:00:00+00:00",
    )


def _make_mock_adapter_and_ingester() -> tuple[MagicMock, MagicMock]:
    """创建 mock adapter 和 ingester"""
    adapter = MagicMock()
    ingester = MagicMock()

    # 默认 inject_entity 返回成功
    ingester.inject_entity.return_value = {"status": "ok", "entities": 1}

    # 默认 inject_custom_kg 返回成功
    ingester.inject_custom_kg.return_value = {
        "status": "ok",
        "entities": 0,
        "relationships": 1,
        "chunks": 0,
    }

    # 默认 list_entities 返回空
    adapter.list_entities.return_value = {"status": "ok", "data": []}

    # 默认 explore_node 返回空结果
    adapter.explore_node.return_value = {
        "center": None,
        "nodes": [],
        "edges": [],
        "stats": {"nodes": 0, "edges": 0, "max_depth": 1},
    }

    # 默认 delete_entity 返回成功
    adapter.delete_entity.return_value = {"status": "ok"}

    return adapter, ingester


# ============== Test 1: create_region_nodes ==============


class TestCreateRegionNodes:
    """test_create_region_nodes — 从 CommunityDetectionResult 创建主节点"""

    @pytest.mark.asyncio
    async def test_creates_region_entities(self):
        """为每个社区创建 XXX脑区 实体"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)
        manager._generate_labels = lambda summaries_list, existing: [
            summaries[0].split("(")[0] if summaries else "unknown"
            for summaries in summaries_list
        ]

        # Create partitions with enough members to pass MIN_COMMUNITY_SIZE check
        partitions = [
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=[f"Entity{i}" for i in range(100)],  # 100 members
                entity_types={"language": 50, "framework": 50},
                edge_count=2,
                modularity_score=0.15,
            ),
            RegionPartition(
                region_id=1,
                region_name="region_1",
                entity_names=[f"Node{i}" for i in range(100)],  # 100 members
                entity_types={"framework": 100},
                edge_count=3,
                modularity_score=0.15,
            ),
        ]
        result = _make_partition_result(partitions)

        region_names = manager.create_region_nodes(result)

        # 应创建 2 个脑区
        assert len(region_names) == 2
        assert all(name.endswith(REGION_SUFFIX) for name in region_names)

        # inject_custom_kg 应被调用 1 次（批量注入）
        assert ingester.inject_custom_kg.call_count == 1

        # 验证注入的实体类型为 BrainRegion
        call_kwargs = ingester.inject_custom_kg.call_args[1]
        entities = call_kwargs.get("entities", [])
        assert len(entities) == 2
        for entity in entities:
            assert entity["entity_type"] == REGION_ENTITY_TYPE

    @pytest.mark.asyncio
    async def test_creates_anchor_and_belongs_to_relations(self):
        """创建 Niu -> region 锚点关系和 region -> member belongs_to 关系"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)
        manager._generate_labels = lambda summaries_list, existing: [
            summaries[0].split("(")[0] if summaries else "unknown"
            for summaries in summaries_list
        ]

        partitions = [
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=["Python", "Django", "FastAPI"] + [f"E{i}" for i in range(97)],
                entity_types={"language": 50, "framework": 50},
                edge_count=2,
                modularity_score=0.15,
            ),
            RegionPartition(
                region_id=1,
                region_name="region_1",
                entity_names=["React", "Vue", "Angular"] + [f"N{i}" for i in range(97)],
                entity_types={"framework": 100},
                edge_count=3,
                modularity_score=0.15,
            ),
        ]
        result = _make_partition_result(partitions)

        manager.create_region_nodes(result)

        # Batch inject: 1 call total
        assert ingester.inject_custom_kg.call_count == 1

        call_kwargs = ingester.inject_custom_kg.call_args[1]
        relationships = call_kwargs.get("relationships", [])

        # Verify anchor relations (Niu -> region)
        anchor_rels = [r for r in relationships if r["keywords"] == ANCHOR_RELATION]
        assert len(anchor_rels) == 2
        for rel in anchor_rels:
            assert rel["src_id"] == "Niu"
            assert rel["weight"] == 0.5

        # Verify belongs_to relations (region -> member)
        belongs_rels = [r for r in relationships if r["keywords"] == BELONGS_TO_RELATION]
        assert len(belongs_rels) == 200  # 100 members per region * 2 regions
        for rel in belongs_rels:
            assert rel["weight"] == 0.5

    @pytest.mark.asyncio
    async def test_skips_brain_region_nodes(self):
        """跳过名称以 XXX脑区 格式的现有脑区节点"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)
        manager._generate_labels = lambda summaries_list, existing: [
            summaries[0].split("(")[0] if summaries else "unknown"
            for summaries in summaries_list
        ]

        # "OldRegion脑区" is a brain region name and should be filtered from members.
        # Remaining members must be >= MIN_COMMUNITY_SIZE to create a region.
        partition = RegionPartition(
            region_id=0,
            region_name="region_0",
            entity_names=["OldRegion脑区", "Python"] + [f"E{i}" for i in range(100)],
            entity_types={"BrainRegion": 1, "language": 100},
            edge_count=0,
            modularity_score=0.0,
        )
        result = _make_partition_result([partition])

        region_names = manager.create_region_nodes(result)

        # Should create 1 region (脑区 names filtered out)
        assert len(region_names) == 1

        # Verify brain region names are NOT in the belongs_to members
        call_kwargs = ingester.inject_custom_kg.call_args[1]
        relationships = call_kwargs.get("relationships", [])
        member_targets = [r["tgt_id"] for r in relationships if r["keywords"] == BELONGS_TO_RELATION]
        assert "OldRegion脑区" not in member_targets
        assert "Python" in member_targets

    @pytest.mark.asyncio
    async def test_empty_partition_returns_empty(self):
        """空分区结果返回空列表"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        result = CommunityDetectionResult(
            partitions=[],
            total_nodes=0,
            total_edges=0,
            total_regions=0,
            modularity=0.0,
            timestamp="2026-04-24T12:00:00+00:00",
        )

        region_names = manager.create_region_nodes(result)

        assert region_names == []
        ingester.inject_custom_kg.assert_not_called()


# ============== Test 2: update_region_summaries ==============


class TestUpdateRegionSummaries:
    """test_update_region_summaries — 重新生成指定脑区的摘要"""

    @pytest.mark.asyncio
    async def test_updates_region_description(self):
        """更新脑区主节点的描述"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock list_entities to return the region entity
        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_0 | brain_meta_size:3 | brain_meta_representative:Python | brain_meta_updated_at:1745366400",
                },
            ],
        }

        # Mock lightrag_manager.get_region_members
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "niu_api.internal.lightrag_manager.get_region_members",
                lambda name: ["Python", "Django"] if name == "Python脑区" else [],
            )
            manager.update_region_summaries(["Python脑区"])

        # inject_custom_kg 应被调用一次以更新（batch inject）
        ingester.inject_custom_kg.assert_called_once()
        call_kwargs = ingester.inject_custom_kg.call_args[1]
        entities = call_kwargs["entities"]
        assert len(entities) == 1
        assert entities[0]["entity_name"] == "Python脑区"
        assert entities[0]["entity_type"] == REGION_ENTITY_TYPE
        # Description should contain brain_meta_* attributes
        assert "brain_meta_region_id:" in entities[0]["description"]
        assert "brain_meta_size:" in entities[0]["description"]

    @pytest.mark.asyncio
    async def test_skips_region_with_no_members(self):
        """无成员的脑区跳过更新"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock lightrag_manager.get_region_members to return empty
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "niu_api.internal.lightrag_manager.get_region_members",
                lambda name: [],
            )
            manager.update_region_summaries(["Empty脑区"])

        # inject_entity 不应被调用
        ingester.inject_custom_kg.assert_not_called()


# ============== Test 3: get_all_regions ==============


class TestGetAllRegions:
    """test_get_all_regions — 查询所有 BrainRegion 实体"""

    @pytest.mark.asyncio
    async def test_returns_brain_region_infos(self):
        """返回 BrainRegionInfo 列表"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock list_entities 返回 BrainRegion 实体
        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "entity_type": "BrainRegion",
                    "description": "Python(language)、Django(framework) | brain_meta_region_id:community_0 | brain_meta_size:3 | brain_meta_representative:Python | brain_meta_updated_at:1745366400",
                },
                {
                    "id": "React脑区",
                    "entity_type": "BrainRegion",
                    "description": "React(framework)、Vue(framework) | brain_meta_region_id:community_1 | brain_meta_size:3 | brain_meta_representative:React | brain_meta_updated_at:1745366400",
                },
            ],
        }

        regions = manager.get_all_regions()

        assert len(regions) == 2
        assert all(isinstance(r, BrainRegionInfo) for r in regions)

        # 验证第一个区域
        r0 = regions[0]
        assert r0.name == "Python脑区"
        assert r0.label == "Python"
        assert r0.community_id == "community_0"
        assert r0.size == 3
        assert r0.representative == "Python"
        assert r0.updated_at == 1745366400.0

        # 验证 list_entities 调用参数
        adapter.list_entities.assert_called_once_with(
            list_type="entities",
            entity_type=REGION_ENTITY_TYPE,
            limit=1000,
        )

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self):
        """查询失败时返回空列表"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {"status": "error", "message": "failed"}

        regions = manager.get_all_regions()

        assert regions == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_regions(self):
        """无 BrainRegion 实体时返回空列表"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {"status": "ok", "data": []}

        regions = manager.get_all_regions()

        assert regions == []


# ============== Test 4: get_region_members ==============


class TestGetRegionMembers:
    """test_get_region_members — 通过 lightrag_manager 获取成员"""

    @pytest.mark.asyncio
    async def test_returns_members_from_lightrag_manager(self):
        """从 lightrag_manager.get_region_members 获取成员"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock lightrag_manager.get_region_members
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "niu_api.internal.lightrag_manager.get_region_members",
                lambda name: ["Python", "Django", "FastAPI"] if name == "Python脑区" else [],
            )
            members = manager.get_region_members("Python脑区")

        assert len(members) == 3
        assert "Python" in members
        assert "Django" in members
        assert "FastAPI" in members

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_members(self):
        """无成员时返回空列表"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock lightrag_manager.get_region_members to return empty
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "niu_api.internal.lightrag_manager.get_region_members",
                lambda name: [],
            )
            members = manager.get_region_members("Empty脑区")

        assert members == []


# ============== Test 5: cleanup_stale_regions ==============


class TestCleanupStaleRegions:
    """test_cleanup_stale_regions — 清理不再存在的脑区主节点"""

    @pytest.mark.asyncio
    async def test_removes_stale_region_nodes(self):
        """删除不在当前分区中的脑区节点（Jaccard=0，无成员重叠）"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock get_all_regions 返回 2 个已有脑区
        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_0 | brain_meta_size:3 | brain_meta_representative:Python | brain_meta_updated_at:1745366400",
                },
                {
                    "id": "OldRegion脑区",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_99 | brain_meta_size:2 | brain_meta_representative:OldEntity | brain_meta_updated_at:1745366400",
                },
            ],
        }

        # 当前分区只有 community_0，community_99 已不存在
        current_partition = _make_partition_result([
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=["Python", "Django"],
                entity_types={"language": 1, "framework": 1},
                edge_count=1,
                modularity_score=0.15,
            ),
        ])

        # Mock get_all_region_members: Python脑区 shares members with community_0,
        # OldRegion脑区 has no overlap (will be removed)
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "niu_api.internal.lightrag_manager.get_all_region_members",
                lambda: {
                    "Python脑区": ["Python", "Django"],
                    "OldRegion脑区": ["OldEntity", "LegacyLib"],
                },
            )
            removed, drifted, drifted_cids = manager.cleanup_stale_regions(current_partition)

        # 只有 OldRegion 应被删除
        assert len(removed) == 1
        assert "OldRegion脑区" in removed
        adapter.delete_entity.assert_called_once_with("OldRegion脑区")

    @pytest.mark.asyncio
    async def test_no_stale_regions_returns_empty(self):
        """所有脑区成员与当前分区高度重叠，无需清理"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_0 | brain_meta_size:3 | brain_meta_representative:Python | brain_meta_updated_at:1745366400",
                },
            ],
        }

        current_partition = _make_partition_result([
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=["Python", "Django"],
                entity_types={"language": 1, "framework": 1},
                edge_count=1,
                modularity_score=0.15,
            ),
        ])

        # Mock: Python脑区 members fully overlap with community_0
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "niu_api.internal.lightrag_manager.get_all_region_members",
                lambda: {"Python脑区": ["Python", "Django"]},
            )
            removed, drifted, drifted_cids = manager.cleanup_stale_regions(current_partition)

        assert removed == []
        assert drifted == []
        assert drifted_cids == set()
        adapter.delete_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_all_when_no_current_partitions(self):
        """当前分区为空时，所有脑区都被清理（Jaccard=0，无分区可匹配）"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_0 | brain_meta_size:3 | brain_meta_representative:Python | brain_meta_updated_at:1745366400",
                },
                {
                    "id": "React脑区",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_1 | brain_meta_size:3 | brain_meta_representative:React | brain_meta_updated_at:1745366400",
                },
            ],
        }

        empty_partition = CommunityDetectionResult(
            partitions=[],
            total_nodes=0,
            total_edges=0,
            total_regions=0,
            modularity=0.0,
            timestamp="2026-04-24T12:00:00+00:00",
        )

        # Mock: both regions have members but no communities to match
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "niu_api.internal.lightrag_manager.get_all_region_members",
                lambda: {
                    "Python脑区": ["Python", "Django"],
                    "React脑区": ["React", "Vue"],
                },
            )
            removed, drifted, drifted_cids = manager.cleanup_stale_regions(empty_partition)

        assert len(removed) == 2
        assert adapter.delete_entity.call_count == 2

    @pytest.mark.asyncio
    async def test_detects_membership_drift(self):
        """成员部分重叠时，触发漂移检测（0 < Jaccard < drift_threshold）"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "OldCommunity脑区",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_0 | brain_meta_size:5 | brain_meta_representative:A | brain_meta_updated_at:1745366400",
                },
            ],
        }

        # Partition has mostly different members — only 1 out of 10 overlap
        # Jaccard = 1/10 = 0.1 < 0.3 threshold → drift
        current_partition = _make_partition_result([
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=["A", "X", "Y", "Z", "W", "Q", "R", "S", "T", "U"],
                entity_types={"type1": 5, "type2": 5},
                edge_count=5,
                modularity_score=0.2,
            ),
        ])

        # OldCommunity脑区 has 5 members, only "A" overlaps with community_0's 10 members
        # Jaccard = |{A}| / |{A,B,C,D,E,X,Y,Z,W,Q,R,S,T,U}| = 1/14 ≈ 0.07
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "niu_api.internal.lightrag_manager.get_all_region_members",
                lambda: {"OldCommunity脑区": ["A", "B", "C", "D", "E"]},
            )
            m.setattr(
                "niu_api.internal.lightrag_manager.remove_region_edges",
                lambda name, etype: 0,
            )
            removed, drifted, drifted_cids = manager.cleanup_stale_regions(current_partition)

        # Should detect drift, not removal
        assert removed == []
        assert len(drifted) == 1
        assert "OldCommunity脑区" in drifted
        assert "community_0" in drifted_cids

    @pytest.mark.asyncio
    async def test_no_drift_when_membership_overlaps(self):
        """成员重叠度高时不标记漂移（Jaccard >= drift_threshold）"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Stable脑区",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_0 | brain_meta_size:4 | brain_meta_representative:A | brain_meta_updated_at:1745366400",
                },
            ],
        }

        current_partition = _make_partition_result([
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=["A", "B", "C", "D", "E"],
                entity_types={"type1": 5},
                edge_count=3,
                modularity_score=0.2,
            ),
        ])

        # Stable脑区 has 4 members, 3 overlap with community_0's 5 members
        # Jaccard = |{A,B,C}| / |{A,B,C,D,E}| = 3/5 = 0.6 >= 0.3 → stable
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "niu_api.internal.lightrag_manager.get_all_region_members",
                lambda: {"Stable脑区": ["A", "B", "C", "D"]},
            )
            removed, drifted, drifted_cids = manager.cleanup_stale_regions(current_partition)

        assert removed == []
        assert drifted == []
        assert drifted_cids == set()

    @pytest.mark.asyncio
    async def test_default_regions_protected_from_drift(self):
        """默认脑区不参与漂移检测和删除"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "聊天历史脑区",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_default | brain_meta_size:2 | brain_meta_representative:User | brain_meta_updated_at:1745366400",
                },
            ],
        }

        # No matching community — would be Jaccard=0 (stale) if not protected
        current_partition = _make_partition_result([
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=["Python", "Django"],
                entity_types={"language": 1, "framework": 1},
                edge_count=1,
                modularity_score=0.15,
            ),
        ])

        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "niu_api.internal.lightrag_manager.get_all_region_members",
                lambda: {"聊天历史脑区": ["User", "Session"]},
            )
            removed, drifted, drifted_cids = manager.cleanup_stale_regions(current_partition)

        # Default region should not be removed or marked as drifted
        assert "聊天历史脑区" not in removed
        assert "聊天历史脑区" not in drifted


class TestDescriptionEncoding:
    """验证 brain_meta_* 属性的编码和解码"""

    def test_encode_description(self):
        """编码包含 brain_meta_* 属性的描述"""
        result = _encode_description(
            summary="Python编程、Django框架",
            region_id="community_3",
            size=6,
            representative="Python",
            updated_at=1745366400.0,
        )

        assert "Python编程、Django框架" in result
        assert "brain_meta_region_id:community_3" in result
        assert "brain_meta_size:6" in result
        assert "brain_meta_representative:Python" in result
        assert "brain_meta_updated_at:1745366400" in result
        # 属性之间用 <SEP> 分隔
        assert "<SEP>" in result

    def test_parse_description(self):
        """解析包含 brain_meta_* 属性的描述"""
        description = (
            "Python编程、Django框架 | brain_meta_region_id:community_3 | "
            "brain_meta_size:6 | brain_meta_representative:Python | "
            "brain_meta_updated_at:1745366400"
        )

        parsed = _parse_description(description)

        assert parsed["summary"] == "Python编程、Django框架"
        assert parsed["region_id"] == "community_3"
        assert parsed["size"] == "6"
        assert parsed["representative"] == "Python"
        assert parsed["updated_at"] == "1745366400"

    def test_roundtrip_encode_parse(self):
        """编码后再解码应恢复原始数据"""
        original = {
            "summary": "Web开发技术栈",
            "region_id": "community_0",
            "size": 5,
            "representative": "React",
            "updated_at": 1745366400.0,
        }

        encoded = _encode_description(**original)
        parsed = _parse_description(encoded)

        assert parsed["summary"] == original["summary"]
        assert parsed["region_id"] == original["region_id"]
        assert parsed["size"] == str(original["size"])
        assert parsed["representative"] == original["representative"]
        assert parsed["updated_at"] == str(int(original["updated_at"]))

    def test_parse_empty_description(self):
        """空描述返回空字典"""
        parsed = _parse_description("")
        assert parsed["summary"] == ""
        assert parsed["region_id"] == ""




class TestGenerateRegionSummary:
    """Test _generate_region_summary — top-10 entity names joined by <SEP>."""

    def test_summary_uses_sep_separator(self):
        """Summary should use <SEP> separator between entity names."""
        manager = RegionManager.__new__(RegionManager)
        entity_summaries = ["Python(skill)", "Django(framework)", "FastAPI(framework)"]
        result = manager._generate_region_summary(entity_summaries)
        assert result == "Python<SEP>Django<SEP>FastAPI"

    def test_summary_top_10_entities(self):
        """Summary should include at most 10 entity names."""
        manager = RegionManager.__new__(RegionManager)
        entity_summaries = [f"E{i}(type)" for i in range(15)]
        result = manager._generate_region_summary(entity_summaries)
        parts = result.split("<SEP>")
        assert len(parts) == 10

    def test_summary_extracts_name_only(self):
        """Summary should contain entity names without type labels."""
        manager = RegionManager.__new__(RegionManager)
        entity_summaries = ["Python(skill)", "Django(framework)"]
        result = manager._generate_region_summary(entity_summaries)
        assert "skill" not in result
        assert "framework" not in result
        assert "Python" in result
        assert "Django" in result

    def test_summary_empty_input(self):
        """Empty input should return empty string."""
        manager = RegionManager.__new__(RegionManager)
        result = manager._generate_region_summary([])
        assert result == ""

    def test_summary_sanitizes_sep_in_names(self):
        """Entity names containing <SEP> or | should be sanitized."""
        manager = RegionManager.__new__(RegionManager)
        entity_summaries = ["Bad<SEP>Name(type)", "Pipe|Name(type2)"]
        result = manager._generate_region_summary(entity_summaries)
        assert "Bad-Name" in result
        assert "Pipe-Name" in result


class TestGenerateRegionLabel:
    """Test _generate_region_label — LLM-generated semantic region label."""

    def test_returns_label_from_llm_json(self):
        """Should extract label from LLM JSON response."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        manager._call_llm_for_label = lambda prompt: '{"label": "编程开发"}'

        entity_summaries = ["Python(skill)", "Django(framework)", "FastAPI(framework)"]
        existing_regions = []

        result = manager._generate_region_label(entity_summaries, existing_regions)
        assert result == "编程开发"

    def test_fallback_on_json_parse_failure(self):
        """Should fallback to entity_names[0] when JSON parse fails after retry."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        def bad_llm(prompt):
            return "这是一个编程相关的社区"
        manager._call_llm_for_label = bad_llm

        entity_summaries = ["Python(skill)", "Django(framework)"]
        existing_regions = []

        result = manager._generate_region_label(entity_summaries, existing_regions)
        assert result == "Python"

    def test_regex_fallback_on_malformed_json(self):
        """Should try regex extraction when JSON parse fails."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        manager._call_llm_for_label = lambda prompt: '结果是 {"label": "编程开发"} 哦'

        entity_summaries = ["Python(skill)", "Django(framework)"]
        existing_regions = []

        result = manager._generate_region_label(entity_summaries, existing_regions)
        assert result == "编程开发"

    def test_label_truncated_over_8_chars(self):
        """Label should be truncated to 8 characters."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        manager._call_llm_for_label = lambda prompt: '{"label": "这是一个非常非常长的标签名称"}'

        entity_summaries = ["Python(skill)"]
        existing_regions = []

        result = manager._generate_region_label(entity_summaries, existing_regions)
        assert len(result) <= 8

    def test_duplicate_label_gets_suffix(self):
        """Should add numeric suffix when label duplicates existing region."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        manager._call_llm_for_label = lambda prompt: '{"label": "编程开发"}'

        entity_summaries = ["Python(skill)"]
        existing_regions = ["编程开发"]

        result = manager._generate_region_label(entity_summaries, existing_regions)
        assert result.startswith("编程开发")
        assert result != "编程开发"

    def test_empty_input_returns_unknown(self):
        """Empty entity_summaries should return 'unknown'."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        result = manager._generate_region_label([], [])
        assert result == "unknown"


class TestCreateRegionNodesWithLLMLabel:
    """Test create_region_nodes uses _generate_region_label for naming."""

    def test_uses_llm_label_for_region_name(self):
        """Region name should use _generate_region_label result."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)
        manager._generate_labels = lambda summaries_list, existing: ["编程开发"] * len(summaries_list)

        partitions = [
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=[f"E{i}" for i in range(100)],
                entity_types={"skill": 100},
                edge_count=2,
                modularity_score=0.15,
            ),
        ]
        result = _make_partition_result(partitions)
        region_names = manager.create_region_nodes(result)

        assert len(region_names) == 1
        assert region_names[0] == "编程开发脑区"

    def test_injects_chunks_with_unique_source_id(self):
        """Chunks should have source_id matching entity's rewritten source_id format."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)
        manager._generate_labels = lambda summaries_list, existing: ["编程开发"] * len(summaries_list)

        partitions = [
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=[f"E{i}" for i in range(100)],
                entity_types={"skill": 100},
                edge_count=2,
                modularity_score=0.15,
            ),
        ]
        result = _make_partition_result(partitions)
        manager.create_region_nodes(result)

        call_kwargs = ingester.inject_custom_kg.call_args[1]
        chunks = call_kwargs.get("chunks", [])
        assert len(chunks) >= 1
        chunk = chunks[0]
        assert chunk["source_id"] == "brain_编程开发脑区"

    def test_entity_source_id_is_base(self):
        """Entity source_id should be base 'brain' (inject_custom_kg will rewrite it)."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)
        manager._generate_labels = lambda summaries_list, existing: ["编程开发"] * len(summaries_list)

        partitions = [
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=[f"E{i}" for i in range(100)],
                entity_types={"skill": 100},
                edge_count=2,
                modularity_score=0.15,
            ),
        ]
        result = _make_partition_result(partitions)
        manager.create_region_nodes(result)

        call_kwargs = ingester.inject_custom_kg.call_args[1]
        entities = call_kwargs.get("entities", [])
        assert len(entities) == 1
        assert entities[0]["source_id"] == "brain"

    def test_chunk_content_contains_label_and_members(self):
        """Chunk content should include region label and top member names."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)
        manager._generate_labels = lambda summaries_list, existing: ["编程开发"] * len(summaries_list)

        partitions = [
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=[f"E{i}" for i in range(100)],
                entity_types={"skill": 100},
                edge_count=2,
                modularity_score=0.15,
            ),
        ]
        result = _make_partition_result(partitions)
        manager.create_region_nodes(result)

        call_kwargs = ingester.inject_custom_kg.call_args[1]
        chunks = call_kwargs.get("chunks", [])
        assert len(chunks) >= 1
        assert "编程开发" in chunks[0]["content"]


class TestUpdateRegionSummariesNoLLM:
    """Test update_region_summaries does NOT call _generate_region_label."""

    def test_update_does_not_call_generate_label(self):
        """update_region_summaries should not call _generate_region_label."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock _generate_region_label to track calls
        label_calls = []
        original_fn = manager._generate_region_label
        def track_label_calls(*args, **kwargs):
            label_calls.append(1)
            return original_fn(*args, **kwargs)
        manager._generate_region_label = track_label_calls

        # Setup: return existing region data
        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "description": _encode_description(
                        summary="旧摘要", region_id="community_0",
                        size=3, representative="Python", updated_at=1000.0,
                    ),
                },
            ],
        }

        # Mock get_region_members
        from unittest.mock import patch
        with patch.object(manager, "get_region_members", return_value=["Python", "Django"]):
            manager.update_region_summaries(["Python脑区"])

        assert label_calls == []

    def test_update_uses_generate_region_summary(self):
        """update_region_summaries should use _generate_region_summary format."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "description": _encode_description(
                        summary="旧摘要", region_id="community_0",
                        size=3, representative="Python", updated_at=1000.0,
                    ),
                },
            ],
        }

        from unittest.mock import patch
        with patch.object(manager, "get_region_members", return_value=["Python", "Django"]):
            manager.update_region_summaries(["Python脑区"])

        call_kwargs = ingester.inject_custom_kg.call_args[1]
        entities = call_kwargs.get("entities", [])
        assert len(entities) == 1
        desc = entities[0]["description"]
        assert "Python" in desc


class TestBatchLabelGeneration:
    """Test batch LLM label generation for 3+ regions."""

    def test_batch_label_for_many_regions(self):
        """When 3+ regions, should use single batch LLM call."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        batch_called = []
        def mock_batch(prompts_list, existing):
            batch_called.append(len(prompts_list))
            return {i: f"标签{i}" for i in range(len(prompts_list))}
        manager._generate_region_labels_batch = mock_batch

        single_called = []
        original_single = manager._generate_region_label
        def mock_single(summaries, existing):
            single_called.append(1)
            return original_single(summaries, existing)
        manager._generate_region_label = mock_single

        entity_summaries_list = [
            ["Python(skill)", "Django(framework)"],
            ["任飞(person)", "李明(person)"],
            ["雄安分行(org)", "河北分行(org)"],
        ]
        existing = []
        labels = manager._generate_labels(entity_summaries_list, existing)

        assert len(labels) == 3
        assert batch_called == [3]
        assert single_called == []

    def test_individual_label_for_few_regions(self):
        """When < 3 regions, should use individual LLM calls."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        manager._generate_region_label = lambda summaries, existing: "测试标签"

        entity_summaries_list = [
            ["Python(skill)"],
            ["任飞(person)"],
        ]
        existing = []
        labels = manager._generate_labels(entity_summaries_list, existing)

        assert len(labels) == 2

    def test_batch_dedup_same_label_for_multiple_regions(self):
        """When batch LLM returns same label for multiple regions, dedup should rename correctly."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock batch to return the same label for regions 0 and 1
        def mock_batch(prompts_list, existing):
            return {0: "编程", 1: "编程", 2: "开发"}
        manager._generate_region_labels_batch = mock_batch

        entity_summaries_list = [
            ["Python(skill)", "Django(framework)"],
            ["React(skill)", "Vue(framework)"],
            ["雄安分行(org)", "河北分行(org)"],
        ]
        existing = []
        labels = manager._generate_labels(entity_summaries_list, existing)

        assert len(labels) == 3
        # labels[0] and labels[1] must be different (one gets a numeric suffix)
        assert labels[0] != labels[1]
        # The first occurrence keeps the original label
        assert labels[0] == "编程"
        # The duplicate gets a suffix like "编程2"
        assert labels[1].startswith("编程")
        assert labels[1] != "编程"
        # The third label is unaffected
        assert labels[2] == "开发"

    def test_batch_fallback_on_missing_regions(self):
        """When batch returns fewer labels than input, fallback to individual."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        def mock_batch(prompts_list, existing):
            return {0: "标签0", 1: "标签1"}
        manager._generate_region_labels_batch = mock_batch
        manager._generate_region_label = lambda summaries, existing: "备用名"

        entity_summaries_list = [
            ["Python(skill)"],
            ["任飞(person)"],
            ["雄安分行(org)"],
        ]
        existing = []
        labels = manager._generate_labels(entity_summaries_list, existing)

        assert len(labels) == 3
        assert labels[0] == "标签0"
        assert labels[1] == "标签1"
        assert labels[2] == "备用名"


class TestSummaryDisplayFormat:
    """Test that BrainRegionInfo.description uses readable separator for display."""

    def test_description_replaces_sep_with_chinese_comma(self):
        """BrainRegionInfo.description should replace <SEP> with '、' for display."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Setup: region with <SEP> format summary
        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "编程开发脑区",
                    "description": _encode_description(
                        summary="Python<SEP>Django<SEP>FastAPI",
                        region_id="community_0",
                        size=3,
                        representative="Python",
                        updated_at=1000.0,
                    ),
                },
            ],
        }

        regions = manager.get_all_regions()
        assert len(regions) == 1
        assert regions[0].description == "Python、Django、FastAPI"


class TestSkipRelationshipInjectionForExistingRegions:
    """test_skips_relationship_injection_for_existing_regions — 已存在脑区只更新描述，不注入关系和chunk"""

    def test_existing_region_only_updates_description(self):
        """已存在脑区只 append entity，不 append relationship 和 chunk"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock _generate_labels to return "编程开发" for partition 0 and "React" for partition 1
        manager._generate_labels = lambda summaries_list, existing: ["编程开发", "React"]

        # Mock get_all_regions to return an existing region "编程开发脑区"
        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "编程开发脑区",
                    "entity_type": "BrainRegion",
                    "description": _encode_description(
                        summary="Python<SEP>Django",
                        region_id="community_0",
                        size=3,
                        representative="Python",
                        updated_at=1000.0,
                    ),
                },
            ],
        }

        # Partition 0 maps to "编程开发" label → "编程开发脑区" (existing)
        # Partition 1 maps to "React" label → "React脑区" (new)
        partitions = [
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=["Python", "Django", "FastAPI"] + [f"E{i}" for i in range(97)],
                entity_types={"language": 50, "framework": 50},
                edge_count=2,
                modularity_score=0.15,
            ),
            RegionPartition(
                region_id=1,
                region_name="region_1",
                entity_names=["React", "Vue", "Angular"] + [f"N{i}" for i in range(97)],
                entity_types={"framework": 100},
                edge_count=3,
                modularity_score=0.15,
            ),
        ]
        result = _make_partition_result(partitions)

        created_regions = manager.create_region_nodes(result)

        # created_regions should only contain the NEW region
        assert "编程开发脑区" not in created_regions
        assert "React脑区" in created_regions
        assert len(created_regions) == 1

        # Verify inject_custom_kg was called
        assert ingester.inject_custom_kg.call_count == 1
        call_kwargs = ingester.inject_custom_kg.call_args[1]

        # Both entities should be present (existing gets description update)
        entities = call_kwargs.get("entities", [])
        entity_names = [e["entity_name"] for e in entities]
        assert "编程开发脑区" in entity_names
        assert "React脑区" in entity_names

        # Relationships should NOT contain 编程开发脑区
        relationships = call_kwargs.get("relationships", [])
        rel_targets = [r["tgt_id"] for r in relationships]
        assert "编程开发脑区" not in rel_targets
        # React脑区 should have anchor + belongs_to relationships
        assert "React脑区" in rel_targets

        # Chunks should NOT contain 编程开发脑区
        chunks = call_kwargs.get("chunks", [])
        chunk_source_ids = [c["source_id"] for c in chunks]
        assert "brain_编程开发脑区" not in chunk_source_ids
        assert "brain_React脑区" in chunk_source_ids

    def test_skip_community_ids_filters_partitions(self):
        """skip_community_ids 参数过滤漂移分区"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)
        manager._generate_labels = lambda summaries_list, existing: [
            summaries[0].split("(")[0] if summaries else "unknown"
            for summaries in summaries_list
        ]

        partitions = [
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=[f"E{i}" for i in range(100)],
                entity_types={"skill": 100},
                edge_count=2,
                modularity_score=0.15,
            ),
            RegionPartition(
                region_id=1,
                region_name="region_1",
                entity_names=[f"N{i}" for i in range(100)],
                entity_types={"framework": 100},
                edge_count=3,
                modularity_score=0.15,
            ),
        ]
        result = _make_partition_result(partitions)

        # Skip community_0
        created_regions = manager.create_region_nodes(result, skip_community_ids={"community_0"})

        # Only region_1 should be processed
        assert len(created_regions) == 1

        call_kwargs = ingester.inject_custom_kg.call_args[1]
        entities = call_kwargs.get("entities", [])
        assert len(entities) == 1

    def test_all_existing_regions_no_relationships(self):
        """所有脑区都已存在时，只更新描述，不注入任何关系和chunk"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)
        manager._generate_labels = lambda summaries_list, existing: [
            summaries[0].split("(")[0] if summaries else "unknown"
            for summaries in summaries_list
        ]

        # Mock get_all_regions to return both regions as existing
        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "entity_type": "BrainRegion",
                    "description": _encode_description(
                        summary="Python<SEP>Django",
                        region_id="community_0",
                        size=3,
                        representative="Python",
                        updated_at=1000.0,
                    ),
                },
                {
                    "id": "React脑区",
                    "entity_type": "BrainRegion",
                    "description": _encode_description(
                        summary="React<SEP>Vue",
                        region_id="community_1",
                        size=3,
                        representative="React",
                        updated_at=1000.0,
                    ),
                },
            ],
        }

        partitions = [
            RegionPartition(
                region_id=0,
                region_name="region_0",
                entity_names=["Python", "Django", "FastAPI"] + [f"E{i}" for i in range(97)],
                entity_types={"language": 50, "framework": 50},
                edge_count=2,
                modularity_score=0.15,
            ),
            RegionPartition(
                region_id=1,
                region_name="region_1",
                entity_names=["React", "Vue", "Angular"] + [f"N{i}" for i in range(97)],
                entity_types={"framework": 100},
                edge_count=3,
                modularity_score=0.15,
            ),
        ]
        result = _make_partition_result(partitions)

        created_regions = manager.create_region_nodes(result)

        # No new regions created
        assert created_regions == []

        # inject_custom_kg should still be called (for entity description updates)
        assert ingester.inject_custom_kg.call_count == 1
        call_kwargs = ingester.inject_custom_kg.call_args[1]

        # Entities present (description updates)
        entities = call_kwargs.get("entities", [])
        assert len(entities) == 2

        # No relationships or chunks
        relationships = call_kwargs.get("relationships", [])
        chunks = call_kwargs.get("chunks", [])
        assert relationships == []
        assert chunks == []


class TestUpdateRegionSummariesPreservesTypeInfo:
    """D-16 fix: update_region_summaries preserves entity type info from NetworkX graph."""

    def test_type_info_from_graph_in_summary(self):
        """Entity types from NetworkX graph should be passed to _build_entity_summaries."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock list_entities to return the region entity
        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "description": _encode_description(
                        summary="Python<SEP>Django",
                        region_id="community_0",
                        size=2,
                        representative="Python",
                        updated_at=1000.0,
                    ),
                },
            ],
        }

        # Mock _get_rag to return a LightRAG instance with node data
        mock_rag = MagicMock()
        mock_kg = MagicMock()
        mock_nx_graph = MagicMock()
        mock_nx_graph.nodes = {
            "python": {"entity_type": "language"},
            "django": {"entity_type": "framework"},
        }
        mock_nx_graph.__contains__ = lambda _, key: key in mock_nx_graph.nodes
        mock_kg._graph = mock_nx_graph
        mock_rag.chunk_entity_relation_graph = mock_kg
        adapter._get_rag.return_value = mock_rag

        # Spy on _build_entity_summaries to capture its entity_name_to_type arg
        original_build = manager._build_entity_summaries
        captured_type_map = {}
        def spy_build(members, entity_types, entity_name_to_type=None):
            if entity_name_to_type:
                captured_type_map.update(entity_name_to_type)
            return original_build(members, entity_types, entity_name_to_type)
        manager._build_entity_summaries = spy_build

        from unittest.mock import patch
        with patch.object(manager, "get_region_members", return_value=["Python", "Django"]), \
             patch("niu_api.internal.lightrag_manager.graph_read_lock", return_value=MagicMock()):
            manager.update_region_summaries(["Python脑区"])

        # Verify entity_name_to_type was populated from graph node data
        assert captured_type_map == {"Python": "language", "Django": "framework"}

    def test_fallback_when_rag_is_none(self):
        """When _get_rag returns None, summary still works (types become 'unknown')."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "description": _encode_description(
                        summary="Python(language)<SEP>Django(framework)",
                        region_id="community_0",
                        size=2,
                        representative="Python",
                        updated_at=1000.0,
                    ),
                },
            ],
        }

        # _get_rag returns None
        adapter._get_rag.return_value = None

        from unittest.mock import patch
        with patch.object(manager, "get_region_members", return_value=["Python", "Django"]):
            manager.update_region_summaries(["Python脑区"])

        # Should still work (with 'unknown' types as before the fix)
        ingester.inject_custom_kg.assert_called_once()

    def test_fallback_when_graph_raises_exception(self):
        """When graph read raises an exception, summary still works."""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "Python脑区",
                    "description": _encode_description(
                        summary="Python(language)<SEP>Django(framework)",
                        region_id="community_0",
                        size=2,
                        representative="Python",
                        updated_at=1000.0,
                    ),
                },
            ],
        }

        # _get_rag raises an exception
        adapter._get_rag.side_effect = RuntimeError("graph not ready")

        from unittest.mock import patch
        with patch.object(manager, "get_region_members", return_value=["Python", "Django"]):
            manager.update_region_summaries(["Python脑区"])

        # Should still work (exception caught, falls back to empty mapping)
        ingester.inject_custom_kg.assert_called_once()