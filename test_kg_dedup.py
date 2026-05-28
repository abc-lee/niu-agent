"""
知识图谱去重测试 — 直接读取 graphml 文件验证 ainsert_custom_kg 的覆盖行为
纯后台测试，不依赖飞书/Electron，不依赖 LLM
"""
import networkx as nx
from pathlib import Path

GRAPH_PATH = Path.home() / ".niu" / "lightrag_storage" / "graph_chunk_entity_relation.graphml"


def load_graph():
    """直接加载 graphml 文件"""
    print(f"加载图谱: {GRAPH_PATH}")
    G = nx.read_graphml(str(GRAPH_PATH))
    print(f"节点数: {G.number_of_nodes()}, 边数: {G.number_of_edges()}")
    return G


def test_has_node_precision(G):
    """测试1: 验证 has_node 是精确匹配"""
    print(f"\n=== 测试1: has_node 精确匹配验证 ===")

    test_names = ["Niu", "niu", "NIU", "Niu ", "不可能存在_XYZ999"]
    for name in test_names:
        has = G.has_node(name)
        print(f"  has_node('{name}'): {has}")

    # 找到实际存在的节点名
    nodes = list(G.nodes())[:10]
    print(f"\n  前10个节点名: {nodes[:5]}...")

    if nodes:
        first = nodes[0]
        print(f"  精确匹配 '{first}': {G.has_node(first)}")
        print(f"  变体 '{first.upper()}': {G.has_node(first.upper())}")
        print(f"  变体 '{first.lower()}': {G.has_node(first.lower())}")
        print(f"  变体 '{first} ': {G.has_node(first + ' ')}")

    print(f"\n  结论: Python dict key 是精确匹配，大小写敏感")


def test_entity_types(G):
    """测试2: 查看各类实体数量和覆盖风险"""
    print(f"\n=== 测试2: 实体类型分布 ===")

    type_count = {}
    for node, data in G.nodes(data=True):
        etype = data.get("entity_type", "unknown")
        type_count[etype] = type_count.get(etype, 0) + 1

    for etype, count in sorted(type_count.items(), key=lambda x: -x[1]):
        print(f"  {etype}: {count}")


def test_duplicate_entities(G):
    """测试3: 检查是否已有同名实体（大小写变体）"""
    print(f"\n=== 测试3: 同名实体检测（大小写变体） ===")

    name_map = {}
    for node in G.nodes():
        lower = node.lower()
        if lower not in name_map:
            name_map[lower] = []
        name_map[lower].append(node)

    duplicates = {k: v for k, v in name_map.items() if len(v) > 1}
    if duplicates:
        print(f"  发现 {len(duplicates)} 组大小写变体:")
        for lower, variants in list(duplicates.items())[:5]:
            print(f"    '{lower}': {variants}")
    else:
        print(f"  无大小写变体冲突")


def test_description_sep(G):
    """测试4: 检查 description 中 <SEP> 的使用情况"""
    print(f"\n=== 测试4: description 中 <SEP> 分隔符使用情况 ===")

    sep_count = 0
    multi_desc_examples = []
    for node, data in G.nodes(data=True):
        desc = data.get("description", "")
        if "<SEP>" in desc:
            sep_count += 1
            if len(multi_desc_examples) < 3:
                parts = desc.split("<SEP>")
                multi_desc_examples.append((node, len(parts), desc[:150]))

    total = G.number_of_nodes()
    print(f"  含 <SEP> 的实体: {sep_count}/{total} ({100*sep_count/total:.1f}%)")

    for name, parts_count, sample in multi_desc_examples:
        print(f"\n  示例: '{name}' ({parts_count}段)")
        print(f"    '{sample}...'")


def test_source_id_accumulation(G):
    """测试5: 检查 source_id 的累积情况"""
    print(f"\n=== 测试5: source_id 累积情况 ===")

    multi_source = 0
    examples = []
    for node, data in G.nodes(data=True):
        src = data.get("source_id", "")
        if "<SEP>" in str(src):
            multi_source += 1
            if len(examples) < 3:
                examples.append((node, src[:100]))

    total = G.number_of_nodes()
    print(f"  含多 source_id 的实体: {multi_source}/{total}")

    for name, src in examples:
        print(f"    '{name}': source_id='{src}...'")


def test_cover_behavior_simulation(G):
    """测试6: 模拟覆盖行为 — 如果再次 ainsert_custom_kg 同名实体会怎样"""
    print(f"\n=== 测试6: 覆盖行为模拟 ===")

    # 找一个有 <SEP> 的实体，模拟再次插入
    target = None
    for node, data in G.nodes(data=True):
        desc = data.get("description", "")
        if "<SEP>" in desc and len(desc) > 50:
            target = (node, data)
            break

    if not target:
        # 没找到含SEP的，随便找一个
        for node, data in G.nodes(data=True):
            if data.get("description", ""):
                target = (node, data)
                break

    if not target:
        print(f"  没找到有 description 的实体")
        return

    node_name, node_data = target
    print(f"  选中实体: '{node_name}'")
    print(f"  当前 description: '{node_data.get('description', '')[:100]}...'")
    print(f"  当前 source_id: '{node_data.get('source_id', '')[:80]}...'")
    print(f"  当前 entity_type: '{node_data.get('entity_type', '')}'")

    print(f"\n  模拟：如果通过 ainsert_custom_kg 再次插入同名实体：")
    print(f"    NetworkX add_node(node_name, **new_data) 的行为：")
    print(f"    - 新 data 中存在的 key → 覆盖旧值")
    print(f"    - 新 data 中不存在的 key → 保留旧值")
    print(f"    - 所以 description、source_id、entity_type 会被新值覆盖")
    print(f"    - 但 brain_meta_* 等新 data 不包含的 key 会保留")

    # 验证 NetworkX add_node 行为
    print(f"\n  NetworkX 行为验证:")
    test_G = nx.DiGraph()
    test_G.add_node("test", a="old_a", b="old_b", c="old_c")
    test_G.add_node("test", a="new_a", d="new_d")  # 再次 add_node
    data = dict(test_G.nodes["test"])
    print(f"    第一次: a=old_a, b=old_b, c=old_c")
    print(f"    第二次: a=new_a, d=new_d (覆盖a, 新增d)")
    print(f"    结果: {data}")
    print(f"    a被覆盖: {data['a'] == 'new_a'}, b保留: {data.get('b') == 'old_b'}, c保留: {data.get('c') == 'old_c'}, d新增: {data.get('d') == 'new_d'}")

    print(f"\n  ❌ 结论: ainsert_custom_kg 会让 description/source_id/entity_type 被覆盖，旧值丢失")
    print(f"  ✅ 修复方向: 插入前先查询已有实体，已存在则跳过")


def main():
    print("知识图谱去重测试 — 直接读取 graphml 文件")
    print("=" * 60)

    G = load_graph()
    test_has_node_precision(G)
    test_entity_types(G)
    test_duplicate_entities(G)
    test_description_sep(G)
    test_source_id_accumulation(G)
    test_cover_behavior_simulation(G)

    print("\n" + "=" * 60)
    print("测试完成")


if __name__ == "__main__":
    main()