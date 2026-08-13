"""
Tests for niu_api/internal/region_injector.py

Brain Context Injector 测试 — 验证 BrainContextInjector 的
区域地图格式、脑区点亮数量软控制、format_region_map_only、
和主入口返回格式化文本。
"""


from unittest.mock import MagicMock

import pytest

from niu_api.internal.region_activation import (
    STATUS_DIMMING,
    STATUS_LIT,
    STATUS_OFF,
    BrainRegionState,
    RegionActivationManager,
)
from niu_api.internal.region_injector import BrainContextInjector
from niu_api.internal.region_manager import BrainRegionInfo, RegionManager

# ============== 辅助函数 ==============


def _make_region_infos() -> list[BrainRegionInfo]:
    """创建测试用的 BrainRegionInfo 列表"""
    return [
        BrainRegionInfo(
            name="编程开发脑区",
            label="编程开发",
            community_id="community_0",
            description="Python/NumPy/Web技术栈",
            size=6,
            representative="Python",
            members=["Python", "NumPy", "Data_Analysis", "Web_Development", "Django", "FastAPI"],
            updated_at=1745366400.0,
        ),
        BrainRegionInfo(
            name="项目管理脑区",
            label="项目管理",
            community_id="community_1",
            description="AI_Bot项目，主开发者",
            size=4,
            representative="AI_Bot",
            members=["AI_Bot", "Project_Plan", "Sprint", "Backlog"],
            updated_at=1745366400.0,
        ),
        BrainRegionInfo(
            name="日常偏好脑区",
            label="日常偏好",
            community_id="community_2",
            description="暗色主题，远程办公",
            size=3,
            representative="暗色主题",
            members=["暗色主题", "远程办公", "MacOS"],
            updated_at=1745366400.0,
        ),
        BrainRegionInfo(
            name="财务知识脑区",
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
        # Set different activation levels using region name as key
        # (region_id = region.name, e.g. "编程开发脑区", not community_id)
        _set_activation(activation_mgr, "编程开发脑区", 1.0)  # lit
        _set_activation(activation_mgr, "项目管理脑区", 0.5)  # dimming
        _set_activation(activation_mgr, "日常偏好脑区", 0.2)  # off (< 0.3)
        _set_activation(activation_mgr, "财务知识脑区", 0.0)  # off

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
        lit_indices = [i for i, line in enumerate(lines) if STATUS_LIT in line]
        dimming_indices = [i for i, line in enumerate(lines) if STATUS_DIMMING in line]
        off_indices = [i for i, line in enumerate(lines) if STATUS_OFF in line]

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
                label=f"测试脑区{i}", activation=0.8,
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
                label=f"测试脑区{i}", activation=0.8,
                last_activated_at=0, activation_count=1, manually_dimmed=False,
            )
        injector = _make_injector(activation_mgr)

        result = injector.format_region_map_only()

        assert "建议关闭" not in result

    def test_format_region_map_no_warn_at_seven_yellow(self):
        """T2-5: 7个黄灯（0.5）不算点亮——lit_count 口径 >0.7（与 🟢 一致）"""
        activation_mgr = RegionActivationManager()
        for i in range(7):
            activation_mgr._regions[f"region_{i}"] = BrainRegionState(
                region_id=f"region_{i}", community_id="",
                label=f"黄灯脑区{i}", activation=0.5,
                last_activated_at=0, activation_count=1, manually_dimmed=False,
            )
        injector = _make_injector(activation_mgr)

        result = injector.format_region_map_only()

        # 旧口径 >0.3 下 7 个黄灯虚警"点亮"→ 断言失败（红）
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


# ============== Bug 1: format_region_map_only 差集过滤已删脑区 ==============


class TestFormatRegionMapOnlyFiltersStaleCache:
    """Bug 1: format_region_map_only 应过滤缓存中已删脑区并主动清理缓存

    场景：activation_mgr 缓存有 A脑区 + B脑区，但图中 B 已被删（list_entities
    只返回 A）。format_region_map_only 应：
    1) 返回结果不含 B脑区
    2) 主动调 activation_mgr.remove_region("B脑区") 清理缓存
    """

    def test_format_region_map_only_filters_stale_cache(self):
        """缓存有 Ghost 但图没有 Ghost 时，返回结果不含 Ghost 且主动 remove_region"""
        activation_mgr = _make_activation_manager()
        # 添加一个"幽灵脑区"（缓存有，图已删）
        activation_mgr._regions["Ghost脑区"] = BrainRegionState(
            region_id="Ghost脑区",
            community_id="community_ghost",
            label="Ghost",
            activation=1.0,
            last_activated_at=0,
            activation_count=1,
            manually_dimmed=False,
        )

        # mock adapter.list_entities 返回的图只含 4 个预置脑区（Ghost 已删）
        adapter = MagicMock()
        adapter.list_entities.return_value = {
            "status": "ok",
            "data": [
                {"id": "编程开发脑区", "entity_type": "BrainRegion", "description": ""},
                {"id": "项目管理脑区", "entity_type": "BrainRegion", "description": ""},
                {"id": "日常偏好脑区", "entity_type": "BrainRegion", "description": ""},
                {"id": "财务知识脑区", "entity_type": "BrainRegion", "description": ""},
                # 注意：没有 Ghost脑区（已被删除）
            ],
        }
        region_mgr = MagicMock(spec=RegionManager)
        injector = BrainContextInjector(
            adapter=adapter,
            activation_mgr=activation_mgr,
            region_mgr=region_mgr,
        )

        result = injector.format_region_map_only()

        # 断言1：返回结果不含 Ghost
        assert "Ghost" not in result
        # 断言2：缓存中 Ghost 已被主动清理
        assert "Ghost脑区" not in activation_mgr._regions, (
            "Ghost脑区应被 activation_mgr.remove_region 主动清理，但仍残留在缓存中"
        )


# ============== Test 5: activate_for_query 返回 region_entities ==============


def _make_region_info(
    name: str,
    label: str,
    members: list[str] | None = None,
) -> BrainRegionInfo:
    """创建指定名称/标签/成员的 BrainRegionInfo"""
    members = members or []
    return BrainRegionInfo(
        name=name,
        label=label,
        community_id="community_x",
        description=f"{label}描述",
        size=len(members),
        representative=members[0] if members else "",
        members=members,
        updated_at=1745366400.0,
    )


# 与 _classify_entity_to_region 默认分类一致的 6 个脑区名
_DEFAULT_CLASSIFY_REGIONS = [
    ("聊天历史脑区", "聊天历史"),
    ("文档库脑区", "文档库"),
    ("人际关系脑区", "人际关系"),
    ("工作事务脑区", "工作事务"),
    ("生活事务脑区", "生活事务"),
    ("知识体系脑区", "知识体系"),
]


def _make_classify_manager() -> RegionActivationManager:
    """创建含 6 个默认分类脑区的激活管理器（_classify 结果都能命中真实脑区）"""
    manager = RegionActivationManager()
    manager.initialize_from_regions(
        [_make_region_info(name, label) for name, label in _DEFAULT_CLASSIFY_REGIONS]
    )
    return manager


class TestActivateForQueryRegionEntities:
    """T2-1: activate_for_query 返回 region_entities（region → 命中实体 dict 列表）"""

    def test_activate_for_query_returns_region_entities(self):
        """返回结构：region_entities["知识体系脑区"] 为 2 条 dict，含三键"""
        activation_mgr = _make_classify_manager()
        injector = _make_injector(activation_mgr)
        injector._adapter.query_data = MagicMock(return_value={
            "data": {"entities": [
                {"entity_name": "python_sk", "entity_type": "skill", "description": "Python 技能描述"},
                {"entity_name": "numpy_sk", "entity_type": "skill", "description": "NumPy 技能描述"},
            ]}
        })

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "niu_api.internal.region_injector.get_all_region_members",
                lambda: {},
            )
            region_entities, _, _ = injector.activate_for_query("Python数据分析")

        # 结构断言（非仅数量）：2 条且每条 dict 含 entity_name/entity_type/description 键
        assert len(region_entities["知识体系脑区"]) == 2
        for entity in region_entities["知识体系脑区"]:
            assert {"entity_name", "entity_type", "description"} <= set(entity)


# ============== Test 6: format_region_knowledge 分级格式化 ==============


class TestFormatRegionKnowledgeTiered:
    """T2-2/3/4: format_region_knowledge 分级（🟢 5 / 🟡 3 / ⚫ 0 + 缓存回退 + 合并更新）"""

    def test_green_current_hits_top5(self):
        """场景 A：🟢 本轮命中 → 输出本轮命中 top 5"""
        activation_mgr = _make_classify_manager()
        injector = _make_injector(activation_mgr)
        entities = [
            {"entity_name": f"skill{i}", "entity_type": "skill", "description": f"d{i}"}
            for i in range(6)
        ]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "niu_api.internal.region_injector.get_all_region_members",
                lambda: {},
            )
            injector._adapter.query_data = MagicMock(return_value={"data": {"entities": entities}})
            region_entities, _, _ = injector.activate_for_query("q")

        result = injector.format_region_knowledge(region_entities)

        # 命中置 1.0 → 🟢 → top 5，保持 query_data 相似度序
        assert len(result) == 5
        assert all(label.startswith("🟢") for label, *_ in result)
        assert [entry[1] for entry in result] == [f"skill{i}" for i in range(5)]

    def test_green_falls_back_to_cache(self):
        """场景 B：🟢 未命中轮 → 缓存回退 top 5（真实衰减时序：命中脑区多轮仍 🟢）"""
        activation_mgr = _make_classify_manager()
        injector = _make_injector(activation_mgr)
        entities = [
            {"entity_name": f"skill{i}", "entity_type": "skill", "description": f"d{i}"}
            for i in range(6)
        ]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "niu_api.internal.region_injector.get_all_region_members",
                lambda: {},
            )
            # 轮 1：命中
            injector._adapter.query_data = MagicMock(return_value={"data": {"entities": entities}})
            injector.activate_for_query("round1")
            # 轮间衰减：1.0 × 0.92 = 0.92 仍 🟢（lit=1 无加速）
            activation_mgr.decay_all()
            # 轮 2：未命中
            injector._adapter.query_data = MagicMock(return_value={"data": {"entities": []}})
            region_entities, _, _ = injector.activate_for_query("round2")

        assert region_entities == {}
        result = injector.format_region_knowledge(region_entities)

        # 🟢 未命中 → 缓存回退（非单调归零）
        assert len(result) == 5
        assert all(label.startswith("🟢") for label, *_ in result)

    def test_yellow_uses_cache_top3_and_off_skipped(self):
        """场景 C：🟡 输出缓存 top 3（3 ≠ 5 区分分级）；⚫ 输出 0 条"""
        activation_mgr = _make_classify_manager()
        injector = _make_injector(activation_mgr)
        entities = [
            {"entity_name": f"skill{i}", "entity_type": "skill", "description": f"d{i}"}
            for i in range(6)
        ]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "niu_api.internal.region_injector.get_all_region_members",
                lambda: {},
            )
            injector._adapter.query_data = MagicMock(return_value={"data": {"entities": entities}})
            injector.activate_for_query("round1")

        # 手动调至 🟡（0.5）
        _set_activation(activation_mgr, "知识体系脑区", 0.5)
        result = injector.format_region_knowledge({})

        assert len(result) == 3
        assert all(label.startswith("🟡") for label, *_ in result)

        # ⚫（0.0）→ 0 条
        _set_activation(activation_mgr, "知识体系脑区", 0.0)
        result_off = injector.format_region_knowledge({})
        assert result_off == []

    def test_merge_update_preserves_unhit_region_cache(self):
        """场景 D：合并更新——轮 2 仅 Y 命中时，未命中脑区 X 旧缓存保留"""
        activation_mgr = _make_classify_manager()
        injector = _make_injector(activation_mgr)
        x_entities = [
            {"entity_name": f"skill{i}", "entity_type": "skill", "description": f"d{i}"}
            for i in range(5)
        ]
        y_entities = [
            {"entity_name": f"doc{i}", "entity_type": "document", "description": f"dd{i}"}
            for i in range(3)
        ]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "niu_api.internal.region_injector.get_all_region_members",
                lambda: {},
            )
            # 轮 1：X（知识体系脑区）命中
            injector._adapter.query_data = MagicMock(return_value={"data": {"entities": x_entities}})
            injector.activate_for_query("round1")
            # 轮 2：仅 Y（文档库脑区）命中——X 未命中
            injector._adapter.query_data = MagicMock(return_value={"data": {"entities": y_entities}})
            injector.activate_for_query("round2")

        # X 调至 🟡——合并更新只覆盖 Y，X 缓存存活
        _set_activation(activation_mgr, "知识体系脑区", 0.5)
        result = injector.format_region_knowledge({})

        x_entries = [entry for entry in result if entry[0].startswith("🟡")]
        assert len(x_entries) == 3
        assert [entry[1] for entry in x_entries] == [f"skill{i}" for i in range(3)]

    def test_hit_less_than_tier_limit(self):
        """T2-3: 命中不足——🟢 只有 2 条 → 注入 2 条（min）"""
        activation_mgr = _make_classify_manager()
        injector = _make_injector(activation_mgr)
        entities = [
            {"entity_name": f"skill{i}", "entity_type": "skill", "description": f"d{i}"}
            for i in range(2)
        ]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "niu_api.internal.region_injector.get_all_region_members",
                lambda: {},
            )
            injector._adapter.query_data = MagicMock(return_value={"data": {"entities": entities}})
            region_entities, _, _ = injector.activate_for_query("q")

        result = injector.format_region_knowledge(region_entities)
        assert len(result) == 2

    def test_empty_entities_and_cache_returns_empty(self):
        """T2-4: 空 region_entities + 空缓存 → []（含 1 个 🟡 无缓存脑区——跳过）"""
        activation_mgr = _make_classify_manager()
        # 🟡 无缓存脑区：0.5 但从未命中
        _set_activation(activation_mgr, "知识体系脑区", 0.5)
        injector = _make_injector(activation_mgr)

        result = injector.format_region_knowledge({})
        assert result == []


# ============== Test 7: 黑名单双过滤 ==============


class TestFormatRegionKnowledgeBlacklist:
    """T2-6: 黑名单双过滤——entity_type lower 归一化 + entity_name case-sensitive"""

    def test_blacklist_filters_entity_type_and_name(self):
        """Tool/BrainRegion（type 变体）与 handler.py 拦截；Handler.py（大小写变体）入档"""
        activation_mgr = _make_classify_manager()
        injector = _make_injector(activation_mgr)
        entities = [
            {"entity_name": "good_entity", "entity_type": "skill", "description": "正常实体"},
            {"entity_name": "tool_entity", "entity_type": "Tool", "description": "title case 工具"},
            {"entity_name": "brain_entity", "entity_type": "BrainRegion", "description": "脑区实体"},
            {"entity_name": "handler.py", "entity_type": "knowledge", "description": "黑名单精确名"},
            {"entity_name": "Handler.py", "entity_type": "knowledge", "description": "大小写变体"},
        ]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "niu_api.internal.region_injector.get_all_region_members",
                lambda: {},
            )
            injector._adapter.query_data = MagicMock(return_value={"data": {"entities": entities}})
            region_entities, _, _ = injector.activate_for_query("q")

        result = injector.format_region_knowledge(region_entities)
        names = [entry[1] for entry in result]

        assert "good_entity" in names
        # type 过滤 .lower() 归一化生效
        assert "tool_entity" not in names
        assert "brain_entity" not in names
        # name 黑名单：精确匹配拦截
        assert "handler.py" not in names
        # case-sensitive：大小写变体不匹配黑名单 → 入档
        assert "Handler.py" in names


# ============== Test 8: None-desc 归一化 ==============


class TestFormatRegionKnowledgeNoneDesc:
    """T2-7: description 字段缺失（None）→ 输出条目 desc 归一化为 ''"""

    def test_none_desc_normalized_to_empty_str(self):
        """契约：format_region_knowledge 输出 desc 恒为 str"""
        activation_mgr = _make_classify_manager()
        _set_activation(activation_mgr, "知识体系脑区", 1.0)
        injector = _make_injector(activation_mgr)

        region_entities = {
            "知识体系脑区": [
                {"entity_name": "e1", "entity_type": "knowledge", "description": None},
                {"entity_name": "e2", "entity_type": "knowledge"},
            ]
        }

        result = injector.format_region_knowledge(region_entities)

        assert len(result) == 2
        for entry in result:
            assert isinstance(entry[3], str)
            assert entry[3] == ""


# ============== Test 9: 全局条数上限 ==============


def _make_tiered_fixture(
    yellow_names: list[str],
    green_names: list[str],
) -> tuple[RegionActivationManager, dict[str, list[str]], list[dict], list[dict]]:
    """构造分级夹具：🟡 先插入（dict 保序）、🟢 后插入；各区域 5 条命中。

    Returns:
        (activation_mgr, members, yellow_entities, green_entities)
    """
    region_names = list(yellow_names) + list(green_names)
    infos = [
        _make_region_info(name, name, [f"{name}_m{i}" for i in range(5)])
        for name in region_names
    ]
    activation_mgr = RegionActivationManager()
    activation_mgr.initialize_from_regions(infos)

    members: dict[str, list[str]] = {}
    yellow_entities: list[dict] = []
    green_entities: list[dict] = []
    for name in yellow_names:
        members[name] = [f"{name}_m{i}" for i in range(5)]
        yellow_entities += [
            {"entity_name": f"{name}_m{i}", "entity_type": "knowledge", "description": f"d{i}"}
            for i in range(5)
        ]
    for name in green_names:
        members[name] = [f"{name}_m{i}" for i in range(5)]
        green_entities += [
            {"entity_name": f"{name}_m{i}", "entity_type": "knowledge", "description": f"d{i}"}
            for i in range(5)
        ]
    return activation_mgr, members, yellow_entities, green_entities


class TestFormatRegionKnowledgeCap:
    """T2-8/8b: 全局条数上限 _REGION_ENTRY_CAP=26（🟢 先序 + 逐条准入）"""

    def test_caps_at_26_green_first(self):
        """41 条候选（7🟢×5 + 2🟡×3）→ 截断为 26 且全 🟢（🟢 先序 + 逐条准入）"""
        yellow_names = ["黄灯脑区A", "黄灯脑区B"]
        green_names = [f"绿灯脑区{i}" for i in range(7)]
        activation_mgr, members, yellow_entities, green_entities = _make_tiered_fixture(
            yellow_names, green_names
        )
        injector = _make_injector(activation_mgr)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "niu_api.internal.region_injector.get_all_region_members",
                lambda: members,
            )
            # 轮 1：全部区域命中 → 🟡 缓存预置 + 全部置 1.0
            injector._adapter.query_data = MagicMock(
                return_value={"data": {"entities": yellow_entities + green_entities}}
            )
            injector.activate_for_query("round1")
            # 🟡 脑区调回 0.5（保持黄灯）
            for name in yellow_names:
                _set_activation(activation_mgr, name, 0.5)
            # 轮 2：仅 🟢 命中（🟡 不被命中置 1.0 覆盖）
            injector._adapter.query_data = MagicMock(
                return_value={"data": {"entities": green_entities}}
            )
            region_entities, _, _ = injector.activate_for_query("round2")

        result = injector.format_region_knowledge(region_entities)

        # 逐条准入：== 26 且全 🟢（无 🟡——🟢 先序占满额度）
        assert len(result) == 26
        assert all(label.startswith("🟢") for label, *_ in result)

    def test_exact_26_not_truncated(self):
        """恰好 26 条（4🟢×5 + 2🟡×3）→ 全量输出不截断（严格 `>` 准入）"""
        yellow_names = ["黄灯脑区A", "黄灯脑区B"]
        green_names = [f"绿灯脑区{i}" for i in range(4)]
        activation_mgr, members, yellow_entities, green_entities = _make_tiered_fixture(
            yellow_names, green_names
        )
        injector = _make_injector(activation_mgr)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "niu_api.internal.region_injector.get_all_region_members",
                lambda: members,
            )
            injector._adapter.query_data = MagicMock(
                return_value={"data": {"entities": yellow_entities + green_entities}}
            )
            injector.activate_for_query("round1")
            for name in yellow_names:
                _set_activation(activation_mgr, name, 0.5)
            injector._adapter.query_data = MagicMock(
                return_value={"data": {"entities": green_entities}}
            )
            region_entities, _, _ = injector.activate_for_query("round2")

        result = injector.format_region_knowledge(region_entities)

        assert len(result) == 26
        green_count = sum(1 for label, *_ in result if label.startswith("🟢"))
        yellow_count = sum(1 for label, *_ in result if label.startswith("🟡"))
        assert green_count == 20
        assert yellow_count == 6


# ============== Test 10: 精确阈值边界 ==============


class TestFormatRegionKnowledgeThresholdBoundary:
    """T2-9: 用边界值本身锁定 `>` 语义（0.7 → 🟡 top 3；0.3 → ⚫ 0 条）"""

    def test_exact_threshold_boundaries(self):
        """0.7 期望 3 条（非 5）；0.3 期望 0 条（非 3）——锁定严格 `>`"""
        activation_mgr = _make_classify_manager()
        injector = _make_injector(activation_mgr)
        entities = [
            {"entity_name": f"skill{i}", "entity_type": "skill", "description": f"d{i}"}
            for i in range(5)
        ]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "niu_api.internal.region_injector.get_all_region_members",
                lambda: {},
            )
            injector._adapter.query_data = MagicMock(return_value={"data": {"entities": entities}})
            region_entities, _, _ = injector.activate_for_query("q")

        # 0.7：`>0.7` 语义下是 🟡 → top 3（`>=0.7` 实现会出 🟢 5 条 → 红）
        _set_activation(activation_mgr, "知识体系脑区", 0.7)
        result_07 = injector.format_region_knowledge(region_entities)
        assert len(result_07) == 3
        assert all(label.startswith("🟡") for label, *_ in result_07)

        # 0.3：`>0.3` 语义下是 ⚫ → 0 条（`>=0.3` 实现会出 🟡 3 条 → 红）
        _set_activation(activation_mgr, "知识体系脑区", 0.3)
        result_03 = injector.format_region_knowledge(region_entities)
        assert result_03 == []


# ============== Test 11: clear_recent_region_entities ==============


class TestClearRecentRegionEntities:
    """T2-10: 会话边界清理——清空 _recent_region_entities（行为断言）"""

    def test_clear_recent_region_entities(self):
        """命中后缓存非空 → clear 后缓存为空 → 🟡 脑区 format 返回空"""
        activation_mgr = _make_classify_manager()
        injector = _make_injector(activation_mgr)
        entities = [
            {"entity_name": "skill0", "entity_type": "skill", "description": "d0"},
        ]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "niu_api.internal.region_injector.get_all_region_members",
                lambda: {},
            )
            injector._adapter.query_data = MagicMock(return_value={"data": {"entities": entities}})
            injector.activate_for_query("q")

        # 命中 → 缓存非空
        assert injector._recent_region_entities

        injector.clear_recent_region_entities()
        assert injector._recent_region_entities == {}

        # 🟡 脑区无缓存条目 → format 返回空
        _set_activation(activation_mgr, "知识体系脑区", 0.5)
        result = injector.format_region_knowledge({})
        assert result == []
