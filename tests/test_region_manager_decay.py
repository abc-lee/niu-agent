"""测试 _decay_brain_region_edges 永久脑区边与普通脑区一致衰减"""
import networkx as nx
from niu_api.internal.region_manager import _decay_brain_region_edges, FLOOR_WEIGHT


def _build_graph_with_permanent_region():
    """构造 1 个永久脑区 + 1 个普通实体（含多条知识边避免 total_degree<=1）"""
    g = nx.Graph()
    g.add_node("文档库脑区", entity_type="brainregion",
               description="brain_meta_priority:permanent<SEP>brain_meta_source:default<SEP>用户文档库")
    g.add_node("技术脑区", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_source:default<SEP>技术相关")
    g.add_node("实体A", entity_type="concept")
    g.add_node("实体B", entity_type="concept")
    g.add_node("实体C", entity_type="concept")
    g.add_edge("文档库脑区", "实体A", keywords="包含", weight=0.05)
    g.add_edge("技术脑区", "实体A", keywords="包含", weight=0.05)
    g.add_edge("实体A", "实体B", keywords="相关", weight=1.0)
    g.add_edge("实体A", "实体C", keywords="相关", weight=0.5)
    return g


def test_permanent_region_edge_deleted_when_below_floor():
    """永久脑区的实体归属边 weight < FLOOR_WEIGHT + total_degree >= 2 → 应被删除"""
    g = _build_graph_with_permanent_region()
    result = _decay_brain_region_edges(g)
    assert not g.has_edge("文档库脑区", "实体A"), "永久脑区归属边 weight<FLOOR_WEIGHT 应被删除"
    assert not g.has_edge("技术脑区", "实体A"), "普通脑区归属边 weight<FLOOR_WEIGHT 应被删除"
    assert result["deleted"] >= 2, f"应该删除 2 条边，实际 {result['deleted']}"


def test_permanent_region_edge_decayed_normally_when_above_floor():
    """永久脑区的实体归属边 weight > FLOOR_WEIGHT + total_degree >= 2 → 应正常衰减"""
    g = nx.Graph()
    g.add_node("文档库脑区", entity_type="brainregion",
               description="brain_meta_priority:permanent<SEP>brain_meta_source:default<SEP>用户文档库")
    g.add_node("实体A", entity_type="concept")
    g.add_node("实体B", entity_type="concept")
    g.add_edge("文档库脑区", "实体A", keywords="包含", weight=1.0)
    g.add_edge("实体A", "实体B", keywords="相关", weight=1.0)
    result = _decay_brain_region_edges(g)
    assert g.has_edge("文档库脑区", "实体A"), "weight > FLOOR_WEIGHT 的边不应被删除"
    new_w = g.edges["文档库脑区", "实体A"]["weight"]
    assert new_w < 1.0, f"应该正常衰减，weight 应 < 1.0，实际 {new_w}"
    assert new_w > FLOOR_WEIGHT, f"衰减后应仍 > FLOOR_WEIGHT，实际 {new_w}"
    assert result["decayed"] >= 1


def test_permanent_region_protected_when_isolated():
    """永久脑区的孤立实体（total_degree<=1）仍走保底保护分支"""
    g = nx.Graph()
    g.add_node("文档库脑区", entity_type="brainregion",
               description="brain_meta_priority:permanent<SEP>brain_meta_source:default<SEP>用户文档库")
    g.add_node("实体A", entity_type="concept")
    g.add_edge("文档库脑区", "实体A", keywords="包含", weight=0.05)
    result = _decay_brain_region_edges(g)
    assert g.has_edge("文档库脑区", "实体A"), "孤立实体保底边不应删除"
    assert g.edges["文档库脑区", "实体A"]["weight"] == FLOOR_WEIGHT
    assert result["protected"] >= 1


def test_statistics_no_permanent_specific_counter():
    """删除 permanent 分支后，result 字典不应有 permanent 专属计数器"""
    g = _build_graph_with_permanent_region()
    result = _decay_brain_region_edges(g)
    assert set(result.keys()) == {"decayed", "deleted", "protected", "skipped_anchor"}


def test_permanent_region_edge_decayed_when_weight_above_floor():
    """永久脑区边 weight 衰减后 > FLOOR_WEIGHT + total_degree >= 2 → 正常衰减"""
    from niu_api.internal.region_manager import _decay_brain_region_edges, FLOOR_WEIGHT
    g = nx.Graph()
    g.add_node("文档库脑区", entity_type="brainregion",
               description="brain_meta_priority:permanent<SEP>文档库")
    g.add_node("实体A", entity_type="concept")
    g.add_node("实体B", entity_type="concept")
    g.add_edge("文档库脑区", "实体A", weight=1.0, description="包含")
    g.add_edge("实体A", "实体B", weight=1.0, description="相关")
    result = _decay_brain_region_edges(g)
    assert g.has_edge("文档库脑区", "实体A"), "weight > FLOOR_WEIGHT 的边不应被删除"
    new_w = g["文档库脑区"]["实体A"]["weight"]
    assert new_w < 1.0, f"应该正常衰减，weight 应 < 1.0，实际 {new_w}"
    assert new_w > FLOOR_WEIGHT, f"衰减后应仍 > FLOOR_WEIGHT，实际 {new_w}"
    assert result["decayed"] >= 1


def test_permanent_region_edge_at_boundary_floor_value_decayed():
    """永久脑区边 weight 衰减后正好略大于 FLOOR_WEIGHT → 走 else 正常衰减分支"""
    from niu_api.internal.region_manager import _decay_brain_region_edges, FLOOR_WEIGHT
    from niu_api.internal.region_manager import daily_decay_rate
    decay_rate = daily_decay_rate("permanent")
    initial_weight = (FLOOR_WEIGHT + 0.001) / decay_rate
    g = nx.Graph()
    g.add_node("文档库脑区", entity_type="brainregion",
               description="brain_meta_priority:permanent<SEP>文档库")
    g.add_node("实体A", entity_type="concept")
    g.add_node("实体B", entity_type="concept")
    g.add_edge("文档库脑区", "实体A", weight=initial_weight, description="包含")
    g.add_edge("实体A", "实体B", weight=1.0, description="相关")
    result = _decay_brain_region_edges(g)
    assert g.has_edge("文档库脑区", "实体A"), "new_weight > FLOOR_WEIGHT 的边不应删除"
    actual_weight = g["文档库脑区"]["实体A"]["weight"]
    expected = FLOOR_WEIGHT + 0.001
    assert abs(actual_weight - expected) < 1e-9, \
        f"weight 应等于 new_weight={expected}，实际 {actual_weight}"
    assert result["decayed"] >= 1, "应进入 decayed 计数"
    assert result["deleted"] == 0, "不应进入 deleted 计数"


def test_permanent_decay_rate_still_uses_permanent_halflife():
    """永久脑区 decay_rate 仍用 PRIORITY_HALFLIFE['permanent']=360"""
    from niu_api.internal.region_manager import daily_decay_rate, PRIORITY_HALFLIFE
    assert PRIORITY_HALFLIFE["permanent"] == 360
    rate = daily_decay_rate("permanent")
    expected = 0.5 ** (1.0 / 360)
    assert abs(rate - expected) < 1e-9


def test_normal_region_edge_delete_contrast():
    """对照：普通脑区 weight < FLOOR_WEIGHT + total_degree >= 2 → 删除"""
    from niu_api.internal.region_manager import _decay_brain_region_edges
    g = nx.Graph()
    g.add_node("技术脑区", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>技术")
    g.add_node("实体A", entity_type="concept")
    g.add_node("实体B", entity_type="concept")
    g.add_edge("技术脑区", "实体A", weight=0.05, description="包含")
    g.add_edge("实体A", "实体B", weight=1.0, description="相关")
    _decay_brain_region_edges(g)
    assert not g.has_edge("技术脑区", "实体A"), "普通脑区 weight<FLOOR_WEIGHT 应删除"


def test_permanent_region_node_not_dissolved_when_empty():
    """永久脑区即使所有归属边删除，脑区节点本身不删除（dissolve 跳过 is_default_region）"""
    from niu_api.internal.region_manager import (
        is_default_region, get_default_regions_config, REGION_SUFFIX,
        RegionManager,
    )
    import inspect
    # 默认脑区（含 permanent 永久脑区）都被 is_default_region 识别 → dissolve 跳过
    defaults = get_default_regions_config()
    assert len(defaults) > 0, "默认脑区配置不应为空"
    for d in defaults:
        region_name = f"{d['label']}{REGION_SUFFIX}"
        assert is_default_region(region_name), \
            f"默认脑区 {region_name} 应被 is_default_region 识别（dissolve 流程跳过）"
    # dissolve_shrunk_regions 源码应包含 is_default_region 跳过逻辑
    src = inspect.getsource(RegionManager.dissolve_shrunk_regions)
    assert "is_default_region" in src, "dissolve_shrunk_regions 应通过 is_default_region 跳过默认脑区（含 permanent）"


def test_dissolve_shrink_threshold_default_is_100():
    """dissolve_shrunk_regions 默认 shrink_threshold 必须是 100（用户要求，4f03f10d 越权改成 10 要恢复）"""
    import inspect
    from niu_api.internal.region_manager import RegionManager
    sig = inspect.signature(RegionManager.dissolve_shrunk_regions)
    default = sig.parameters["shrink_threshold"].default
    assert default == 100, \
        f"shrink_threshold 默认值必须是 100（用户要求），实际 {default}（4f03f10d 越权改成 10）"


def test_has_isolated_member_returns_true_when_any_member_degree_is_1():
    """有任何一个成员 total_degree=1 → 返回 True（会变孤岛）"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    # 构造图：脑区 A + 成员 X（只有归属边，degree=1）+ 成员 Y（有归属边+知识边，degree=2）
    # 图节点 id 用小写（跟 LightRAG 实际存储一致），传入参数用大写验证 lower 查找路径
    g = nx.Graph()
    g.add_node("测试脑区", entity_type="brainregion")
    g.add_node("成员x", entity_type="concept")
    g.add_node("成员y", entity_type="concept")
    g.add_node("其他实体", entity_type="concept")
    g.add_edge("测试脑区", "成员x", keywords="包含", weight=1.0)  # x 只有这条边
    g.add_edge("测试脑区", "成员y", keywords="包含", weight=1.0)
    g.add_edge("成员y", "其他实体", keywords="相关", weight=1.0)  # y 有 2 条边

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member(["成员X", "成员Y"])  # 传入大写，验证 lower 查找
    assert result is True, "成员X degree=1，应返回 True（会变孤岛）"


def test_has_isolated_member_returns_false_when_all_members_degree_ge_2():
    """所有成员 total_degree >= 2 → 返回 False（安全可解散）"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    # 图节点 id 用小写（跟 LightRAG 实际存储一致），传入参数用大写验证 lower 查找路径
    g = nx.Graph()
    g.add_node("测试脑区", entity_type="brainregion")
    g.add_node("成员x", entity_type="concept")
    g.add_node("成员y", entity_type="concept")
    g.add_node("其他实体A", entity_type="concept")
    g.add_node("其他实体B", entity_type="concept")
    g.add_edge("测试脑区", "成员x", keywords="包含", weight=1.0)
    g.add_edge("测试脑区", "成员y", keywords="包含", weight=1.0)
    g.add_edge("成员x", "其他实体A", keywords="相关", weight=1.0)  # x 有 2 条边
    g.add_edge("成员y", "其他实体B", keywords="相关", weight=1.0)  # y 有 2 条边

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member(["成员X", "成员Y"])  # 传入大写，验证 lower 查找
    assert result is False, "所有成员 degree>=2，应返回 False（安全）"


def test_has_isolated_member_returns_true_when_member_not_in_graph():
    """成员不在图里（数据不一致）→ 返回 True（保守，阻止 dissolve）"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    g = nx.Graph()
    g.add_node("测试脑区", entity_type="brainregion")
    # 成员 X 不在图里

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member(["成员X"])
    assert result is True, "成员不在图里应保守返回 True（阻止 dissolve）"


def test_has_isolated_member_empty_members_returns_false():
    """空成员列表 → 返回 False（脑区 0 成员，无孤岛风险）"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    g = nx.Graph()
    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member([])
    assert result is False, "空成员列表应返回 False"


def test_has_isolated_member_rag_none_returns_true():
    """RAG 实例拿不到 → 返回 True（保守，阻止 dissolve）"""
    from unittest import mock
    from niu_api.internal.region_manager import RegionManager

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = None

    result = manager._has_isolated_member(["成员X"])
    assert result is True, "RAG 拿不到应保守返回 True（阻止 dissolve）"


def test_has_isolated_member_lowercase_lookup():
    """成员名小写查找（跟现有代码 region_manager.py L614-615 模式一致）"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    g = nx.Graph()
    g.add_node("测试脑区", entity_type="brainregion")
    g.add_node("成员x", entity_type="concept")  # 节点 id 小写
    g.add_node("其他实体", entity_type="concept")
    g.add_edge("测试脑区", "成员x", keywords="包含", weight=1.0)
    g.add_edge("成员x", "其他实体", keywords="相关", weight=1.0)

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    # 传入 "成员X"（大写），图里是 "成员x"（小写）——应小写化后找到
    result = manager._has_isolated_member(["成员X"])
    assert result is False, "小写查找后成员x degree=2 应返回 False"


def test_has_isolated_member_non_string_member_skipped():
    """成员名是 int 类型 → 跳过该成员，不影响其他成员检查"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    g = nx.Graph()
    g.add_node("测试脑区", entity_type="brainregion")
    g.add_node("成员x", entity_type="concept")
    g.add_node("其他实体", entity_type="concept")
    g.add_edge("测试脑区", "成员x", keywords="包含", weight=1.0)
    g.add_edge("成员x", "其他实体", keywords="相关", weight=1.0)

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    # 传入 [123, "成员X"]——123 是 int 跳过，"成员X" degree=2
    result = manager._has_isolated_member([123, "成员X"])
    assert result is False, "int 成员跳过，字符串成员 degree=2 应返回 False"


def test_has_isolated_member_nx_graph_none_returns_true():
    """nx_graph 是 None → 返回 True（保守阻止 dissolve）"""
    from unittest import mock
    from niu_api.internal.region_manager import RegionManager

    fake_rag = mock.MagicMock()
    # chunk_entity_relation_graph 没有 _graph 属性且本身是 None
    fake_rag.chunk_entity_relation_graph = None

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member(["成员X"])
    assert result is True, "nx_graph 是 None 应保守返回 True"


def test_has_isolated_member_second_member_isolated_returns_true():
    """第一个成员安全，第二个成员 degree=1 → 仍返回 True（遍历所有成员）"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    g = nx.Graph()
    g.add_node("测试脑区", entity_type="brainregion")
    g.add_node("成员x", entity_type="concept")  # degree=2（安全）
    g.add_node("成员y", entity_type="concept")  # degree=1（孤岛）
    g.add_node("其他实体", entity_type="concept")
    g.add_edge("测试脑区", "成员x", keywords="包含", weight=1.0)
    g.add_edge("测试脑区", "成员y", keywords="包含", weight=1.0)
    g.add_edge("成员x", "其他实体", keywords="相关", weight=1.0)  # x 有 2 条边
    # y 只有归属边，degree=1

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member(["成员X", "成员Y"])
    assert result is True, "第二个成员 degree=1 应返回 True（遍历所有成员）"


def test_has_isolated_member_degree_zero_returns_true():
    """成员 degree=0（理论上不可能但防御性）→ 返回 True"""
    from unittest import mock
    import networkx as nx
    from niu_api.internal.region_manager import RegionManager

    g = nx.Graph()
    # 孤立节点，没有任何边
    g.add_node("成员x", entity_type="concept")

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member(["成员X"])
    assert result is True, "degree=0 应返回 True（防御性，<=1 都算孤岛风险）"
