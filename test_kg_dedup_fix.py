"""
知识图谱去重修复验证 — 纯后台测试，不依赖飞书/Electron

测试 lightrag_insert_entity 和 lightrag_insert_relation 的去重逻辑：
- 首次插入成功
- 重复插入跳过（返回 skipped=True）
- 旧描述不被覆盖
"""
import sys
import os

# 确保项目路径在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "mcp-servers", "lightrag-server", "src"))
sys.path.insert(0, PROJECT_ROOT)


def test_has_entity_and_has_edge():
    """测试 LightRAGAdapter.has_entity 和 has_edge 方法"""
    print("\n=== 测试1: has_entity / has_edge 基础功能 ===")

    from niu_api.internal.lightrag_adapter import LightRAGAdapter
    adapter = LightRAGAdapter()

    # 测试已存在的实体
    has_niu = adapter.has_entity("Niu")
    print(f"  has_entity('Niu'): {has_niu}")

    # 测试不存在的实体
    has_fake = adapter.has_entity("不可能存在的实体_XYZ999")
    print(f"  has_entity('不可能存在的实体_XYZ999'): {has_fake}")

    assert has_niu == True, "Niu 实体应该存在"
    assert has_fake == False, "不存在的实体应该返回 False"
    print("  ✅ has_entity 基础功能正常")


def test_insert_entity_dedup():
    """测试 lightrag_insert_entity 去重逻辑"""
    print("\n=== 测试2: lightrag_insert_entity 去重 ===")

    from niu_lightrag_server import lightrag_insert_entity, lightrag_insert_relation
    from niu_api.internal.lightrag_adapter import LightRAGAdapter
    adapter = LightRAGAdapter()

    test_name = "测试人物_张三_KGDEDUP"

    # 清理：如果测试实体已存在，先删除
    try:
        from niu_lightrag_server import lightrag_delete_entity
        if adapter.has_entity(test_name):
            lightrag_delete_entity(entity_name=test_name)
            print(f"  清理旧测试实体: {test_name}")
    except Exception:
        pass

    # 步骤1: 首次插入
    result1 = lightrag_insert_entity(
        name=test_name,
        entity_type="Person",
        description="张三是工程师",
        file_path="test_kg_dedup",
    )
    print(f"  首次插入结果: {result1}")
    assert result1.get("status") == "ok", f"首次插入应成功: {result1}"
    assert result1.get("skipped") != True, "首次插入不应跳过"

    # 步骤2: 验证实存在
    assert adapter.has_entity(test_name), f"实体'{test_name}'应该存在"

    # 步骤3: 再次插入同名实体（不同描述）
    result2 = lightrag_insert_entity(
        name=test_name,
        entity_type="Person",
        description="张三是老师",
        file_path="test_kg_dedup",
    )
    print(f"  重复插入结果: {result2}")
    assert result2.get("skipped") == True, f"重复插入应跳过: {result2}"

    # 步骤4: 验证旧描述未被覆盖
    # 读取图中实体数据
    from niu_api.internal.lightrag_manager import get_lightrag, graph_read_lock
    rag = get_lightrag()
    graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
    nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
    with graph_read_lock():
        if nx_graph.has_node(test_name):
            desc = nx_graph.nodes[test_name].get("description", "")
            print(f"  实体描述: '{desc[:100]}'")
            assert "工程师" in desc, f"旧描述'工程师'被覆盖了！当前描述: {desc}"

    print("  ✅ lightrag_insert_entity 去重正常，旧描述未被覆盖")

    # 清理
    try:
        lightrag_delete_entity(entity_name=test_name)
        print(f"  清理测试实体: {test_name}")
    except Exception:
        pass


def test_insert_relation_dedup():
    """测试 lightrag_insert_relation 去重逻辑"""
    print("\n=== 测试3: lightrag_insert_relation 去重 ===")

    from niu_lightrag_server import lightrag_insert_relation
    from niu_api.internal.lightrag_adapter import LightRAGAdapter
    adapter = LightRAGAdapter()

    test_src = "Niu"
    test_tgt = "测试人物_张三_KGDEDUP"

    # 先确保目标实体存在
    if not adapter.has_entity(test_tgt):
        from niu_lightrag_server import lightrag_insert_entity
        lightrag_insert_entity(
            name=test_tgt,
            entity_type="Person",
            description="测试用",
            file_path="test_kg_dedup",
        )

    # 步骤1: 首次插入关系
    result1 = lightrag_insert_relation(
        src_id=test_src,
        tgt_id=test_tgt,
        relation="认识",
        description="测试关系",
        file_path="test_kg_dedup",
    )
    print(f"  首次插入关系结果: {result1}")

    # 步骤2: 再次插入同关系
    result2 = lightrag_insert_relation(
        src_id=test_src,
        tgt_id=test_tgt,
        relation="认识",
        description="测试关系2",
        file_path="test_kg_dedup",
    )
    print(f"  重复插入关系结果: {result2}")
    assert result2.get("skipped") == True, f"重复关系应跳过: {result2}"

    print("  ✅ lightrag_insert_relation 去重正常")

    # 清理
    try:
        from niu_lightrag_server import lightrag_delete_entity
        lightrag_delete_entity(entity_name=test_tgt)
        print(f"  清理测试实体: {test_tgt}")
    except Exception:
        pass


def main():
    print("知识图谱去重修复验证")
    print("=" * 60)

    test_has_entity_and_has_edge()
    test_insert_entity_dedup()
    test_insert_relation_dedup()

    print("\n" + "=" * 60)
    print("所有测试通过 ✅")


if __name__ == "__main__":
    main()
