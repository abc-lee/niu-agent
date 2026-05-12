"""
TDD 测试：照片/文档入库后知识图谱实体合并验证。

分层测试：
- P0 单元测试：直接操作 NetworkX 图，验证 ainsert_custom_kg 的合并行为
  不需要 LLM proxy，只需要 LightRAG 初始化（可离线）
- P1 集成测试：需要 LLM proxy 运行，验证 ainsert 的 LLM 实体提取

核心验证问题：
1. person:{uuid} 实体注入后，再次注入同名实体是否合并 description
2. ainsert_custom_kg 注入 person:{uuid} 实体后，ainsert 是否能自动合并
3. name_person 的 inject_entity 是否正确更新 description
4. 前端分类兼容性验证
"""

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "niu_api"))
sys.path.insert(0, str(PROJECT_ROOT))


def get_rag():
    """获取 LightRAG 实例。"""
    from niu_api.internal.lightrag_manager import get_lightrag
    return get_lightrag()


def get_ingester():
    """获取 LightRAGIngester 实例。"""
    from niu_api.internal.lightrag_adapter import LightRAGIngester
    return LightRAGIngester()


def read_kg_graph_nodes():
    """读取 NetworkX 图谱中的所有节点。"""
    rag = get_rag()
    if rag is None:
        print("❌ LightRAG not available")
        return {}
    graph = rag.chunk_entity_relation_graph
    # NetworkXStorage 内部用 _graph 存储实际的 NetworkX 图
    nx_graph = graph._graph if hasattr(graph, '_graph') else graph
    nodes = {}
    for node_id in nx_graph.nodes():
        node_data = dict(nx_graph.nodes[node_id])
        nodes[node_id] = node_data
    return nodes


def find_person_uuid_entities():
    """找到所有 person:{uuid} 格式的实体。"""
    nodes = read_kg_graph_nodes()
    result = {}
    for node_id, data in nodes.items():
        if node_id.startswith("person:"):
            result[node_id] = data
    return result


def find_plain_name_person_entities():
    """找到所有普通名字的人物实体（非 person:{uuid} 格式）。"""
    nodes = read_kg_graph_nodes()
    result = {}
    for node_id, data in nodes.items():
        if not node_id.startswith("person:"):
            et = data.get("entity_type", "").lower()
            if et == "person":
                result[node_id] = data
    return result


def find_depicts_edges():
    """找到所有 depicts 关系。"""
    rag = get_rag()
    if rag is None:
        return []
    nx_graph = rag.chunk_entity_relation_graph._graph
    edges = []
    for src, tgt, data in nx_graph.edges(data=True):
        kw = data.get("keywords", "")
        if "depicts" in kw.lower():
            edges.append({"src": src, "tgt": tgt, "data": data})
    return edges


def find_co_appears_edges():
    """找到所有 co_appears_with 关系。"""
    rag = get_rag()
    if rag is None:
        return []
    nx_graph = rag.chunk_entity_relation_graph._graph
    edges = []
    for src, tgt, data in nx_graph.edges(data=True):
        kw = data.get("keywords", "")
        if "co_appears_with" in kw.lower():
            edges.append({"src": src, "tgt": tgt, "data": data})
    return edges


# ============================================================
# P0-1: 验证 ainsert_custom_kg 的实体合并行为
# ============================================================

def test_p0_1_custom_kg_entity_merge():
    """P0-1: 验证注入同名 person:{uuid} 实体时 description 是否合并。

    测试步骤：
    1. 注入 person:test-uuid-001, description="张三, detected in photo: test"
    2. 再次注入 person:test-uuid-001, description="Renamed to: 张三"
    3. 检查 KG 中该实体的 description 是否包含两次的内容
    """
    print("\n" + "=" * 60)
    print("P0-1: ainsert_custom_kg 实体合并验证")
    print("=" * 60)

    ingester = get_ingester()

    # Step 1: 首次注入
    test_uuid = "test-uuid-001"
    entity_name = f"person:{test_uuid}"

    result1 = ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description="张三, detected in photo: test_photo_001",
        source_id="test_p0_1",
        file_path="test_photo_001.jpg",
    )
    print(f"\n首次注入结果: {result1}")

    time.sleep(2)

    # 检查首次注入后的实体
    nodes = read_kg_graph_nodes()
    if entity_name in nodes:
        data = nodes[entity_name]
        print(f"✅ 首次注入后实体存在: {entity_name}")
        print(f"  entity_type: {data.get('entity_type', 'N/A')}")
        print(f"  description: {data.get('description', 'N/A')[:100]}")
    else:
        print(f"❌ 首次注入后实体不存在: {entity_name}")
        return False

    first_desc = data.get("description", "")

    # Step 2: 再次注入（模拟改名）
    result2 = ingester.inject_entity(
        name=entity_name,
        entity_type="person",
        description="Renamed to: 张三",
        source_id="test_p0_1_rename",
        file_path="test_rename",
    )
    print(f"\n改名注入结果: {result2}")

    time.sleep(2)

    # 检查合并后的实体
    nodes = read_kg_graph_nodes()
    if entity_name in nodes:
        data = nodes[entity_name]
        merged_desc = data.get("description", "")
        print(f"\n✅ 合并后实体存在: {entity_name}")
        print(f"  entity_type: {data.get('entity_type', 'N/A')}")
        print(f"  description: {merged_desc[:200]}")

        # 关键验证：description 是否包含两次注入的内容
        if "张三, detected in photo" in merged_desc and "Renamed to" in merged_desc:
            print(f"\n✅ description 包含两次注入的内容 — 合并成功！")
            return True
        elif "Renamed to" in merged_desc and "张三, detected in photo" not in merged_desc:
            print(f"\n⚠️ description 只包含第二次注入的内容 — 被覆盖而非合并！")
            print(f"  这意味着 ainsert_custom_kg 对同名实体是 upsert（覆盖），不是 merge")
            return "OVERWRITE"
        else:
            print(f"\n❌ description 内容异常: {merged_desc[:200]}")
            return False
    else:
        print(f"❌ 合并后实体不存在: {entity_name}")
        return False


# ============================================================
# P0-2: 验证 inject_entity 的 entity_type 大小写行为
# ============================================================

def test_p0_2_entity_type_case():
    """P0-2: 验证 entity_type 大小写对合并的影响。

    测试步骤：
    1. 注入 person:test-uuid-002, entity_type="Person" (大写)
    2. 再次注入 person:test-uuid-002, entity_type="person" (小写)
    3. 检查最终 entity_type 是什么
    """
    print("\n" + "=" * 60)
    print("P0-2: entity_type 大小写合并验证")
    print("=" * 60)

    ingester = get_ingester()

    test_uuid = "test-uuid-002"
    entity_name = f"person:{test_uuid}"

    # Step 1: 大写 Person
    ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description="李四, detected in photo: test",
        source_id="test_p0_2",
    )
    time.sleep(2)

    nodes = read_kg_graph_nodes()
    if entity_name in nodes:
        data = nodes[entity_name]
        print(f"首次注入后 entity_type: {data.get('entity_type', 'N/A')}")

    # Step 2: 小写 person
    ingester.inject_entity(
        name=entity_name,
        entity_type="person",
        description="Renamed to: 李四",
        source_id="test_p0_2_rename",
    )
    time.sleep(2)

    nodes = read_kg_graph_nodes()
    if entity_name in nodes:
        data = nodes[entity_name]
        final_type = data.get("entity_type", "N/A")
        print(f"\n合并后 entity_type: {final_type}")

        # LightRAG 的 _merge_nodes_then_upsert 用 Counter 取最高频次的 entity_type
        # 如果只有两个值 "Person" 和 "person"，各出现1次，取排序后第一个
        # 但 LightRAG operate.py line 441 强制 .lower()，所以最终应该是 "person"
        if final_type.lower() == "person":
            print(f"✅ entity_type 最终为 {final_type} — 前端 mapNodeType 可兼容")
            return True
        else:
            print(f"❌ entity_type 不兼容: {final_type}")
            return False
    else:
        print(f"❌ 实体不存在")
        return False


# ============================================================
# P0-3: 验证 depicts 和 co_appears_with 关系
# ============================================================

def test_p0_3_depicts_relation():
    """P0-3: 验证 photo → person:{uuid} 的 depicts 关系。"""
    print("\n" + "=" * 60)
    print("P0-3: depicts 关系验证")
    print("=" * 60)

    ingester = get_ingester()

    # 注入一个照片实体和人物实体，建立 depicts 关系
    photo_path = "test_photo_for_depicts.jpg"
    person_uuid = "test-uuid-003"

    result = ingester.inject_custom_kg(
        entities=[
            {"entity_name": f"person:{person_uuid}", "entity_type": "Person",
             "description": "王五, detected in photo: test", "source_id": "test_p0_3"},
        ],
        relationships=[
            {"src_id": photo_path, "tgt_id": f"person:{person_uuid}",
             "keywords": "depicts",
             "description": f"Photo test depicts 王五",
             "source_id": "test_p0_3", "weight": 0.8},
        ],
        chunks=[],
        source_id="test_p0_3",
    )
    print(f"\n注入结果: {result}")
    time.sleep(2)

    # 检查 depicts 关系
    depicts_edges = find_depicts_edges()
    test_depicts = [e for e in depicts_edges
                    if e["tgt"] == f"person:{person_uuid}" and e["src"] == photo_path]

    if test_depicts:
        print(f"✅ 找到 depicts 关系: {photo_path} → person:{person_uuid}")
        for e in test_depicts:
            print(f"  keywords: {e['data'].get('keywords', 'N/A')}")
            print(f"  description: {e['data'].get('description', 'N/A')[:80]}")
        return True
    else:
        print(f"❌ 未找到 depicts 关系")
        # 检查所有边
        rag = get_rag()
        nx_graph = rag.chunk_entity_relation_graph._graph
        for src, tgt, data in nx_graph.edges(data=True):
            if src == photo_path or tgt == f"person:{person_uuid}":
                print(f"  边: {src} → {tgt}, keywords={data.get('keywords', 'N/A')}")
        return False


# ============================================================
# P0-4: 验证前端分类兼容性
# ============================================================

def test_p0_4_frontend_classification():
    """P0-4: 验证前端 mapNodeType 对 person:{uuid} 实体的分类。

    前端 renderer.js 的 mapNodeType() 使用 entityType.toLowerCase() 匹配。
    验证 "Person" 和 "person" 都能正确映射到 "person" 分类。
    """
    print("\n" + "=" * 60)
    print("P0-4: 前端分类兼容性验证")
    print("=" * 60)

    # 模拟前端的 typeColors 和 mapNodeType
    type_colors = {
        "person": "#FF6B6B",
        "organization": "#4ECDC4",
        "technology": "#45B7D1",
        "document": "#96CEB4",
        "photo": "#FFEAA7",
        "video": "#DDA0DD",
        "note": "#87CEEB",
        "chat": "#F0E68C",
        "concept": "#98D8C8",
        "location": "#7FDBFF",
        "event": "#FFA07A",
        "other": "#CCCCCC",
    }

    # 模拟 mapNodeType 逻辑（与 renderer.js 一致）
    def map_node_type(entity_type: str, node_type: str = "", source: str = "") -> str:
        # Document 节点用 source 字段分类
        if node_type == "Document":
            source_map = {"photo": "photo", "video": "video", "note": "note", "chat": "chat"}
            return source_map.get(source, "document")
        if node_type == "Concept":
            return "concept"
        # 其他节点用 entityType.toLowerCase()
        et = entity_type.lower() if entity_type else "other"
        if et in type_colors:
            return et
        return "other"

    # 测试用例
    test_cases = [
        ("Person", "", "", "person"),     # sync_photo_to_kg 用大写
        ("person", "", "", "person"),     # name_person 用小写
        ("PERSON", "", "", "person"),     # 任何大小写
        ("Organization", "", "", "organization"),
        ("Technology", "", "", "technology"),
        ("", "Document", "photo", "photo"),       # 照片文件节点
        ("", "Document", "video", "video"),       # 视频文件节点
        ("", "Document", "", "document"),         # 普通文档节点
        ("", "Document", "note", "note"),         # 便利贴节点
    ]

    all_pass = True
    for entity_type, node_type, source, expected in test_cases:
        result = map_node_type(entity_type, node_type, source)
        status = "✅" if result == expected else "❌"
        print(f"  {status} mapNodeType('{entity_type}', nodeType='{node_type}', source='{source}') → '{result}' (期望 '{expected}')")
        if result != expected:
            all_pass = False

    if all_pass:
        print(f"\n✅ 所有前端分类测试通过 — Person/person 都能正确分类")
    else:
        print(f"\n❌ 有分类不兼容的情况")

    return all_pass


# ============================================================
# P0-5: 验证 ainsert_custom_kg 的 description 覆盖 vs 合并
# ============================================================

def test_p0_5_description_behavior():
    """P0-5: 精确验证 ainsert_custom_kg 对同名实体的 description 处理。

    这是核心测试：确认 LightRAG 对同名实体的 description 是合并还是覆盖。
    这决定了 name_person 改名后应该怎么写 description。
    """
    print("\n" + "=" * 60)
    print("P0-5: description 覆盖 vs 合并行为验证")
    print("=" * 60)

    ingester = get_ingester()

    test_uuid = "test-uuid-005"
    entity_name = f"person:{test_uuid}"

    # 清理：先删除可能存在的旧实体
    rag = get_rag()
    if rag is not None:
        nx_graph = rag.chunk_entity_relation_graph._graph
        if entity_name in nx_graph.nodes():
            nx_graph.remove_node(entity_name)
            print(f"  清理旧实体: {entity_name}")

    # Step 1: 注入原始 description
    desc1 = "赵六, detected in photo: DSC_3272"
    ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description=desc1,
        source_id="photo_DSC_3272",
        file_path="DSC_3272.jpg",
    )
    time.sleep(2)

    nodes = read_kg_graph_nodes()
    if entity_name not in nodes:
        print(f"❌ Step 1 注入失败")
        return False
    after_step1 = nodes[entity_name].get("description", "")
    print(f"\nStep 1 后 description: {after_step1}")

    # Step 2: 注入改名 description
    desc2 = "赵六"  # 只写名字，不写 "Renamed to:"
    ingester.inject_entity(
        name=entity_name,
        entity_type="person",
        description=desc2,
        source_id="rename_action",
        file_path="rename",
    )
    time.sleep(2)

    nodes = read_kg_graph_nodes()
    if entity_name not in nodes:
        print(f"❌ Step 2 注入失败")
        return False
    after_step2 = nodes[entity_name].get("description", "")
    print(f"\nStep 2 后 description: {after_step2}")

    # 分析结果
    if desc1 in after_step2 and desc2 in after_step2:
        print(f"\n✅ description 合并模式 — 两次内容都保留")
        print(f"  这意味着 name_person 应写完整 description（包含名字和照片信息）")
        return "MERGE"
    elif desc2 in after_step2 and desc1 not in after_step2:
        print(f"\n⚠️ description 覆盖模式 — 只保留最新内容")
        print(f"  这意味着 name_person 的 description 会覆盖原始信息")
        print(f"  建议: name_person 应写完整 description（名字 + 所有照片信息）")
        return "OVERWRITE"
    else:
        print(f"\n❌ description 行为异常")
        return "UNKNOWN"


# ============================================================
# P0-6: 验证 ainsert_custom_kg 对已有实体注入时的 source_id 合并
# ============================================================

def test_p0_6_source_id_merge():
    """P0-6: 验证多次注入同名实体时 source_id 是否合并。

    source_id 合并意味着实体关联了多个来源，这对查询很重要。
    """
    print("\n" + "=" * 60)
    print("P0-6: source_id 合并验证")
    print("=" * 60)

    ingester = get_ingester()

    test_uuid = "test-uuid-006"
    entity_name = f"person:{test_uuid}"

    # Step 1: 注入来自照片 A
    ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description="孙七, detected in photo: photo_A",
        source_id="photo_A",
        file_path="photo_A.jpg",
    )
    time.sleep(2)

    nodes = read_kg_graph_nodes()
    source1 = nodes.get(entity_name, {}).get("source_id", "")
    print(f"\nStep 1 后 source_id: {source1}")

    # Step 2: 注入来自照片 B
    ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description="孙七, detected in photo: photo_B",
        source_id="photo_B",
        file_path="photo_B.jpg",
    )
    time.sleep(2)

    nodes = read_kg_graph_nodes()
    source2 = nodes.get(entity_name, {}).get("source_id", "")
    print(f"\nStep 2 后 source_id: {source2}")

    if "photo_A" in source2 and "photo_B" in source2:
        print(f"✅ source_id 合并 — 两个来源都保留")
        return True
    elif "photo_B" in source2 and "photo_A" not in source2:
        print(f"⚠️ source_id 覆盖 — 只保留最新来源")
        return False
    else:
        print(f"❌ source_id 异常: {source2}")
        return False


# ============================================================
# 主测试流程
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="KG 实体合并 TDD 测试")
    parser.add_argument("--step", type=int, default=0, help="执行指定步骤 (1-6)，0=全部")
    args = parser.parse_args()

    # 先检查 LightRAG 是否可用
    rag = get_rag()
    if rag is None:
        print("❌ LightRAG 不可用！需要 API 服务运行或 LLM proxy 可用")
        print("   尝试启动 Python API 服务后再运行测试")
        sys.exit(1)
    else:
        print(f"✅ LightRAG 可用，图谱节点数: {len(rag.chunk_entity_relation_graph._graph.nodes())}")

    results = {}

    if args.step == 0 or args.step == 1:
        results["P0-1"] = test_p0_1_custom_kg_entity_merge()

    if args.step == 0 or args.step == 2:
        results["P0-2"] = test_p0_2_entity_type_case()

    if args.step == 0 or args.step == 3:
        results["P0-3"] = test_p0_3_depicts_relation()

    if args.step == 0 or args.step == 4:
        results["P0-4"] = test_p0_4_frontend_classification()

    if args.step == 0 or args.step == 5:
        results["P0-5"] = test_p0_5_description_behavior()

    if args.step == 0 or args.step == 6:
        results["P0-6"] = test_p0_6_source_id_merge()

    # 汇总
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, result in results.items():
        status = "✅ PASS" if result is True else f"⚠️ {result}" if result else "❌ FAIL"
        print(f"  {name}: {status}")

    print("=" * 60)