"""
脑区边衰减增强机制测试

真实测试：需要程序运行 + 真实 LLM。
手动执行：python -m pytest tests/test_brain_region_edge_decay.py -v

单元测试部分可直接运行：python -m pytest tests/test_brain_region_edge_decay.py -v -k "not integration"
"""
import pytest


class TestPriorityConstants:
    """优先级常量和日衰减率计算"""

    def test_priority_halflife_defined(self):
        from niu_api.internal.region_manager import PRIORITY_HALFLIFE
        assert "permanent" in PRIORITY_HALFLIFE
        assert "long" in PRIORITY_HALFLIFE
        assert "medium" in PRIORITY_HALFLIFE
        assert "short" in PRIORITY_HALFLIFE

    def test_priority_halflife_values(self):
        from niu_api.internal.region_manager import PRIORITY_HALFLIFE
        assert PRIORITY_HALFLIFE["permanent"] == 360
        assert PRIORITY_HALFLIFE["long"] == 360
        assert PRIORITY_HALFLIFE["medium"] == 180
        assert PRIORITY_HALFLIFE["short"] == 90

    def test_floor_and_initial_weight(self):
        from niu_api.internal.region_manager import FLOOR_WEIGHT, INITIAL_WEIGHT
        assert FLOOR_WEIGHT == 0.1
        assert INITIAL_WEIGHT == 1.0

    def test_default_priority(self):
        from niu_api.internal.region_manager import DEFAULT_PRIORITY
        assert DEFAULT_PRIORITY == "medium"

    def test_daily_decay_calculation(self):
        from niu_api.internal.region_manager import daily_decay_rate
        # 360天半衰期
        rate_360 = daily_decay_rate("permanent")
        assert rate_360 == pytest.approx(0.5 ** (1/360), rel=1e-6)
        assert rate_360 == pytest.approx(0.99808, rel=0.001)

        # 180天半衰期
        rate_180 = daily_decay_rate("medium")
        assert rate_180 == pytest.approx(0.5 ** (1/180), rel=1e-6)

        # 90天半衰期
        rate_90 = daily_decay_rate("short")
        assert rate_90 == pytest.approx(0.5 ** (1/90), rel=1e-6)

        # long 和 permanent 半衰期相同
        assert daily_decay_rate("long") == daily_decay_rate("permanent")

    def test_daily_decay_unknown_priority(self):
        from niu_api.internal.region_manager import daily_decay_rate
        # 未知优先级回退到 medium
        rate = daily_decay_rate("unknown_priority")
        assert rate == daily_decay_rate("medium")


class TestEncodeDescriptionPriority:
    """_encode_description 的 priority 字段写入和解析"""

    def test_encode_description_includes_priority(self):
        from niu_api.internal.region_manager import _encode_description
        desc = _encode_description(
            summary="测试摘要",
            region_id="community_1",
            size=5,
            representative="代表实体",
            updated_at=1000000,
            priority="permanent",
        )
        assert "brain_meta_priority:permanent" in desc

    def test_encode_description_default_priority(self):
        from niu_api.internal.region_manager import _encode_description, DEFAULT_PRIORITY
        desc = _encode_description(
            summary="测试摘要",
            region_id="community_1",
            size=5,
            representative="代表实体",
            updated_at=1000000,
            priority=DEFAULT_PRIORITY,
        )
        assert "brain_meta_priority:medium" in desc

    def test_parse_priority_from_description(self):
        from niu_api.internal.region_manager import parse_priority_from_description
        desc = "brain_meta_priority:long<SEP>brain_meta_source:default<SEP>..."
        assert parse_priority_from_description(desc) == "long"

    def test_parse_priority_missing(self):
        from niu_api.internal.region_manager import parse_priority_from_description, DEFAULT_PRIORITY
        desc = "brain_meta_source:default<SEP>some other content"
        assert parse_priority_from_description(desc) == DEFAULT_PRIORITY

    def test_parse_priority_empty(self):
        from niu_api.internal.region_manager import parse_priority_from_description, DEFAULT_PRIORITY
        assert parse_priority_from_description("") == DEFAULT_PRIORITY

    def test_parse_priority_old_core_value_warning(self):
        """旧优先级值 core/category 应回退到 DEFAULT_PRIORITY"""
        from niu_api.internal.region_manager import parse_priority_from_description, DEFAULT_PRIORITY
        desc = "brain_meta_priority:core<SEP>brain_meta_source:default"
        assert parse_priority_from_description(desc) == DEFAULT_PRIORITY
