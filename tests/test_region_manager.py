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
    REGION_PREFIX,
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
        """为每个社区创建 brain:region:{name} 实体"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)
        result = _make_partition_result()

        region_names = await manager.create_region_nodes(result)

        # 应创建 2 个脑区
        assert len(region_names) == 2
        assert all(name.startswith(REGION_PREFIX) for name in region_names)

        # inject_entity 应被调用 2 次（每个社区一次）
        assert ingester.inject_entity.call_count == 2

        # 验证注入的实体类型为 BrainRegion
        for call_item in ingester.inject_entity.call_args_list:
            kwargs = call_item[1] if call_item[1] else call_item[0][0] if call_item[0] else {}
            # Check via keyword args
            if "entity_type" in kwargs:
                assert kwargs["entity_type"] == REGION_ENTITY_TYPE

    @pytest.mark.asyncio
    async def test_creates_anchor_and_belongs_to_relations(self):
        """创建 brain:Niu -> region 锚点关系和 region -> member belongs_to 关系"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)
        result = _make_partition_result()

        await manager.create_region_nodes(result)

        # inject_custom_kg 被调用：每个社区 1 次锚点 + 1 次成员关系 = 2 次/社区
        # 总共 4 次
        assert ingester.inject_custom_kg.call_count == 4

        # 验证锚点关系（第 1 和第 3 次调用）
        anchor_calls = [
            ingester.inject_custom_kg.call_args_list[0],
            ingester.inject_custom_kg.call_args_list[2],
        ]
        for call_item in anchor_calls:
            kwargs = call_item[1]
            relationships = kwargs.get("relationships", [])
            assert len(relationships) == 1
            assert relationships[0]["src_id"] == "brain:Niu"
            assert relationships[0]["keywords"] == ANCHOR_RELATION
            assert relationships[0]["weight"] == 1.0

        # 验证成员关系（第 2 和第 4 次调用）
        member_calls = [
            ingester.inject_custom_kg.call_args_list[1],
            ingester.inject_custom_kg.call_args_list[3],
        ]
        for call_item in member_calls:
            kwargs = call_item[1]
            relationships = kwargs.get("relationships", [])
            for rel in relationships:
                assert rel["keywords"] == BELONGS_TO_RELATION
                assert rel["weight"] == 0.8

    @pytest.mark.asyncio
    async def test_skips_brain_region_prefix_nodes(self):
        """跳过名称以 brain:region: 开头的现有节点"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        partition = RegionPartition(
            region_id=0,
            region_name="region_0",
            entity_names=["brain:region:OldRegion", "Python"],
            entity_types={"BrainRegion": 1, "language": 1},
            edge_count=0,
            modularity_score=0.0,
        )
        result = _make_partition_result([partition])

        region_names = await manager.create_region_nodes(result)

        # 应创建 1 个脑区（仅 Python 作为成员）
        assert len(region_names) == 1

        # 成员应只包含 Python
        member_call = ingester.inject_custom_kg.call_args_list[1]
        kwargs = member_call[1]
        relationships = kwargs.get("relationships", [])
        member_targets = [r["tgt_id"] for r in relationships]
        assert "brain:region:OldRegion" not in member_targets
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

        region_names = await manager.create_region_nodes(result)

        assert region_names == []
        ingester.inject_entity.assert_not_called()


# ============== Test 2: update_region_summaries ==============


class TestUpdateRegionSummaries:
    """test_update_region_summaries — 重新生成指定脑区的摘要"""

    @pytest.mark.asyncio
    async def test_updates_region_description(self):
        """更新脑区主节点的描述"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock get_region_members 返回成员列表
        # 需要模拟 explore_node 返回 belongs_to 边
        adapter.explore_node.return_value = {
            "center": {"id": "brain:region:Python", "name": "brain:region:Python", "type": "BrainRegion"},
            "nodes": [
                {"id": "brain:region:Python", "name": "brain:region:Python", "type": "BrainRegion",
                 "description": "summary | brain_meta_region_id:community_0 | brain_meta_size:3"},
            ],
            "edges": [
                {"source": "brain:region:Python", "target": "Python", "relation": "belongs_to", "weight": 0.8},
                {"source": "brain:region:Python", "target": "Django", "relation": "belongs_to", "weight": 0.8},
            ],
            "stats": {"nodes": 3, "edges": 2, "max_depth": 1},
        }

        await manager.update_region_summaries(["brain:region:Python"])

        # inject_entity 应被调用一次以更新
        ingester.inject_entity.assert_called_once()
        call_kwargs = ingester.inject_entity.call_args[1]
        assert call_kwargs["name"] == "brain:region:Python"
        assert call_kwargs["entity_type"] == REGION_ENTITY_TYPE
        # Description should contain brain_meta_* attributes
        assert "brain_meta_region_id:" in call_kwargs["description"]
        assert "brain_meta_size:" in call_kwargs["description"]

    @pytest.mark.asyncio
    async def test_skips_region_with_no_members(self):
        """无成员的脑区跳过更新"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # explore_node 返回无 belongs_to 边
        adapter.explore_node.return_value = {
            "center": None,
            "nodes": [],
            "edges": [],
            "stats": {"nodes": 0, "edges": 0, "max_depth": 1},
        }

        await manager.update_region_summaries(["brain:region:Empty"])

        # inject_entity 不应被调用
        ingester.inject_entity.assert_not_called()


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
                    "id": "brain:region:Python",
                    "entity_type": "BrainRegion",
                    "description": "Python(language)、Django(framework) | brain_meta_region_id:community_0 | brain_meta_size:3 | brain_meta_representative:Python | brain_meta_updated_at:1745366400",
                },
                {
                    "id": "brain:region:React",
                    "entity_type": "BrainRegion",
                    "description": "React(framework)、Vue(framework) | brain_meta_region_id:community_1 | brain_meta_size:3 | brain_meta_representative:React | brain_meta_updated_at:1745366400",
                },
            ],
        }

        regions = await manager.get_all_regions()

        assert len(regions) == 2
        assert all(isinstance(r, BrainRegionInfo) for r in regions)

        # 验证第一个区域
        r0 = regions[0]
        assert r0.name == "brain:region:Python"
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

        regions = await manager.get_all_regions()

        assert regions == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_regions(self):
        """无 BrainRegion 实体时返回空列表"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {"status": "ok", "data": []}

        regions = await manager.get_all_regions()

        assert regions == []


# ============== Test 4: get_region_members ==============


class TestGetRegionMembers:
    """test_get_region_members — 通过 belongs_to 关系获取成员"""

    @pytest.mark.asyncio
    async def test_returns_members_from_outgoing_edges(self):
        """从 region -> member 方向的 belongs_to 边获取成员"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.explore_node.return_value = {
            "center": {"id": "brain:region:Python"},
            "nodes": [],
            "edges": [
                {"source": "brain:region:Python", "target": "Python", "relation": "belongs_to", "weight": 0.8},
                {"source": "brain:region:Python", "target": "Django", "relation": "belongs_to", "weight": 0.8},
                {"source": "brain:region:Python", "target": "FastAPI", "relation": "belongs_to", "weight": 0.8},
                # 非成员边应被忽略
                {"source": "brain:Niu", "target": "brain:region:Python", "relation": "brain_region_anchor", "weight": 1.0},
            ],
            "stats": {"nodes": 4, "edges": 4, "max_depth": 1},
        }

        members = await manager.get_region_members("brain:region:Python")

        assert len(members) == 3
        assert "Python" in members
        assert "Django" in members
        assert "FastAPI" in members
        # brain:Niu 不应在成员列表中
        assert "brain:Niu" not in members

    @pytest.mark.asyncio
    async def test_handles_incoming_belongs_to_edges(self):
        """处理反向 belongs_to 边（member -> region）"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.explore_node.return_value = {
            "center": {"id": "brain:region:Python"},
            "nodes": [],
            "edges": [
                {"source": "Python", "target": "brain:region:Python", "relation": "belongs_to", "weight": 0.8},
            ],
            "stats": {"nodes": 2, "edges": 1, "max_depth": 1},
        }

        members = await manager.get_region_members("brain:region:Python")

        assert "Python" in members

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_members(self):
        """无 belongs_to 边时返回空列表"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.explore_node.return_value = {
            "center": None,
            "nodes": [],
            "edges": [],
            "stats": {"nodes": 0, "edges": 0, "max_depth": 1},
        }

        members = await manager.get_region_members("brain:region:Empty")

        assert members == []


# ============== Test 5: cleanup_stale_regions ==============


class TestCleanupStaleRegions:
    """test_cleanup_stale_regions — 清理不再存在的脑区主节点"""

    @pytest.mark.asyncio
    async def test_removes_stale_region_nodes(self):
        """删除不在当前分区中的脑区节点"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # Mock get_all_regions 返回 2 个已有脑区
        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "brain:region:Python",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_0 | brain_meta_size:3 | brain_meta_representative:Python | brain_meta_updated_at:1745366400",
                },
                {
                    "id": "brain:region:OldRegion",
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

        removed = await manager.cleanup_stale_regions(current_partition)

        # 只有 OldRegion 应被删除
        assert len(removed) == 1
        assert "brain:region:OldRegion" in removed
        adapter.delete_entity.assert_called_once_with("brain:region:OldRegion")

    @pytest.mark.asyncio
    async def test_no_stale_regions_returns_empty(self):
        """所有脑区都在当前分区中，无需清理"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "brain:region:Python",
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

        removed = await manager.cleanup_stale_regions(current_partition)

        assert removed == []
        adapter.delete_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_all_when_no_current_partitions(self):
        """当前分区为空时，所有脑区都被清理"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {
                    "id": "brain:region:Python",
                    "entity_type": "BrainRegion",
                    "description": "summary | brain_meta_region_id:community_0 | brain_meta_size:3 | brain_meta_representative:Python | brain_meta_updated_at:1745366400",
                },
                {
                    "id": "brain:region:React",
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

        removed = await manager.cleanup_stale_regions(empty_partition)

        assert len(removed) == 2
        assert adapter.delete_entity.call_count == 2


# ============== Description Encoding/Decoding Tests ==============


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
        # 属性之间用 | 分隔
        assert " | " in result

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


# ============== _summarize_region Heuristic Tests ==============


class TestSummarizeRegionHeuristic:
    """验证 _summarize_region 的启发式命名逻辑"""

    def test_returns_representative_as_name(self):
        """使用第一个实体名作为区域名"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        name, summary = manager._summarize_region([
            "Python(language)",
            "Django(framework)",
            "FastAPI(framework)",
        ])

        assert name == "Python"
        assert "Python(language)" in summary

    def test_empty_summaries_return_unknown(self):
        """空实体列表返回 unknown"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        name, summary = manager._summarize_region([])

        assert name == "unknown"
        assert summary == "空区域"

    def test_summary_limits_to_max_entities(self):
        """摘要最多包含 MAX_SUMMARY_ENTITIES 个实体"""
        adapter, ingester = _make_mock_adapter_and_ingester()
        manager = RegionManager(adapter, ingester)

        # 创建超过 5 个实体
        entities = [f"Entity{i}(type{i})" for i in range(10)]

        name, summary = manager._summarize_region(entities)

        assert name == "Entity0"
        # 摘要应包含实体数量信息
        assert "10个实体" in summary