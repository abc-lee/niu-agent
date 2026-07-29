"""
脑区边衰减增强机制测试

单元测试：python -m pytest tests/test_brain_region_edge_decay.py -v
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
        from niu_api.internal.region_manager import DEFAULT_PRIORITY, _encode_description
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
        from niu_api.internal.region_manager import (
            DEFAULT_PRIORITY,
            parse_priority_from_description,
        )
        desc = "brain_meta_source:default<SEP>some other content"
        assert parse_priority_from_description(desc) == DEFAULT_PRIORITY

    def test_parse_priority_empty(self):
        from niu_api.internal.region_manager import (
            DEFAULT_PRIORITY,
            parse_priority_from_description,
        )
        assert parse_priority_from_description("") == DEFAULT_PRIORITY

    def test_parse_priority_old_core_value_warning(self):
        """旧优先级值 core/category 应回退到 DEFAULT_PRIORITY"""
        from niu_api.internal.region_manager import (
            DEFAULT_PRIORITY,
            parse_priority_from_description,
        )
        desc = "brain_meta_priority:core<SEP>brain_meta_source:default"
        assert parse_priority_from_description(desc) == DEFAULT_PRIORITY


import networkx as nx  # noqa: E402


class TestDecayStructuralEdges:
    """衰减算法测试 — 使用内存 NetworkX 图"""

    def _build_test_graph(self):
        """构建测试用图：2个脑区 + 3个实体"""
        g = nx.Graph()
        # 脑区节点
        g.add_node("region_permanent", entity_type="brainregion",
                   description="brain_meta_priority:permanent<SEP>brain_meta_source:default<SEP>永久脑区")
        g.add_node("region_short", entity_type="brainregion",
                   description="brain_meta_priority:short<SEP>brain_meta_source:default<SEP>短期脑区")
        # 实体节点
        g.add_node("entity_a", entity_type="person", description="人物A")
        g.add_node("entity_b", entity_type="skill", description="技能B")
        g.add_node("entity_c", entity_type="topic", description="话题C")
        # 脑区边（权重1.0）
        g.add_edge("region_permanent", "entity_a", weight=1.0, description="包含")
        g.add_edge("region_short", "entity_a", weight=1.0, description="包含")
        g.add_edge("region_short", "entity_b", weight=1.0, description="包含")
        # 知识关系边（不应被衰减）
        g.add_edge("entity_a", "entity_c", weight=1.0, description="讨论")
        return g

    def test_decay_short_priority(self):
        """short 级（90天半衰期）边权重衰减"""
        from niu_api.internal.region_manager import _decay_brain_region_edges, daily_decay_rate
        g = self._build_test_graph()
        _decay_brain_region_edges(g)
        # entity_b 只有1条脑区边 + 0条知识边 = 总边数1 → 保底
        weight_b = g["region_short"]["entity_b"]["weight"]
        expected = 1.0 * daily_decay_rate("short")
        assert weight_b == pytest.approx(max(expected, 0.1), rel=1e-6)

    def test_permanent_isolated_entity_floor_protection(self):
        """permanent 脑区 + 孤立实体（total_degree=1）→ 保底保护

        注意：本测试验证的是"孤立实体保底"分支（total_degree<=1），
        与 priority 无关——medium/short 脑区同样会保底。用 permanent
        脑区构造场景是为了对照"修复前 permanent 走专属永久保底分支，
        修复后走统一的孤立保底分支"。
        """
        from niu_api.internal.region_manager import FLOOR_WEIGHT, _decay_brain_region_edges
        g = nx.Graph()
        g.add_node("region_perm", entity_type="brainregion",
                   description="brain_meta_priority:permanent<SEP>永久脑区")
        g.add_node("entity_x", entity_type="person", description="人物X")
        g.add_edge("region_perm", "entity_x", weight=0.11, description="包含")
        _decay_brain_region_edges(g)
        weight = g["region_perm"]["entity_x"]["weight"]
        # 孤立实体保底：weight 衰减后 max(., FLOOR_WEIGHT)，仍 >= FLOOR_WEIGHT
        assert weight >= FLOOR_WEIGHT, f"孤立实体保底后 weight 应 >= FLOOR_WEIGHT，实际 {weight}"

    def test_floor_protection_orphan(self):
        """总边数==1时保底保护"""
        from niu_api.internal.region_manager import FLOOR_WEIGHT, _decay_brain_region_edges
        g = nx.Graph()
        g.add_node("region_short", entity_type="brainregion",
                   description="brain_meta_priority:short<SEP>短期脑区")
        g.add_node("entity_lonely", entity_type="topic", description="孤独话题")
        g.add_edge("region_short", "entity_lonely", weight=0.05, description="包含")
        _decay_brain_region_edges(g)
        weight = g["region_short"]["entity_lonely"]["weight"]
        assert weight >= FLOOR_WEIGHT

    def test_delete_below_floor_with_other_edges(self):
        """非 permanent + 总边数>=2 + 低于保底 → 删除边"""
        from niu_api.internal.region_manager import (
            FLOOR_WEIGHT,
            _decay_brain_region_edges,
            daily_decay_rate,
        )
        g = nx.Graph()
        g.add_node("region_short", entity_type="brainregion",
                   description="brain_meta_priority:short<SEP>短期脑区")
        g.add_node("entity_multi", entity_type="person", description="多边人物")
        g.add_node("entity_other", entity_type="skill", description="其他技能")
        g.add_edge("region_short", "entity_multi", weight=0.03, description="包含")
        g.add_edge("entity_multi", "entity_other", weight=1.0, description="擅长")
        # 前提：0.03 * decay_rate("short") < FLOOR_WEIGHT，确认进入删除分支
        assert 0.03 * daily_decay_rate("short") < FLOOR_WEIGHT
        _decay_brain_region_edges(g)
        assert not g.has_edge("region_short", "entity_multi")

    def test_permanent_not_deleted_with_other_edges(self):
        """permanent + 总边数>=2 + 低于保底 → 删除（2026-07-18 修复：与普通脑区一致）"""
        from niu_api.internal.region_manager import (
            FLOOR_WEIGHT,
            _decay_brain_region_edges,
            daily_decay_rate,
        )
        g = nx.Graph()
        g.add_node("region_perm", entity_type="brainregion",
                   description="brain_meta_priority:permanent<SEP>永久脑区")
        g.add_node("entity_multi", entity_type="person", description="多边人物")
        g.add_node("entity_other", entity_type="skill", description="其他技能")
        g.add_edge("region_perm", "entity_multi", weight=0.03, description="包含")
        g.add_edge("entity_multi", "entity_other", weight=1.0, description="擅长")
        # 前提：0.03 * decay_rate("permanent") < FLOOR_WEIGHT，确认进入删除分支
        assert 0.03 * daily_decay_rate("permanent") < FLOOR_WEIGHT
        _decay_brain_region_edges(g)
        # 永久脑区的实体归属边与普通脑区一致：weight < FLOOR_WEIGHT + total_degree >= 2 → 删除
        assert not g.has_edge("region_perm", "entity_multi")

    def test_knowledge_edge_not_decayed(self):
        """知识关系边（实体→实体）不被衰减"""
        from niu_api.internal.region_manager import _decay_brain_region_edges
        g = self._build_test_graph()
        _decay_brain_region_edges(g)
        weight = g["entity_a"]["entity_c"]["weight"]
        assert weight == 1.0

    def test_anchor_edge_not_decayed(self):
        """脑区之间的锚点边不被衰减"""
        from niu_api.internal.region_manager import _decay_brain_region_edges
        g = nx.Graph()
        g.add_node("Niu", entity_type="brainregion", description="根节点")
        g.add_node("region_perm", entity_type="brainregion",
                   description="brain_meta_priority:permanent<SEP>永久脑区")
        g.add_edge("Niu", "region_perm", weight=0.5, description="锚点")
        _decay_brain_region_edges(g)
        weight = g["Niu"]["region_perm"]["weight"]
        assert weight == 0.5

    def test_missing_priority_fallback(self):
        """description 中缺少 brain_meta_priority 时回退到 medium"""
        from niu_api.internal.region_manager import (
            DEFAULT_PRIORITY,
            _decay_brain_region_edges,
            daily_decay_rate,
        )
        g = nx.Graph()
        g.add_node("region_no_priority", entity_type="brainregion",
                   description="brain_meta_source:leiden<SEP>无优先级脑区")
        g.add_node("entity_y", entity_type="topic", description="话题Y")
        g.add_node("entity_z", entity_type="skill", description="技能Z")
        g.add_edge("region_no_priority", "entity_y", weight=1.0, description="包含")
        g.add_edge("entity_y", "entity_z", weight=1.0, description="相关")
        _decay_brain_region_edges(g)
        expected = 1.0 * daily_decay_rate(DEFAULT_PRIORITY)
        weight = g["region_no_priority"]["entity_y"]["weight"]
        assert weight == pytest.approx(expected, rel=1e-6)

    def test_empty_graph_safe(self):
        """空图不会报错"""
        from niu_api.internal.region_manager import _decay_brain_region_edges
        g = nx.Graph()
        _decay_brain_region_edges(g)

    def test_session_edge_not_decayed(self):
        """_session: 前缀边不被衰减"""
        from niu_api.internal.region_manager import _decay_brain_region_edges
        g = nx.Graph()
        g.add_node("region_short", entity_type="brainregion",
                   description="brain_meta_priority:short<SEP>短期脑区")
        g.add_node("entity_a", entity_type="person", description="人物A")
        g.add_node("entity_b", entity_type="topic", description="话题B")
        g.add_edge("region_short", "entity_a", weight=1.0, description="包含")
        g.add_edge("region_short", "entity_b", weight=1.0, keywords="_session:xyz")
        _decay_brain_region_edges(g)
        weight_a = g["region_short"]["entity_a"]["weight"]
        assert weight_a < 1.0
        weight_b = g["region_short"]["entity_b"]["weight"]
        assert weight_b == 1.0


class TestReinforceEdgeWeight:
    """增强算法测试 — 使用独立函数 _reinforce_brain_region_edges"""

    def _build_test_graph(self):
        g = nx.Graph()
        g.add_node("region_permanent", entity_type="brainregion",
                   description="brain_meta_priority:permanent<SEP>永久脑区")
        g.add_node("region_short", entity_type="brainregion",
                   description="brain_meta_priority:short<SEP>短期脑区")
        g.add_node("entity_a", entity_type="person", description="人物A")
        g.add_node("Niu", entity_type="brainregion", description="根节点")
        # 衰减后的边
        g.add_edge("region_permanent", "entity_a", weight=0.3, description="包含")
        g.add_edge("region_short", "entity_a", weight=0.2, description="包含")
        # 锚点边（不应增强）
        g.add_edge("Niu", "region_permanent", weight=0.5, description="锚点")
        return g

    def test_reinforce_restores_to_initial_weight(self):
        """增强将权重恢复到 INITIAL_WEIGHT (1.0)"""
        from agent.brain_tools import _reinforce_brain_region_edges
        from niu_api.internal.region_manager import INITIAL_WEIGHT
        g = self._build_test_graph()
        _reinforce_brain_region_edges(g, "region_permanent")
        weight = g["region_permanent"]["entity_a"]["weight"]
        assert weight == INITIAL_WEIGHT

    def test_reinforce_skips_anchor_edges(self):
        """增强跳过锚点边（脑区→脑区）"""
        from agent.brain_tools import _reinforce_brain_region_edges
        g = self._build_test_graph()
        _reinforce_brain_region_edges(g, "region_permanent")
        weight = g["Niu"]["region_permanent"]["weight"]
        assert weight == 0.5

    def test_reinforce_only_target_region(self):
        """增强只影响目标脑区的边，不影响其他脑区"""
        from agent.brain_tools import _reinforce_brain_region_edges
        g = self._build_test_graph()
        _reinforce_brain_region_edges(g, "region_permanent")
        weight = g["region_short"]["entity_a"]["weight"]
        assert weight == 0.2

    def test_reinforce_no_brainregion_neighbors(self):
        """脑区没有实体邻居时安全返回"""
        from agent.brain_tools import _reinforce_brain_region_edges
        g = nx.Graph()
        g.add_node("region_empty", entity_type="brainregion",
                   description="brain_meta_priority:short<SEP>空脑区")
        _reinforce_brain_region_edges(g, "region_empty")

    def test_reinforce_skips_session_edges(self):
        """增强跳过 _session: 前缀边（会话临时边）"""
        from agent.brain_tools import _reinforce_brain_region_edges
        g = nx.Graph()
        g.add_node("region_short", entity_type="brainregion",
                   description="brain_meta_priority:short<SEP>短期脑区")
        g.add_node("entity_a", entity_type="person", description="人物A")
        g.add_node("entity_b", entity_type="topic", description="话题B")
        g.add_edge("region_short", "entity_a", weight=0.3, description="包含")
        g.add_edge("region_short", "entity_b", weight=0.3, keywords="_session:xyz")
        _reinforce_brain_region_edges(g, "region_short")
        assert g["region_short"]["entity_a"]["weight"] == 1.0
        assert g["region_short"]["entity_b"]["weight"] == 0.3
