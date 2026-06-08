"""
Tests for niu_api/internal/region_activation.py

Brain Region Activation Manager 测试 — 验证 RegionActivationManager 的
激活、衰减、强化、溢出和手动控制机制。
"""

import time

from niu_api.internal.region_manager import BrainRegionInfo
from niu_api.internal.region_activation import (
    RegionActivationManager,
    STATUS_LIT,
    STATUS_DIMMING,
    STATUS_OFF,
)


# ============== 辅助函数 ==============


def _make_region_infos() -> list[BrainRegionInfo]:
    """创建测试用的 BrainRegionInfo 列表"""
    return [
        BrainRegionInfo(
            name="Python脑区",
            label="Python",
            community_id="community_0",
            description="Python编程语言",
            size=3,
            representative="Python",
            members=["Python", "Django", "FastAPI"],
            updated_at=1745366400.0,
        ),
        BrainRegionInfo(
            name="React脑区",
            label="React",
            community_id="community_1",
            description="React前端框架",
            size=3,
            representative="React",
            members=["React", "Vue", "Angular"],
            updated_at=1745366400.0,
        ),
        BrainRegionInfo(
            name="Database脑区",
            label="Database",
            community_id="community_2",
            description="数据库技术",
            size=2,
            representative="PostgreSQL",
            members=["PostgreSQL", "Redis"],
            updated_at=1745366400.0,
        ),
    ]


def _make_manager_with_regions(
    decay_factor: float = 0.92,
    activation_threshold: float = 0.3,
    spillover_factor: float = 0.3,
    tool_reinforce_value: float = 0.85,
    neighbor_map: dict[str, set[str]] | None = None,
) -> RegionActivationManager:
    """创建已初始化区域的 RegionActivationManager"""
    manager = RegionActivationManager(
        decay_factor=decay_factor,
        activation_threshold=activation_threshold,
        spillover_factor=spillover_factor,
        tool_reinforce_value=tool_reinforce_value,
    )
    manager.initialize_from_regions(_make_region_infos())
    if neighbor_map is not None:
        manager.set_region_neighbors(neighbor_map)
    return manager


# ============== Test 1: activate_regions ==============


class TestActivateRegions:
    """验证 activate_regions 激活机制"""

    def test_activate_regions_sets_activation_to_1(self):
        """命中实体的区域激活值设为 1.0"""
        manager = _make_manager_with_regions()

        activated = manager.activate_regions(
            hit_entities=["Python"],
            entity_to_region={},
        )

        # "Python脑区" 应被激活
        assert "Python脑区" in activated
        state = manager._regions["Python脑区"]
        assert state.activation == 1.0
        assert state.activation_count == 1

    def test_activate_regions_uses_provided_mapping(self):
        """使用传入的 entity_to_region 映射"""
        manager = _make_manager_with_regions()

        activated = manager.activate_regions(
            hit_entities=["React"],
            entity_to_region={"React": "React脑区"},
        )

        assert "React脑区" in activated
        assert manager._regions["React脑区"].activation == 1.0

    def test_activate_regions_ignores_unknown_entities(self):
        """未知实体不激活任何区域"""
        manager = _make_manager_with_regions()

        activated = manager.activate_regions(
            hit_entities=["UnknownEntity"],
            entity_to_region={},
        )

        assert len(activated) == 0

    def test_activate_regions_sets_last_activated_at(self):
        """激活时更新 last_activated_at 时间戳"""
        manager = _make_manager_with_regions()
        before_time = time.time()

        manager.activate_regions(
            hit_entities=["Python"],
            entity_to_region={},
        )

        state = manager._regions["Python脑区"]
        assert state.last_activated_at >= before_time

    def test_activate_multiple_regions(self):
        """多个不同区域的实体同时激活多个区域"""
        manager = _make_manager_with_regions()

        activated = manager.activate_regions(
            hit_entities=["Python", "React"],
            entity_to_region={},
        )

        assert len(activated) == 2
        assert "Python脑区" in activated
        assert "React脑区" in activated


# ============== Test 2: reinforce_by_tool_use ==============


class TestReinforceByToolUse:
    """验证 reinforce_by_tool_use 强化机制"""

    def test_reinforce_sets_activation_to_max(self):
        """强化时 activation = max(current, 0.85)"""
        manager = _make_manager_with_regions()

        # 先让区域有较低激活值
        manager._regions["Python脑区"].activation = 0.4

        result = manager.reinforce_by_tool_use(
            tool_name="kg-server/query",
            tool_to_region={"kg-server/query": "Python脑区"},
        )

        assert result == "Python脑区"
        assert manager._regions["Python脑区"].activation == 0.85

    def test_reinforce_does_not_reduce_higher_activation(self):
        """强化不降低已有高激活值"""
        manager = _make_manager_with_regions()

        # 区域已有高激活值
        manager._regions["Python脑区"].activation = 1.0

        manager.reinforce_by_tool_use(
            tool_name="kg-server/query",
            tool_to_region={"kg-server/query": "Python脑区"},
        )

        # activation 保持在 1.0（max(1.0, 0.85) = 1.0）
        assert manager._regions["Python脑区"].activation == 1.0

    def test_reinforce_unknown_tool_returns_none(self):
        """未知工具返回 None"""
        manager = _make_manager_with_regions()

        result = manager.reinforce_by_tool_use(
            tool_name="unknown-tool",
            tool_to_region={"kg-server/query": "Python脑区"},
        )

        assert result is None

    def test_reinforce_unknown_region_returns_none(self):
        """未知区域返回 None"""
        manager = _make_manager_with_regions()

        result = manager.reinforce_by_tool_use(
            tool_name="kg-server/query",
            tool_to_region={"kg-server/query": "NonExistent脑区"},
        )

        assert result is None


# ============== Test 3: reinforce steady state ==============


class TestReinforceSteadyState:
    """验证强化稳态 — 连续使用工具时 activation 振荡在 0.78-0.85"""

    def test_reinforce_steady_state(self):
        """连续工具使用时，激活值振荡在稳态区间"""
        manager = _make_manager_with_regions()

        # Turn 1: tool call -> 0.85
        manager.reinforce_by_tool_use(
            tool_name="kg-server/query",
            tool_to_region={"kg-server/query": "Python脑区"},
        )
        assert manager._regions["Python脑区"].activation == 0.85

        # Simulate 10 turns of continuous tool use
        for _ in range(10):
            # Decay
            manager.decay_all()
            activation_after_decay = manager._regions["Python脑区"].activation

            # Reinforce
            manager.reinforce_by_tool_use(
                tool_name="kg-server/query",
                tool_to_region={"kg-server/query": "Python脑区"},
            )
            activation_after_reinforce = manager._regions["Python脑区"].activation

            # After decay: ~0.78 (0.85 * 0.92)
            # After reinforce: back to 0.85 (max(~0.78, 0.85))
            assert activation_after_decay >= 0.78 * 0.99  # allow small rounding
            assert activation_after_decay <= 0.85
            assert activation_after_reinforce == 0.85

        # Steady state: oscillates between ~0.78 and 0.85, well above threshold 0.3
        assert manager._regions["Python脑区"].activation > 0.3


# ============== Test 4: decay curve ==============


class TestDecayCurve:
    """验证 0.92^n 衰减曲线"""

    def test_decay_curve(self):
        """验证 activation *= 0.92 衰减曲线"""
        manager = _make_manager_with_regions(decay_factor=0.92)

        # Start with full activation
        manager._regions["Python脑区"].activation = 1.0

        # Track expected decay
        expected = 1.0

        for turn in range(30):
            manager.decay_all()
            expected *= 0.92
            actual = manager._regions["Python脑区"].activation

            # Allow small floating-point tolerance
            assert abs(actual - expected) < 0.01, (
                f"Turn {turn + 1}: expected {expected:.4f}, got {actual:.4f}"
            )

    def test_decay_curve_key_points(self):
        """验证衰减曲线的关键节点"""
        manager = _make_manager_with_regions(decay_factor=0.92)
        manager._regions["Python脑区"].activation = 1.0

        # Turn 5: 0.92^5 ≈ 0.66
        for _ in range(5):
            manager.decay_all()
        assert abs(manager._regions["Python脑区"].activation - 0.66) < 0.01

        # Continue to turn 10: 0.92^10 ≈ 0.43
        for _ in range(5):
            manager.decay_all()
        assert abs(manager._regions["Python脑区"].activation - 0.43) < 0.02

        # Continue to turn 20: 0.92^20 ≈ 0.19
        for _ in range(10):
            manager.decay_all()
        assert abs(manager._regions["Python脑区"].activation - 0.19) < 0.02

    def test_decay_reaches_near_zero(self):
        """持续衰减趋近于 0"""
        manager = _make_manager_with_regions(decay_factor=0.92)
        manager._regions["Python脑区"].activation = 1.0

        # 0.92^85 ≈ 0.0008, below 0.001 clamp threshold
        for _ in range(85):
            manager.decay_all()

        # Should be clamped to 0.0
        assert manager._regions["Python脑区"].activation == 0.0


# ============== Test 5: spillover activation ==============


class TestSpilloverActivation:
    """验证溢出激活 — 邻居区域获得 0.3 × activation"""

    def test_spillover_to_neighbors(self):
        """激活区域时邻居获得 spillover_factor × activation"""
        neighbor_map = {
            "community_0": {"community_1"},  # Python -> React
        }
        manager = _make_manager_with_regions(
            spillover_factor=0.3,
            neighbor_map=neighbor_map,
        )

        # Activate Python region
        manager.activate_regions(
            hit_entities=["Python"],
            entity_to_region={},
        )

        # React (neighbor) should get 0.3 × 1.0 = 0.3
        assert manager._regions["React脑区"].activation == 0.3

    def test_spillover_does_not_reduce_higher_activation(self):
        """溢出不降低邻居已有的更高激活值"""
        neighbor_map = {
            "community_0": {"community_1"},
        }
        manager = _make_manager_with_regions(
            spillover_factor=0.3,
            neighbor_map=neighbor_map,
        )

        # Set React region to high activation first
        manager._regions["React脑区"].activation = 0.8

        # Activate Python region (spillover = 0.3 × 1.0 = 0.3)
        manager.activate_regions(
            hit_entities=["Python"],
            entity_to_region={},
        )

        # React should keep its 0.8 (spillover 0.3 < 0.8)
        assert manager._regions["React脑区"].activation == 0.8

    def test_spillover_multiple_neighbors(self):
        """溢出传播到多个邻居"""
        neighbor_map = {
            "community_0": {"community_1", "community_2"},
        }
        manager = _make_manager_with_regions(
            spillover_factor=0.3,
            neighbor_map=neighbor_map,
        )

        manager.activate_regions(
            hit_entities=["Python"],
            entity_to_region={},
        )

        # Both neighbors get 0.3 × 1.0 = 0.3
        assert manager._regions["React脑区"].activation == 0.3
        assert manager._regions["Database脑区"].activation == 0.3

    def test_no_spillover_without_neighbors(self):
        """无邻居关系时不发生溢出"""
        manager = _make_manager_with_regions(
            spillover_factor=0.3,
            neighbor_map={},
        )

        manager.activate_regions(
            hit_entities=["Python"],
            entity_to_region={},
        )

        # community_1 and community_2 should stay at 0.0
        assert manager._regions["React脑区"].activation == 0.0
        assert manager._regions["Database脑区"].activation == 0.0

    def test_spillover_skips_manually_dimmed_neighbor(self):
        """溢出跳过手动调暗的邻居"""
        neighbor_map = {
            "community_0": {"community_1"},
        }
        manager = _make_manager_with_regions(
            spillover_factor=0.3,
            neighbor_map=neighbor_map,
        )

        # Manually dim React region
        manager.manual_dim(["React"])

        # Activate Python region
        manager.activate_regions(
            hit_entities=["Python"],
            entity_to_region={},
        )

        # React should stay dimmed (spillover skipped)
        assert manager._regions["React脑区"].activation == 0.0
        assert manager._regions["React脑区"].manually_dimmed is True


# ============== Test 6: manual_dim blocks auto-activation ==============


class TestManualDimBlocksAuto:
    """验证手动调暗阻止自动激活"""

    def test_manual_dim_blocks_auto_activation(self):
        """手动调暗的区域在该轮次中不会被自动激活"""
        manager = _make_manager_with_regions()

        # Manually dim Python region
        manager.manual_dim(["Python"])

        # Try to auto-activate it via hit entities
        activated = manager.activate_regions(
            hit_entities=["Python", "Django"],
            entity_to_region={},
        )

        # "Python脑区" should NOT be activated
        assert "Python脑区" not in activated
        assert manager._regions["Python脑区"].activation == 0.0
        assert manager._regions["Python脑区"].manually_dimmed is True

    def test_manual_dim_blocks_tool_reinforce(self):
        """手动调暗的区域在该轮次中不会被工具强化"""
        manager = _make_manager_with_regions()

        manager.manual_dim(["Python"])

        result = manager.reinforce_by_tool_use(
            tool_name="kg-server/query",
            tool_to_region={"kg-server/query": "Python脑区"},
        )

        # Reinforce should be skipped
        assert result is None
        assert manager._regions["Python脑区"].activation == 0.0

    def test_manual_activate_overrides_dim(self):
        """手动激活可以覆盖调暗状态"""
        manager = _make_manager_with_regions()

        # First dim, then activate
        manager.manual_dim(["Python"])
        assert manager._regions["Python脑区"].manually_dimmed is True

        manager.manual_activate(["Python"])
        assert manager._regions["Python脑区"].activation == 1.0
        assert manager._regions["Python脑区"].manually_dimmed is False


# ============== Test 7: decay clears manually_dimmed ==============


class TestDecayClearsManuallyDimmed:
    """验证 decay_all 清除 manually_dimmed 标记"""

    def test_decay_clears_manually_dimmed(self):
        """衰减后 manually_dimmed 标记被清除，下轮可自动激活"""
        manager = _make_manager_with_regions()

        # Dim a region
        manager.manual_dim(["Python"])
        assert manager._regions["Python脑区"].manually_dimmed is True

        # Decay (end of turn)
        manager.decay_all()
        assert manager._regions["Python脑区"].manually_dimmed is False

        # Next turn: auto-activation should work
        activated = manager.activate_regions(
            hit_entities=["Python"],
            entity_to_region={},
        )
        assert "Python脑区" in activated
        assert manager._regions["Python脑区"].activation == 1.0

    def test_decay_clears_all_manually_dimmed_flags(self):
        """衰减清除所有区域的 manually_dimmed 标记"""
        manager = _make_manager_with_regions()

        # Dim two regions
        manager.manual_dim(["Python", "React"])
        assert manager._regions["Python脑区"].manually_dimmed is True
        assert manager._regions["React脑区"].manually_dimmed is True

        manager.decay_all()

        # Both should be cleared
        assert manager._regions["Python脑区"].manually_dimmed is False
        assert manager._regions["React脑区"].manually_dimmed is False


# ============== Test 8: get_status_light ==============


class TestGetStatusLight:
    """验证三状态灯 🟢🟡⚫"""

    def test_lit_for_high_activation(self):
        """activation > 0.7 返回 🟢"""
        manager = _make_manager_with_regions()

        assert manager.get_status_light(1.0) == STATUS_LIT
        assert manager.get_status_light(0.85) == STATUS_LIT
        assert manager.get_status_light(0.71) == STATUS_LIT

    def test_dimming_for_medium_activation(self):
        """0.3 < activation <= 0.7 返回 🟡"""
        manager = _make_manager_with_regions()

        assert manager.get_status_light(0.7) == STATUS_DIMMING
        assert manager.get_status_light(0.5) == STATUS_DIMMING
        assert manager.get_status_light(0.31) == STATUS_DIMMING

    def test_off_for_low_activation(self):
        """activation <= 0.1 返回 ⚫"""
        manager = _make_manager_with_regions()

        assert manager.get_status_light(0.1) == STATUS_OFF
        assert manager.get_status_light(0.05) == STATUS_OFF
        assert manager.get_status_light(0.0) == STATUS_OFF

    def test_boundary_values(self):
        """边界值精确匹配"""
        manager = _make_manager_with_regions()

        # Exactly 0.7 is NOT lit (>0.7), so it's dimming
        assert manager.get_status_light(0.7) == STATUS_DIMMING
        # Exactly 0.1 is NOT dimming (>0.1), so it's off
        assert manager.get_status_light(0.1) == STATUS_OFF