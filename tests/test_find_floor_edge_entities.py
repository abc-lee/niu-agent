"""测试 find_entities_with_single_floor_edge 函数"""
from unittest import mock
import networkx as nx


def _build_graph(nodes_spec, edges_spec):
    """构造测试用 LightRAG 图快照（与 LightRAG 实际类型一致：nx.Graph 无向图）"""
    g = nx.Graph()
    for name, etype in nodes_spec:
        g.add_node(name, entity_type=etype)
    for src, tgt, kw, w in edges_spec:
        g.add_edge(src, tgt, keywords=kw, weight=w)
    return g


def _patch_graph(g):
    """统一构造 mock context：patch get_lightrag 返回带 _graph 属性的 fake_rag + graph_read_lock"""
    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph._graph = g
    return [
        mock.patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=fake_rag),
        mock.patch("niu_api.internal.lightrag_manager.graph_read_lock"),
    ]


def test_empty_graph_returns_empty_set():
    from niu_api.internal import lightrag_manager
    fake_rag = mock.MagicMock()
    fake_rag.chunk_entity_relation_graph = None
    with mock.patch.object(lightrag_manager, "get_lightrag", return_value=fake_rag):
        result = lightrag_manager.find_entities_with_single_floor_edge()
    assert result == set()


def test_lightrag_none_returns_empty_set():
    """get_lightrag 返回 None → 返回空集"""
    from niu_api.internal import lightrag_manager
    with mock.patch.object(lightrag_manager, "get_lightrag", return_value=None):
        result = lightrag_manager.find_entities_with_single_floor_edge()
    assert result == set()


def test_entity_with_single_contains_edge_at_floor_returns_it():
    """实体只有 1 条 _region:contains 边且 weight=0.1 → 命中"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[
            ("智家脑区", "brainregion"),
            ("实体A", "concept"),
        ],
        edges_spec=[
            ("智家脑区", "实体A", "包含", 0.1),
        ],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert "实体a" in result


def test_entity_with_single_contains_edge_above_floor_not_returned():
    """实体只有 1 条 _region:contains 边但 weight=0.5 → 不命中"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[("智家脑区", "brainregion"), ("实体A", "concept")],
        edges_spec=[("智家脑区", "实体A", "包含", 0.5)],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()


def test_entity_with_two_contains_edges_not_returned():
    """实体有 2 条 _region:contains 边（到不同脑区）→ 不命中"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[
            ("脑区X", "brainregion"), ("脑区Y", "brainregion"),
            ("实体A", "concept"),
        ],
        edges_spec=[
            ("脑区X", "实体A", "包含", 0.1),
            ("脑区Y", "实体A", "包含", 0.1),
        ],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()


def test_orphan_entity_not_returned():
    """实体有 0 条 _region:contains 边（孤儿）→ 不命中条件 2"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[
            ("脑区X", "brainregion"),
            ("实体A", "concept"),
            ("实体B", "concept"),
        ],
        edges_spec=[
            ("实体A", "实体B", "相关", 0.5),
        ],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()


def test_entity_with_knowledge_edges_still_counted_correctly():
    """实体有 1 条归属边（保底）+ 多条知识边 → 仍命中（知识边不计数）"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[
            ("智家脑区", "brainregion"),
            ("实体A", "concept"),
            ("实体B", "concept"),
            ("实体C", "concept"),
        ],
        edges_spec=[
            ("智家脑区", "实体A", "包含", 0.1),
            ("实体A", "实体B", "相关", 1.0),
            ("实体A", "实体C", "相关", 0.8),
        ],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert "实体a" in result


def test_weight_at_boundary_floor_value_returns_it():
    """weight 恰好等于 0.1（边界值，<=）→ 命中"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[("智家脑区", "brainregion"), ("实体A", "concept")],
        edges_spec=[("智家脑区", "实体A", "包含", 0.1)],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert "实体a" in result


def test_weight_string_converted_to_float():
    """weight 是字符串 "0.1" → 类型转换后命中"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[("智家脑区", "brainregion"), ("实体A", "concept")],
        edges_spec=[("智家脑区", "实体A", "包含", "0.1")],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert "实体a" in result


def test_weight_none_falls_back_to_default_not_returned():
    """weight 是 None → edge_data.get("weight", 1.0) 默认 1.0，不命中"""
    from niu_api.internal import lightrag_manager
    g = nx.Graph()
    g.add_node("智家脑区", entity_type="brainregion")
    g.add_node("实体A", entity_type="concept")
    g.add_edge("智家脑区", "实体A", keywords="包含")
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()


def test_weight_invalid_type_skipped():
    """weight 是非法类型（如 list）→ try float 失败，跳过该实体"""
    from niu_api.internal import lightrag_manager
    g = nx.Graph()
    g.add_node("智家脑区", entity_type="brainregion")
    g.add_node("实体A", entity_type="concept")
    g.add_edge("智家脑区", "实体A", keywords="包含", weight=[0.1])
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()


def test_session_prefix_edges_skipped():
    """_session: 前缀边不算归属边"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[("智家脑区", "brainregion"), ("实体A", "concept")],
        edges_spec=[
            ("智家脑区", "实体A", "_session:contains", 0.1),
        ],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()


def test_region_node_itself_skipped():
    """脑区节点本身不参与判断"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[("智家脑区", "brainregion"), ("工作脑区", "brainregion")],
        edges_spec=[("智家脑区", "工作脑区", "包含", 0.1)],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()


def test_exception_returns_empty_set():
    """函数内部抛异常 → 返回空集（降级）"""
    from niu_api.internal import lightrag_manager
    with mock.patch.object(lightrag_manager, "get_lightrag", side_effect=RuntimeError("boom")):
        result = lightrag_manager.find_entities_with_single_floor_edge()
    assert result == set()


def test_contains_edge_with_non_brainregion_neighbor_skipped():
    """两条普通实体之间也有 keywords="包含" 的边（不是真归属边）→ 不应被计入归属边数。"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[
            ("智家脑区", "brainregion"),
            ("实体A", "concept"),
            ("实体B", "concept"),
        ],
        edges_spec=[
            ("智家脑区", "实体A", "包含", 0.1),
            ("实体A", "实体B", "包含", 0.1),
        ],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert "实体a" in result


def test_find_entities_skips_brainregion_node_with_region_suffix():
    """验证 find_entities 跳过以"脑区"结尾的脑区节点本身。"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[
            ("智家脑区", "brainregion"),
            ("实体A", "concept"),
        ],
        edges_spec=[
            ("智家脑区", "实体A", "包含", 0.1),
        ],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
        assert "实体a" in result
        assert "智家脑区" not in result
    finally:
        for p in patches:
            p.stop()


def test_weight_int_type_converted():
    """weight 是 int 类型 1 → 类型转换后 1.0，不命中（>0.1）"""
    from niu_api.internal import lightrag_manager
    g = _build_graph(
        nodes_spec=[("智家脑区", "brainregion"), ("实体A", "concept")],
        edges_spec=[("智家脑区", "实体A", "包含", 1)],
    )
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()


def test_non_string_node_id_skipped():
    """node_id 是非字符串类型（如 int）→ 跳过，不进入结果集"""
    from niu_api.internal import lightrag_manager
    g = nx.Graph()
    g.add_node("智家脑区", entity_type="brainregion")
    g.add_node(123, entity_type="concept")
    g.add_edge("智家脑区", 123, keywords="包含", weight=0.1)
    patches = _patch_graph(g)
    for p in patches:
        p.start()
    try:
        result = lightrag_manager.find_entities_with_single_floor_edge()
    finally:
        for p in patches:
            p.stop()
    assert result == set()
