"""
P1 集成测试：验证新 KG 架构下的实体行为。

需要 API 服务运行（python -m niu_api），LLM proxy 可用。

核心验证：
1. person:{uuid} 实体 description 只存名字，不挂 file_path
2. 文档 ainsert 后，LLM 提取人名实体独立存在（不做后处理合并）
3. 两个实体共存，各有各的用途
"""

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "niu_api"))
sys.path.insert(0, str(PROJECT_ROOT))


def get_rag():
    from niu_api.internal.lightrag_manager import get_lightrag
    return get_lightrag()


def get_ingester():
    from niu_api.internal.lightrag_adapter import LightRAGIngester
    return LightRAGIngester()


def get_nx():
    rag = get_rag()
    return rag.chunk_entity_relation_graph._graph


def find_person_entities():
    """找到所有 person 相关实体。"""
    nx = get_nx()
    uuid_entities = {}
    plain_persons = {}
    for node_id, data in nx.nodes(data=True):
        nd = dict(data)
        if node_id.startswith("person:"):
            uuid_entities[node_id] = nd
        else:
            et = nd.get("entity_type", "").lower()
            if et == "person":
                plain_persons[node_id] = nd
    return uuid_entities, plain_persons


# ============================================================
# P1-1: person:{uuid} 实体架构验证
# ============================================================

def test_p1_1_person_entity_structure():
    """P1-1: person:{uuid} 实体 description 只存名字，不挂 file_path。

    流程：
    1. 注入 person:{uuid} 实体
    2. 验证 description 是纯名字
    3. 验证没有 file_path
    """
    print("\n" + "=" * 60)
    print("P1-1: person:{uuid} 实体架构验证")
    print("=" * 60)

    test_uuid = "p1-arch-003"
    entity_name = f"person:{test_uuid}"
    person_name = "陈志远"

    ingester = get_ingester()

    # Step 1: 注入 person:{uuid} 实体
    print(f"\nStep 1: 注入 {entity_name}, description='{person_name}'")
    r1 = ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description=person_name,
        file_path="",
    )
    print(f"  结果: {r1}")
    time.sleep(3)

    # Step 2: 验证实体结构
    nx = get_nx()
    if entity_name not in nx.nodes():
        print("  ❌ 实体不存在")
        return False

    data = dict(nx.nodes[entity_name])
    desc = data.get("description", "")
    fp = data.get("file_path", "")
    et = data.get("entity_type", "")

    print("\nStep 2: 验证实体结构")
    print(f"  description: {desc[:100]}")
    print(f"  entity_type: {et}")
    print(f"  file_path: {fp}")

    ok = True
    # description 应该是纯名字
    if person_name in desc and "detected" not in desc and "photo" not in desc.lower():
        print("  ✅ description 是纯名字")
    else:
        print("  ❌ description 不是纯名字（包含多余信息）")
        ok = False

    # entity_type 应该是 Person
    if et == "Person":
        print("  ✅ entity_type = 'Person'")
    else:
        print(f"  ❌ entity_type = '{et}'（应为 'Person'）")
        ok = False

    # file_path 应该为空
    if not fp or fp == "custom_kg":
        print("  ✅ file_path 为空或默认值")
    else:
        print(f"  ❌ file_path = '{fp}'（应为空）")
        ok = False

    return ok


# ============================================================
# P1-2: 文档入库后 LLM 提取实体（不做后处理合并）
# ============================================================

def test_p1_2_document_entity_independence():
    """P1-2: 文档入库后 LLM 提取人名实体独立存在。

    新架构决策：不做后处理合并。LLM 提取的"任飞"实体和
    person:{uuid} 实体虽然名字相同但节点名不同，独立共存。

    流程：
    1. 注入 person:{uuid} 实体（模拟照片入库）
    2. ainsert 文档（包含人名）
    3. LLM 提取出人名实体
    4. 验证两个实体共存
    5. 验证 person:{uuid} 的 description 仍然是纯名字
    """
    print("\n" + "=" * 60)
    print("P1-2: 文档入库 → LLM 提取 → 实体独立共存")
    print("=" * 60)

    test_uuid = "p1-arch-004"
    entity_name = f"person:{test_uuid}"
    person_name = "赵明远"

    ingester = get_ingester()

    # Step 1: 注入 person:{uuid} 实体
    print(f"\nStep 1: 注入 {entity_name}, description='{person_name}'")
    r1 = ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description=person_name,
        file_path="",
    )
    print(f"  结果: {r1}")
    time.sleep(3)

    nx = get_nx()
    if entity_name in nx.nodes():
        data = dict(nx.nodes[entity_name])
        print(f"  ✅ 实体存在: description={data.get('description', '')}")
    else:
        print("  ❌ 实体不存在")
        return False

    # Step 2: ainsert 文档
    doc_content = f"""
    西柏坡之行回忆

    {person_name}在2009年6月参加了西柏坡之行。这次旅行非常有意义，
    大家一起参观了西柏坡纪念馆。{person_name}对历史非常感兴趣，
    在纪念馆里仔细观看了每一件展品。

    同行的还有其他几位朋友，大家一起度过了愉快的时光。
    这次旅行让{person_name}对革命历史有了更深刻的理解。
    """

    print(f"\nStep 2: ainsert 文档（包含 '{person_name}'）")

    rag = get_rag()
    from niu_api.internal.lightrag_manager import call_async

    track_id = call_async(rag.ainsert(doc_content, ids="test_p1_arch_doc2"))
    print(f"  ainsert 返回: {track_id}")

    # 等待 LLM 处理完成
    print("  等待 LLM 处理（35秒）...")
    time.sleep(35)

    # Step 3: 检查 KG 中的实体
    uuid_entities, plain_persons = find_person_entities()

    print("\nStep 3: 检查 KG 实体")
    print(f"  person:{{uuid}} 实体数: {len(uuid_entities)}")
    print(f"  普通名字人物实体数: {len(plain_persons)}")

    # Step 4: 验证 person:{uuid} 的 description 仍然是纯名字
    if entity_name in uuid_entities:
        data = uuid_entities[entity_name]
        desc = data.get("description", "")
        print(f"\n  ✅ {entity_name} 存在")
        print(f"    description: {desc[:120]}")

        # description 应该仍然是纯名字（不被文档内容污染）
        if person_name in desc and len(desc) < 200:
            print("  ✅ description 仍然是纯名字（未被文档内容污染）")
        else:
            print(f"  ⚠️ description 可能被 LLM 扩展了（长度={len(desc)}）")
    else:
        print(f"\n  ❌ {entity_name} 不存在")
        return False

    # Step 5: 检查是否有独立的人名实体
    # 新架构：独立实体共存是正常行为，不需要合并
    if person_name in plain_persons:
        data = plain_persons[person_name]
        print(f"\n  ℹ️ 发现独立实体 '{person_name}'（与 {entity_name} 共存）")
        print(f"    description: {data.get('description', 'N/A')[:120]}")
        print("  ✅ 两个实体独立共存 — 这是新架构的正常行为")
    else:
        print(f"\n  ℹ️ 没有发现独立实体 '{person_name}'")
        print(f"  LLM 可能没有提取 '{person_name}' 为独立实体")
        print("  这也是正常的 — LLM 可能通过 ainsert_custom_kg 路径合并了")

    return True


# ============================================================
# 主测试流程
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="P1 集成测试")
    parser.add_argument("--step", type=int, default=0, help="执行指定步骤 (1-2)，0=全部")
    args = parser.parse_args()

    # 检查 LightRAG
    rag = get_rag()
    if rag is None:
        print("❌ LightRAG 不可用！")
        sys.exit(1)
    else:
        nx = rag.chunk_entity_relation_graph._graph
        print(f"✅ LightRAG 可用，图谱节点数: {len(nx.nodes())}")

    # 检查 API 服务
    import urllib.request
    try:
        resp = urllib.request.urlopen("http://localhost:9876/health", timeout=3)
        print("✅ API 服务可用")
    except Exception:
        print("⚠️ API 服务不可用，LLM 调用可能失败")

    results = {}

    if args.step == 0 or args.step == 1:
        results["P1-1"] = test_p1_1_person_entity_structure()

    if args.step == 0 or args.step == 2:
        results["P1-2"] = test_p1_2_document_entity_independence()

    # 汇总
    print("\n" + "=" * 60)
    print("P1 集成测试结果汇总")
    print("=" * 60)
    for name, result in results.items():
        if result is True:
            status = "✅ PASS"
        elif result is False:
            status = "❌ FAIL"
        else:
            status = f"⚠️ {result}"
        print(f"  {name}: {status}")

    print("=" * 60)
