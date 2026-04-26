"""
Tests for niu_api/internal/region_injector.py

Brain Context Injector 测试 — 验证 BrainContextInjector 的
区域地图格式、详细区域格式、摘要区域格式、激活加权排序、
token 预算控制和主入口返回格式化文本。
"""

import pytest

from unittest.mock import MagicMock

from niu_api.internal.region_activation import (
    BrainRegionState,
    RegionActivationManager,
    STATUS_DIMMING,
    STATUS_LIT,
    STATUS_OFF,
)
from niu_api.internal.region_injector import BrainContextInjector
from niu_api.internal.region_manager import BrainRegionInfo, RegionManager


# ============== 辅助函数 ==============


def _make_region_infos() -> list[BrainRegionInfo]:
    """创建测试用的 BrainRegionInfo 列表"""
    return [
        BrainRegionInfo(
            name="brain:region:编程开发",
            label="编程开发",
            community_id="community_0",
            description="Python/NumPy/Web技术栈",
            size=6,
            representative="Python",
            members=["Python", "NumPy", "Data_Analysis", "Web_Development", "Django", "FastAPI"],
            updated_at=1745366400.0,
        ),
        BrainRegionInfo(
            name="brain:region:项目管理",
            label="项目管理",
            community_id="community_1",
            description="AI_Bot项目，主开发者",
            size=4,
            representative="AI_Bot",
            members=["AI_Bot", "Project_Plan", "Sprint", "Backlog"],
            updated_at=1745366400.0,
        ),
        BrainRegionInfo(
            name="brain:region:日常偏好",
            label="日常偏好",
            community_id="community_2",
            description="暗色主题，远程办公",
            size=3,
            representative="暗色主题",
            members=["暗色主题", "远程办公", "MacOS"],
            updated_at=1745366400.0,
        ),
        BrainRegionInfo(
            name="brain:region:财务知识",
            label="财务知识",
            community_id="community_3",
            description="报销流程、预算审批",
            size=2,
            representative="报销",
            members=["报销", "预算审批"],
            updated_at=1745366400.0,
        ),
    ]


def _make_activation_manager() -> RegionActivationManager:
    """创建已初始化区域的 RegionActivationManager"""
    manager = RegionActivationManager()
    manager.initialize_from_regions(_make_region_infos())
    return manager


def _make_injector(
    activation_mgr: RegionActivationManager | None = None,
) -> BrainContextInjector:
    """创建测试用的 BrainContextInjector"""
    if activation_mgr is None:
        activation_mgr = _make_activation_manager()

    adapter = MagicMock()
    region_mgr = MagicMock(spec=RegionManager)
    return BrainContextInjector(
        adapter=adapter,
        activation_mgr=activation_mgr,
        region_mgr=region_mgr,
    )


def _set_activation(
    manager: RegionActivationManager,
    region_id: str,
    activation: float,
) -> None:
    """设置指定区域的激活值"""
    state = manager._regions.get(region_id)
    if state:
        state.activation = activation


# ============== Test 1: format_region_map ==============


class TestFormatRegionMap:
    """验证 format_region_map 格式和状态灯"""

    def test_format_region_map_with_status_lights(self):
        """区域地图包含状态灯和实体计数"""
        activation_mgr = _make_activation_manager()
        # Set different activation levels
        _set_activation(activation_mgr, "community_0", 1.0)  # lit
        _set_activation(activation_mgr, "community_1", 0.5)  # dimming
        _set_activation(activation_mgr, "community_2", 0.2)  # dimming
        _set_activation(activation_mgr, "community_3", 0.0)  # off

        injector = _make_injector(activation_mgr)
        regions = activation_mgr.get_region_map()

        result = injector.format_region_map(regions)

        # Should have header with region count
        assert "## 脑区状态 (4个脑区)" in result
        # Should contain status lights
        assert STATUS_LIT in result  # 🟢
        assert STATUS_DIMMING in result  # 🟡
        assert STATUS_OFF in result  # ⚫
        # Should contain region labels
        assert "编程开发" in result
        assert "项目管理" in result
        assert "日常偏好" in result
        assert "财务知识" in result
        # Should contain entity counts
        assert "(6实体)" in result
        assert "(4实体)" in result
        assert "(3实体)" in result
        assert "(2实体)" in result

    def test_format_region_map_empty(self):
        """空区域列表返回空字符串"""
        injector = _make_injector()
        result = injector.format_region_map([])
        assert result == ""

    def test_format_region_map_sorted_by_status(self):
        """区域按状态灯排序: lit -> dimming -> off"""
        activation_mgr = _make_activation_manager()
        _set_activation(activation_mgr, "community_0", 1.0)  # lit
        _set_activation(activation_mgr, "community_1", 0.0)  # off
        _set_activation(activation_mgr, "community_2", 0.5)  # dimming
        _set_activation(activation_mgr, "community_3", 0.8)  # lit

        injector = _make_injector(activation_mgr)
        regions = activation_mgr.get_region_map()
        result = injector.format_region_map(regions)
        lines = result.split("\n")

        # First line is header, rest are region lines
        # Lit regions should come before dimming, dimming before off
        lit_indices = [i for i, l in enumerate(lines) if STATUS_LIT in l]
        dimming_indices = [i for i, l in enumerate(lines) if STATUS_DIMMING in l]
        off_indices = [i for i, l in enumerate(lines) if STATUS_OFF in l]

        if lit_indices and dimming_indices:
            assert max(lit_indices) < min(dimming_indices)
        if dimming_indices and off_indices:
            assert max(dimming_indices) < min(off_indices)


# ============== Test 2: format_detailed_region ==============


class TestFormatDetailedRegion:
    """验证 format_detailed_region 详细格式"""

    def test_detailed_region_format(self):
        """高激活区域输出包含实体列表"""
        activation_mgr = _make_activation_manager()
        injector = _make_injector(activation_mgr)

        region = BrainRegionState(
            region_id="community_0",
            label="编程开发",
            activation=0.9,
            last_activated_at=1745366400.0,
            activation_count=3,
            manually_dimmed=False,
        )
        members = ["Python", "NumPy", "Data_Analysis"]

        result = injector.format_detailed_region(
            region, members, budget=500, knowledge=""
        )

        assert "### [编程开发] (活跃)" in result
        assert "实体:" in result
        assert "Python" in result
        assert "NumPy" in result

    def test_detailed_region_with_knowledge(self):
        """高激活区域包含知识片段"""
        activation_mgr = _make_activation_manager()
        injector = _make_injector(activation_mgr)

        region = BrainRegionState(
            region_id="community_0",
            label="编程开发",
            activation=0.95,
            last_activated_at=1745366400.0,
            activation_count=5,
            manually_dimmed=False,
        )
        members = ["Python"]
        knowledge = "Python is used for AI/ML since 2019\nNumPy provides array operations\nData analysis workflow"

        result = injector.format_detailed_region(
            region, members, budget=2000, knowledge=knowledge
        )

        assert "知识:" in result
        assert "Python is used for AI/ML" in result

    def test_detailed_region_no_members(self):
        """无成员时显示 (无实体)"""
        activation_mgr = _make_activation_manager()
        injector = _make_injector(activation_mgr)

        region = BrainRegionState(
            region_id="community_0",
            label="编程开发",
            activation=0.9,
            last_activated_at=1745366400.0,
            activation_count=1,
            manually_dimmed=False,
        )

        result = injector.format_detailed_region(
            region, members=[], budget=500, knowledge=""
        )

        assert "(无实体)" in result


# ============== Test 3: format_summary_region ==============


class TestFormatSummaryRegion:
    """验证 format_summary_region 摘要格式"""

    def test_summary_region_format(self):
        """中激活区域输出摘要格式"""
        activation_mgr = _make_activation_manager()
        injector = _make_injector(activation_mgr)

        region = BrainRegionState(
            region_id="community_1",
            label="项目管理",
            activation=0.5,
            last_activated_at=1745366400.0,
            activation_count=2,
            manually_dimmed=False,
        )

        result = injector.format_summary_region(region)

        assert "### [项目管理] (近期)" in result

    def test_summary_region_fallback_description(self):
        """无描述时使用默认描述"""
        activation_mgr = _make_activation_manager()
        injector = _make_injector(activation_mgr)

        region = BrainRegionState(
            region_id="community_1",
            label="项目管理",
            activation=0.4,
            last_activated_at=1745366400.0,
            activation_count=1,
            manually_dimmed=False,
        )

        result = injector.format_summary_region(region)

        # Should contain a description line (either custom or fallback)
        lines = result.split("\n")
        assert len(lines) >= 2
        assert "### [项目管理] (近期)" in lines[0]

    def test_summary_region_with_entity_count(self):
        """无描述时显示实体计数"""
        # Create a region with no description to trigger the fallback path
        infos = _make_region_infos()
        # Remove description from community_1 to test fallback
        for info in infos:
            if info.community_id == "community_1":
                info.description = ""

        activation_mgr = RegionActivationManager()
        activation_mgr.initialize_from_regions(infos)
        injector = _make_injector(activation_mgr)

        region = BrainRegionState(
            region_id="community_1",
            label="项目管理",
            activation=0.4,
            last_activated_at=1745366400.0,
            activation_count=1,
            manually_dimmed=False,
        )

        result = injector.format_summary_region(region)

        # community_1 has 4 members, so fallback should mention "4个实体"
        assert "4个实体" in result


# ============== Test 4: apply_activation_weight ==============


class TestApplyActivationWeight:
    """验证 apply_activation_weight 分数加权和重排序"""

    def test_score_boosting(self):
        """激活区域中的实体获得分数提升"""
        activation_mgr = _make_activation_manager()
        _set_activation(activation_mgr, "community_0", 1.0)  # Python region

        injector = _make_injector(activation_mgr)

        query_results = [
            {"entity_name": "Python", "score": 0.5},
            {"entity_name": "UnknownEntity", "score": 0.8},
        ]

        boosted = injector.apply_activation_weight(
            query_results, boost_factor=0.3
        )

        # Python: 0.5 + 1.0 * 0.3 = 0.8
        # UnknownEntity: 0.8 + 0.0 = 0.8
        python_result = next(r for r in boosted if r["entity_name"] == "Python")
        unknown_result = next(r for r in boosted if r["entity_name"] == "UnknownEntity")

        assert python_result["score"] == pytest.approx(0.8)
        assert unknown_result["score"] == pytest.approx(0.8)

    def test_score_boosting_changes_order(self):
        """分数提升可以改变排序"""
        activation_mgr = _make_activation_manager()
        _set_activation(activation_mgr, "community_0", 1.0)  # Python region

        injector = _make_injector(activation_mgr)

        query_results = [
            {"entity_name": "UnknownEntity", "score": 0.6},
            {"entity_name": "Python", "score": 0.2},
        ]

        boosted = injector.apply_activation_weight(
            query_results, boost_factor=0.5
        )

        # Python: 0.2 + 1.0 * 0.5 = 0.7
        # UnknownEntity: 0.6 + 0.0 = 0.6
        # Python should now be first
        assert boosted[0]["entity_name"] == "Python"
        assert boosted[0]["score"] == pytest.approx(0.7)

    def test_empty_results(self):
        """空结果列表返回空列表"""
        injector = _make_injector()
        result = injector.apply_activation_weight([])
        assert result == []

    def test_no_mutation(self):
        """不修改原始结果列表"""
        activation_mgr = _make_activation_manager()
        injector = _make_injector(activation_mgr)

        query_results = [{"entity_name": "Python", "score": 0.5}]
        original_score = query_results[0]["score"]

        injector.apply_activation_weight(query_results, boost_factor=0.3)

        # Original should not be modified
        assert query_results[0]["score"] == original_score


# ============== Test 5: context_budget_control ==============


class TestContextBudgetControl:
    """验证 token 预算控制 — 超出预算时截断"""

    def test_truncation_when_exceeding_budget(self):
        """超出预算时截断知识片段"""
        activation_mgr = _make_activation_manager()
        injector = _make_injector(activation_mgr)

        region = BrainRegionState(
            region_id="community_0",
            label="编程开发",
            activation=0.9,
            last_activated_at=1745366400.0,
            activation_count=1,
            manually_dimmed=False,
        )
        members = ["Python"]

        # Very long knowledge text with a tiny budget
        long_knowledge = "\n".join(
            [f"Knowledge line {i}: " + "x" * 200 for i in range(10)]
        )

        result = injector.format_detailed_region(
            region, members, budget=50, knowledge=long_knowledge
        )

        # Result should be truncated (much shorter than full knowledge)
        # Budget of 50 tokens = 200 chars, so result should be bounded
        assert len(result) < len(long_knowledge)
        assert "### [编程开发] (活跃)" in result

    def test_entity_truncation_under_budget(self):
        """实体列表在预算不足时被截断"""
        activation_mgr = _make_activation_manager()
        injector = _make_injector(activation_mgr)

        region = BrainRegionState(
            region_id="community_0",
            label="编程开发",
            activation=0.9,
            last_activated_at=1745366400.0,
            activation_count=1,
            manually_dimmed=False,
        )
        # Many members
        members = [f"Entity_{i}" for i in range(50)]

        # Very small budget (forces truncation)
        result = injector.format_detailed_region(
            region, members, budget=20, knowledge=""
        )

        # Should contain header but truncated entity list
        assert "### [编程开发] (活跃)" in result
        # Should not contain all 50 entities
        assert "Entity_49" not in result

    def test_budget_preserves_content_when_sufficient(self):
        """预算充足时保留完整内容"""
        activation_mgr = _make_activation_manager()
        injector = _make_injector(activation_mgr)

        region = BrainRegionState(
            region_id="community_0",
            label="编程开发",
            activation=0.9,
            last_activated_at=1745366400.0,
            activation_count=1,
            manually_dimmed=False,
        )
        members = ["Python", "NumPy"]
        knowledge = "Python is great"

        result = injector.format_detailed_region(
            region, members, budget=2000, knowledge=knowledge
        )

        # All content should be present
        assert "Python" in result
        assert "NumPy" in result
        assert "Python is great" in result


# ============== Test 6: inject_brain_context_returns_text ==============


class TestInjectBrainContextReturnsText:
    """验证 inject_brain_context 主入口返回格式化文本"""

    def test_returns_formatted_text_on_activation(self):
        """查询命中实体时返回包含区域地图的文本"""
        activation_mgr = _make_activation_manager()
        injector = _make_injector(activation_mgr)

        # Mock adapter to return hit entities
        injector._adapter.query_data = MagicMock(return_value={
            "data": {
                "entities": [
                    {"entity_name": "Python", "entity_type": "skill"},
                    {"entity_name": "NumPy", "entity_type": "skill"},
                ]
            }
        })
        injector._adapter.query = MagicMock(return_value="")

        text = injector.inject_brain_context("Python数据分析")

        # Should contain region map header
        assert "## 脑区状态" in text
        # Should contain the activated region
        assert "编程开发" in text

    def test_returns_empty_on_empty_query(self):
        """空查询返回空字符串"""
        injector = _make_injector()
        result = injector.inject_brain_context("")
        assert result == ""

    def test_returns_text_on_no_hits(self):
        """无命中实体时仍返回区域地图"""
        activation_mgr = _make_activation_manager()
        injector = _make_injector(activation_mgr)

        # Mock adapter to return no entities
        injector._adapter.query_data = MagicMock(return_value={
            "data": {"entities": []}
        })

        result = injector.inject_brain_context("unknown query")

        # Should still have region map (all off/dimming)
        assert "## 脑区状态" in result
