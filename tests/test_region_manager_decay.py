"""测试 _decay_brain_region_edges 永久脑区边与普通脑区一致衰减"""
import networkx as nx

from niu_api.internal.region_manager import FLOOR_WEIGHT, _decay_brain_region_edges


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
    from niu_api.internal.region_manager import FLOOR_WEIGHT, _decay_brain_region_edges
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
    from niu_api.internal.region_manager import (
        FLOOR_WEIGHT,
        _decay_brain_region_edges,
        daily_decay_rate,
    )
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
    from niu_api.internal.region_manager import PRIORITY_HALFLIFE, daily_decay_rate
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
    import inspect

    from niu_api.internal.region_manager import (
        REGION_SUFFIX,
        RegionManager,
        get_default_regions_config,
        is_default_region,
    )
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


def test_dissolve_cancelled_when_member_has_only_one_edge():
    """dissolve 执行前发现有成员 degree=1 → 取消 dissolve，shrink_count 持久化（+1 后值）"""
    from unittest import mock

    import networkx as nx

    from niu_api.internal.region_manager import BrainRegionInfo, RegionManager

    # 构造图：脑区A + 成员X（degree=1，孤岛风险）+ 成员Y（degree=2）
    g = nx.Graph()
    g.add_node("脑区a", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试")
    g.add_node("成员x", entity_type="concept")
    g.add_node("成员y", entity_type="concept")
    g.add_node("其他实体", entity_type="concept")
    g.add_edge("脑区a", "成员x", keywords="包含", weight=1.0)  # x 只有这条边（degree=1）
    g.add_edge("脑区a", "成员y", keywords="包含", weight=1.0)
    g.add_edge("成员y", "其他实体", keywords="相关", weight=1.0)  # y 有 2 条边

    adapter = mock.MagicMock()
    adapter._get_rag.return_value = mock.MagicMock(chunk_entity_relation_graph=mock.MagicMock(_graph=g))
    adapter.list_entities.return_value = {
        "status": "ok",
        "data": [{"id": "脑区a", "description": "brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试"}]
    }
    adapter.delete_entity = mock.Mock(return_value={"status": "ok"})

    ingester = mock.MagicMock()

    manager = RegionManager(adapter, ingester)
    manager.get_all_regions = lambda: [
        BrainRegionInfo(
            name="脑区a", label="脑区a", community_id="1",
            description="测试", size=2, representative="成员x",
            members=["成员x", "成员y"], updated_at=1745366400,
        )
    ]

    with mock.patch("niu_api.internal.region_manager.is_default_region", return_value=False), \
         mock.patch("niu_api.internal.lightrag_manager.get_all_region_members",
                    return_value={"脑区a": ["成员x", "成员y"]}), \
         mock.patch.object(manager, "_find_most_similar_neighbor", return_value=None):
        dissolved = manager.dissolve_shrunk_regions(shrink_threshold=100, shrink_rounds=3)

    # 关键断言：dissolve 被取消（成员x degree=1 是孤岛风险）
    assert dissolved == [], "有孤岛风险成员时应取消 dissolve，返回空列表"
    # 脑区节点不应被删除
    adapter.delete_entity.assert_not_called()
    # shrink_count 应持久化（从 2 累加到 3，因为 current_size=2 < 100）
    ingester.inject_custom_kg.assert_called()
    call = ingester.inject_custom_kg.call_args
    entities = call.kwargs.get("entities", [])
    desc = entities[0].get("description", "") if entities else ""
    assert "brain_meta_shrink_count:3" in desc, \
        f"shrink_count 应累加到 3（从 2 +1），实际 {desc}"


def test_dissolve_executed_when_all_members_have_multiple_edges():
    """所有成员 degree >= 2 → 正常执行 dissolve"""
    from unittest import mock

    import networkx as nx

    from niu_api.internal.region_manager import BrainRegionInfo, RegionManager

    g = nx.Graph()
    g.add_node("脑区a", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试")
    g.add_node("成员x", entity_type="concept")
    g.add_node("成员y", entity_type="concept")
    g.add_node("其他实体a", entity_type="concept")
    g.add_node("其他实体b", entity_type="concept")
    g.add_node("目标脑区", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>目标")
    g.add_edge("脑区a", "成员x", keywords="包含", weight=1.0)
    g.add_edge("脑区a", "成员y", keywords="包含", weight=1.0)
    g.add_edge("成员x", "其他实体a", keywords="相关", weight=1.0)  # x degree=2
    g.add_edge("成员y", "其他实体b", keywords="相关", weight=1.0)  # y degree=2

    adapter = mock.MagicMock()
    adapter._get_rag.return_value = mock.MagicMock(chunk_entity_relation_graph=mock.MagicMock(_graph=g))
    adapter.list_entities.return_value = {
        "status": "ok",
        "data": [{"id": "脑区a", "description": "brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试"}]
    }
    adapter.delete_entity = mock.Mock(return_value={"status": "ok"})

    ingester = mock.MagicMock()

    manager = RegionManager(adapter, ingester)
    manager.get_all_regions = lambda: [
        BrainRegionInfo(
            name="脑区a", label="脑区a", community_id="1",
            description="测试", size=2, representative="成员x",
            members=["成员x", "成员y"], updated_at=1745366400,
        )
    ]

    target_region = BrainRegionInfo(
        name="目标脑区", label="目标脑区", community_id="2",
        description="目标", size=0, representative="",
        members=[], updated_at=1745366400,
    )

    with mock.patch("niu_api.internal.region_manager.is_default_region", return_value=False), \
         mock.patch("niu_api.internal.lightrag_manager.get_all_region_members",
                    return_value={"脑区a": ["成员x", "成员y"]}), \
         mock.patch.object(manager, "_find_most_similar_neighbor", return_value=target_region), \
         mock.patch.object(manager, "_refresh_activation_cache_after_delete"):
        dissolved = manager.dissolve_shrunk_regions(shrink_threshold=100, shrink_rounds=3)

    # 关键断言：dissolve 正常执行
    assert dissolved == ["脑区a"], "所有成员 degree>=2 时应正常 dissolve"
    adapter.delete_entity.assert_called_once_with("脑区a")


def test_dissolve_cancelled_persists_shrink_count_for_next_round():
    """dissolve 被孤岛保护取消后，shrink_count 持久化（累加后值），下轮重新扫"""
    from unittest import mock

    import networkx as nx

    from niu_api.internal.region_manager import BrainRegionInfo, RegionManager

    # 脑区A 成员X 只有 1 条边（孤岛风险），dissolve 应被取消
    g = nx.Graph()
    g.add_node("脑区a", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试")
    g.add_node("成员x", entity_type="concept")
    g.add_edge("脑区a", "成员x", keywords="包含", weight=1.0)  # x degree=1

    adapter = mock.MagicMock()
    adapter._get_rag.return_value = mock.MagicMock(chunk_entity_relation_graph=mock.MagicMock(_graph=g))
    adapter.list_entities.return_value = {
        "status": "ok",
        "data": [{"id": "脑区a", "description": "brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试"}]
    }
    adapter.delete_entity = mock.Mock(return_value={"status": "ok"})

    ingester = mock.MagicMock()

    manager = RegionManager(adapter, ingester)
    manager.get_all_regions = lambda: [
        BrainRegionInfo(
            name="脑区a", label="脑区a", community_id="1",
            description="测试", size=1, representative="成员x",
            members=["成员x"], updated_at=1745366400,
        )
    ]

    with mock.patch("niu_api.internal.region_manager.is_default_region", return_value=False), \
         mock.patch("niu_api.internal.lightrag_manager.get_all_region_members",
                    return_value={"脑区a": ["成员x"]}), \
         mock.patch.object(manager, "_find_most_similar_neighbor", return_value=None):
        dissolved = manager.dissolve_shrunk_regions(shrink_threshold=100, shrink_rounds=3)

    assert dissolved == [], "孤岛保护应取消 dissolve"
    # shrink_count 从 2 累加到 3（current_size=1 < 100），持久化等下轮重新扫
    ingester.inject_custom_kg.assert_called()
    call = ingester.inject_custom_kg.call_args
    entities = call.kwargs.get("entities", [])
    desc = entities[0].get("description", "") if entities else ""
    assert "brain_meta_shrink_count:3" in desc, \
        f"shrink_count 应累加到 3 持久化，实际 {desc}"


def test_dissolve_zero_member_region_not_blocked_by_island_check():
    """0 成员脑区 → _has_isolated_member([]) 返回 False → 正常 dissolve（用户需求第3条）"""
    from unittest import mock

    import networkx as nx

    from niu_api.internal.region_manager import BrainRegionInfo, RegionManager

    g = nx.Graph()
    g.add_node("脑区a", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试")

    adapter = mock.MagicMock()
    adapter._get_rag.return_value = mock.MagicMock(chunk_entity_relation_graph=mock.MagicMock(_graph=g))
    adapter.list_entities.return_value = {
        "status": "ok",
        "data": [{"id": "脑区a", "description": "brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试"}]
    }
    adapter.delete_entity = mock.Mock(return_value={"status": "ok"})

    ingester = mock.MagicMock()

    manager = RegionManager(adapter, ingester)
    manager.get_all_regions = lambda: [
        BrainRegionInfo(
            name="脑区a", label="脑区a", community_id="1",
            description="测试", size=0, representative="",
            members=[], updated_at=1745366400,
        )
    ]

    target_region = BrainRegionInfo(
        name="目标脑区", label="目标脑区", community_id="2",
        description="目标", size=0, representative="",
        members=[], updated_at=1745366400,
    )

    with mock.patch("niu_api.internal.region_manager.is_default_region", return_value=False), \
         mock.patch("niu_api.internal.lightrag_manager.get_all_region_members",
                    return_value={"脑区a": []}), \
         mock.patch.object(manager, "_find_most_similar_neighbor", return_value=target_region), \
         mock.patch.object(manager, "_refresh_activation_cache_after_delete"):
        dissolved = manager.dissolve_shrunk_regions(shrink_threshold=100, shrink_rounds=3)

    # 0 成员脑区该删就删（孤岛保护不挡）
    assert dissolved == ["脑区a"], "0 成员脑区应正常 dissolve（无孤岛风险）"
    adapter.delete_entity.assert_called_once_with("脑区a")


def test_dissolve_default_region_skipped_no_island_check():
    """缺省脑区仍被 is_default_region 跳过，不进孤岛检查"""
    from unittest import mock

    import networkx as nx

    from niu_api.internal.region_manager import BrainRegionInfo, RegionManager

    g = nx.Graph()
    g.add_node("文档库脑区", entity_type="brainregion",
               description="brain_meta_priority:permanent<SEP>brain_meta_shrink_count:5<SEP>测试")
    g.add_node("成员x", entity_type="concept")
    g.add_edge("文档库脑区", "成员x", keywords="包含", weight=1.0)  # x degree=1（孤岛风险）

    adapter = mock.MagicMock()
    adapter._get_rag.return_value = mock.MagicMock(chunk_entity_relation_graph=mock.MagicMock(_graph=g))
    adapter.list_entities.return_value = {
        "status": "ok",
        "data": [{"id": "文档库脑区", "description": "brain_meta_priority:permanent<SEP>brain_meta_shrink_count:5<SEP>测试"}]
    }
    adapter.delete_entity = mock.Mock(return_value={"status": "ok"})

    ingester = mock.MagicMock()

    manager = RegionManager(adapter, ingester)
    manager.get_all_regions = lambda: [
        BrainRegionInfo(
            name="文档库脑区", label="文档库", community_id="1",
            description="测试", size=1, representative="成员x",
            members=["成员x"], updated_at=1745366400,
        )
    ]

    # is_default_region 返回 True（缺省脑区）
    with mock.patch("niu_api.internal.region_manager.is_default_region", return_value=True), \
         mock.patch("niu_api.internal.lightrag_manager.get_all_region_members",
                    return_value={"文档库脑区": ["成员x"]}):
        dissolved = manager.dissolve_shrunk_regions(shrink_threshold=100, shrink_rounds=3)

    # 缺省脑区直接跳过，不进孤岛检查，不删
    assert dissolved == [], "缺省脑区应被跳过，不 dissolve"
    adapter.delete_entity.assert_not_called()


def test_dissolve_multiple_regions_one_blocked_one_succeeds():
    """多个脑区同时 dissolve：脑区A 被孤岛保护挡住、脑区B 正常 dissolve"""
    from unittest import mock

    import networkx as nx

    from niu_api.internal.region_manager import BrainRegionInfo, RegionManager

    g = nx.Graph()
    # 脑区A：成员x degree=1（孤岛风险）
    g.add_node("脑区a", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试")
    g.add_node("成员x", entity_type="concept")
    g.add_edge("脑区a", "成员x", keywords="包含", weight=1.0)
    # 脑区B：成员y degree=2（安全）
    g.add_node("脑区b", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试")
    g.add_node("成员y", entity_type="concept")
    g.add_node("其他实体", entity_type="concept")
    g.add_edge("脑区b", "成员y", keywords="包含", weight=1.0)
    g.add_edge("成员y", "其他实体", keywords="相关", weight=1.0)
    # 目标脑区
    g.add_node("目标脑区", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>目标")

    adapter = mock.MagicMock()
    adapter._get_rag.return_value = mock.MagicMock(chunk_entity_relation_graph=mock.MagicMock(_graph=g))
    adapter.list_entities.return_value = {
        "status": "ok",
        "data": [
            {"id": "脑区a", "description": "brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试"},
            {"id": "脑区b", "description": "brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试"},
        ]
    }
    adapter.delete_entity = mock.Mock(return_value={"status": "ok"})

    ingester = mock.MagicMock()

    manager = RegionManager(adapter, ingester)
    manager.get_all_regions = lambda: [
        BrainRegionInfo(
            name="脑区a", label="脑区a", community_id="1",
            description="测试", size=1, representative="成员x",
            members=["成员x"], updated_at=1745366400,
        ),
        BrainRegionInfo(
            name="脑区b", label="脑区b", community_id="2",
            description="测试", size=1, representative="成员y",
            members=["成员y"], updated_at=1745366400,
        ),
    ]

    target_region = BrainRegionInfo(
        name="目标脑区", label="目标脑区", community_id="3",
        description="目标", size=0, representative="",
        members=[], updated_at=1745366400,
    )

    with mock.patch("niu_api.internal.region_manager.is_default_region", return_value=False), \
         mock.patch("niu_api.internal.lightrag_manager.get_all_region_members",
                    return_value={"脑区a": ["成员x"], "脑区b": ["成员y"]}), \
         mock.patch.object(manager, "_find_most_similar_neighbor", return_value=target_region), \
         mock.patch.object(manager, "_refresh_activation_cache_after_delete"):
        dissolved = manager.dissolve_shrunk_regions(shrink_threshold=100, shrink_rounds=3)

    # 脑区A 被孤岛保护挡住，脑区B 正常 dissolve
    assert dissolved == ["脑区b"], \
        f"应只 dissolve 脑区b（脑区a 被孤岛保护挡住），实际 {dissolved}"
    adapter.delete_entity.assert_called_once_with("脑区b")


def test_dissolve_failure_persists_accumulated_shrink_count():
    """dissolve 失败（delete_entity 返回非 ok）时 shrink_count 持续累加（I1 修复核心场景）

    验证 should_skip_persist 在 else 分支不设 True，让持久化分支写 shrink_count
    """
    from unittest import mock

    import networkx as nx

    from niu_api.internal.region_manager import BrainRegionInfo, RegionManager

    # 构造图：所有成员 degree >= 2（无孤岛风险，should_dissolve=True）
    g = nx.Graph()
    g.add_node("脑区a", entity_type="brainregion",
               description="brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试")
    g.add_node("成员x", entity_type="concept")
    g.add_node("成员y", entity_type="concept")
    g.add_node("其他实体a", entity_type="concept")
    g.add_node("其他实体b", entity_type="concept")
    g.add_edge("脑区a", "成员x", keywords="包含", weight=1.0)
    g.add_edge("脑区a", "成员y", keywords="包含", weight=1.0)
    g.add_edge("成员x", "其他实体a", keywords="相关", weight=1.0)  # x degree=2
    g.add_edge("成员y", "其他实体b", keywords="相关", weight=1.0)  # y degree=2

    adapter = mock.MagicMock()
    adapter._get_rag.return_value = mock.MagicMock(
        chunk_entity_relation_graph=mock.MagicMock(_graph=g)
    )
    adapter.list_entities.return_value = {
        "status": "ok",
        "data": [{"id": "脑区a", "description": "brain_meta_priority:medium<SEP>brain_meta_shrink_count:2<SEP>测试"}]
    }
    # 关键：delete_entity 返回非 ok（模拟失败）
    adapter.delete_entity = mock.Mock(return_value={"status": "error", "message": "kg locked"})

    ingester = mock.MagicMock()

    manager = RegionManager(adapter, ingester)
    manager.get_all_regions = lambda: [
        BrainRegionInfo(
            name="脑区a", label="脑区a", community_id="1",
            description="测试", size=2, representative="成员x",
            members=["成员x", "成员y"], updated_at=1745366400,
        )
    ]

    target_region = BrainRegionInfo(
        name="目标脑区", label="目标脑区", community_id="2",
        description="目标", size=0, representative="",
        members=[], updated_at=1745366400,
    )

    with mock.patch("niu_api.internal.region_manager.is_default_region", return_value=False), \
         mock.patch("niu_api.internal.lightrag_manager.get_all_region_members",
                    return_value={"脑区a": ["成员x", "成员y"]}), \
         mock.patch.object(manager, "_find_most_similar_neighbor", return_value=target_region), \
         mock.patch.object(manager, "_refresh_activation_cache_after_delete"):
        dissolved = manager.dissolve_shrunk_regions(shrink_threshold=100, shrink_rounds=3)

    # 关键断言 1：dissolve 失败，dissolved 为空
    assert dissolved == [], "delete_entity 失败时 dissolved 应为空"
    # 关键断言 2：delete_entity 被调用（确实尝试过 dissolve）
    adapter.delete_entity.assert_called_once_with("脑区a")
    # 关键断言 3：shrink_count 持续累加（从 2 +1 到 3，未清零，未保持原值）
    ingester.inject_custom_kg.assert_called()
    call = ingester.inject_custom_kg.call_args
    entities = call.kwargs.get("entities", [])
    desc = entities[0].get("description", "") if entities else ""
    assert "brain_meta_shrink_count:3" in desc, \
        f"dissolve 失败时 shrink_count 应累加到 3 持久化，实际 {desc}"
    # 关键断言 4：不应该有 brain_meta_shrink_count:0（清零）或 brain_meta_shrink_count:2（保持原值）
    assert "brain_meta_shrink_count:0" not in desc, "dissolve 失败不应清零 shrink_count"
    assert "brain_meta_shrink_count:2" not in desc or "brain_meta_shrink_count:2<" not in desc, \
        "dissolve 失败不应保持原值 shrink_count:2"


def test_has_isolated_member_rag_raises_returns_true():
    """_get_rag() 抛异常（不是返回 None）→ try/except 兜底返回 True（保守阻止 dissolve）"""
    from unittest import mock

    from niu_api.internal.region_manager import RegionManager

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    # 关键：_get_rag 抛 RuntimeError（不是返回 None）
    manager._adapter._get_rag = mock.MagicMock(side_effect=RuntimeError("rag half-init"))

    result = manager._has_isolated_member(["成员X"])
    assert result is True, "_get_rag 抛异常时应保守返回 True（阻止 dissolve）"


def test_has_isolated_member_degree_call_raises_returns_true():
    """nx_graph.degree(node_id) 抛异常 → try/except 兜底返回 True

    用 MagicMock 让 degree 调用抛异常，验证 try/except 覆盖整个 for 循环
    """
    from unittest import mock

    from niu_api.internal.region_manager import RegionManager

    # 构造一个会让 degree() 抛异常的 mock 图
    fake_graph = mock.MagicMock()
    fake_graph.__contains__ = mock.MagicMock(return_value=True)  # node_id in nx_graph 返回 True
    fake_graph.degree = mock.MagicMock(side_effect=RuntimeError("graph corrupted"))

    fake_kg = mock.MagicMock()
    fake_kg._graph = fake_graph

    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph = fake_kg

    manager = RegionManager.__new__(RegionManager)
    manager._adapter = mock.MagicMock()
    manager._adapter._get_rag.return_value = fake_rag

    result = manager._has_isolated_member(["成员X"])
    assert result is True, "degree() 抛异常时应保守返回 True（阻止 dissolve）"

