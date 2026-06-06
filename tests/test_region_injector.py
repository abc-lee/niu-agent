"""
Tests for niu_api/internal/region_injector.py

Brain Context Injector 测试 — 验证 BrainContextInjector 的
区域地图格式、脑区点亮数量软控制、format_region_map_only、
和主入口返回格式化文本。
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


# ============== Test 2: format_region_map_only ==============


class TestFormatRegionMapOnly:
    """验证 format_region_map_only 直接返回区域地图"""

    def test_format_region_map_only_returns_map(self):
        """format_region_map_only 返回区域地图"""
        activation_mgr = _make_activation_manager()
        injector = _make_injector(activation_mgr)

        result = injector.format_region_map_only()

        assert "## 脑区状态" in result
        assert "编程开发" in result

    def test_format_region_map_only_empty_when_no_regions(self):
        """无区域时返回空字符串"""
        activation_mgr = RegionActivationManager()
        injector = _make_injector(activation_mgr)

        result = injector.format_region_map_only()

        assert result == ""


# ============== Test 3: brain region lit count soft control ==============


class TestLitCountSoftControl:
    """验证脑区点亮数量软控制"""

    def test_format_region_map_warns_too_many_lit(self):
        """点亮超过5个脑区时应输出警告提示"""
        activation_mgr = RegionActivationManager()
        # Create 6 lit regions (activation > 0.3)
        for i in range(6):
            activation_mgr._regions[f"region_{i}"] = BrainRegionState(
                region_id=f"region_{i}", community_id="",
                label=f"测试脑区{i}", activation=0.8,
                last_activated_at=0, activation_count=1, manually_dimmed=False,
            )
        injector = _make_injector(activation_mgr)

        result = injector.format_region_map_only()

        assert "建议关闭" in result

    def test_format_region_map_no_warn_within_limit(self):
        """点亮5个以内脑区时不应输出警告"""
        activation_mgr = RegionActivationManager()
        # Create 3 lit regions (activation > 0.3)
        for i in range(3):
            activation_mgr._regions[f"region_{i}"] = BrainRegionState(
                region_id=f"region_{i}", community_id="",
                label=f"测试脑区{i}", activation=0.8,
                last_activated_at=0, activation_count=1, manually_dimmed=False,
            )
        injector = _make_injector(activation_mgr)

        result = injector.format_region_map_only()

        assert "建议关闭" not in result

    def test_format_region_map_warn_at_exactly_six(self):
        """恰好6个点亮脑区时也输出警告"""
        activation_mgr = RegionActivationManager()
        for i in range(6):
            activation_mgr._regions[f"region_{i}"] = BrainRegionState(
                region_id=f"region_{i}", community_id="",
                label=f"测试脑区{i}", activation=0.5,
                last_activated_at=0, activation_count=1, manually_dimmed=False,
            )
        # Add an off region to verify the count is about lit, not total
        activation_mgr._regions["region_off"] = BrainRegionState(
            region_id="region_off", community_id="",
            label="关闭脑区", activation=0.0,
            last_activated_at=0, activation_count=0, manually_dimmed=False,
        )
        injector = _make_injector(activation_mgr)

        result = injector.format_region_map_only()

        assert "6个脑区已点亮" in result

    def test_format_region_map_no_warn_at_five(self):
        """恰好5个点亮脑区时不输出警告（>5才警告）"""
        activation_mgr = RegionActivationManager()
        for i in range(5):
            activation_mgr._regions[f"region_{i}"] = BrainRegionState(
                region_id=f"region_{i}", community_id="",
                label=f"测试脑区{i}", activation=0.5,
                last_activated_at=0, activation_count=1, manually_dimmed=False,
            )
        injector = _make_injector(activation_mgr)

        result = injector.format_region_map_only()

        assert "建议关闭" not in result


# ============== Test 4: inject_brain_context_returns_text ==============


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
