"""
P1 集成测试：验证 LLM 实体提取与 KG 合并行为。

需要 API 服务运行（python -m niu_api），LLM proxy 可用。

测试步骤：
1. 先注入 person:{uuid} 实体（模拟照片入库）
2. 用 ainsert 入库包含人名的文档（原始文本）
3. 检查 LLM 是否提取出同名实体
4. 用 ainsert 入库替换人名后的文档
5. 检查 LLM 是否提取出 person:{uuid} 格式实体
"""

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "niu_api"))


def get_rag():
    from niu_api.internal.lightrag_manager import get_lightrag
    return get_lightrag()


def get_ingester():
    from niu_api.internal.lightrag_adapter import LightRAGIngester
    return LightRAGIngester()


def get_nx_graph():
    rag = get_rag()
    return rag.chunk_entity_relation_graph._graph


def find_person_entities():
    """找到所有 person 相关实体。"""
    nx = get_nx_graph()
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
# P1-1: 注入 person:{uuid} 实体 + ainsert 原始文档
# ============================================================

def test_p1_1_raw_document():
    """P1-1: 先注入 person:{uuid}，再用 ainsert 入库包含人名的文档。"""
    print("\n" + "=" * 60)
    print("P1-1: 原始文档 → LLM 实体提取 → 是否与 person:{uuid} 合并")
    print("=" * 60)

    test_uuid = "p1-test-001"
    entity_name = f"person:{test_uuid}"
    person_name = "李明辉"

    ingester = get_ingester()

    # Step 1: 注入 person:{uuid} 实体
    print(f"\nStep 1: 注入 {entity_name}, name={person_name}")
    r1 = ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description=f"{person_name}, detected in photo: test_photo",
        source_id="test_p1_1",
    )
    print(f"  结果: {r1}")
    time.sleep(3)

    # Step 2: 用 ainsert 入库包含人名的文档
    doc_content = f"""
    西柏坡之行回忆

    {person_name}在2009年6月参加了西柏坡之行。这次旅行非常有意义，
    大家一起参观了西柏坡纪念馆。{person_name}对历史非常感兴趣，
    在纪念馆里仔细观看了每一件展品，特别是关于解放战争时期的历史资料。

    同行的还有其他几位朋友，大家一起度过了愉快的时光。
    这次旅行让{person_name}对革命历史有了更深刻的理解。
    """

    print(f"\nStep 2: ainsert 文档（包含 '{person_name}'）")
    print(f"  文档长度: {len(doc_content)} 字符")

    rag = get_rag()
    from niu_api.internal.lightrag_manager import call_async

    track_id = call_async(rag.ainsert(doc_content, doc_id="test_p1_1_raw_doc"))
    print(f"  ainsert 返回: {track_id}")

    # 等待 LLM 处理完成
    print("  等待 LLM 处理...")
    time.sleep(30)

    # Step 3: 检查 KG 中的实体
    uuid_entities, plain_persons = find_person_entities()

    print("\nStep 3: 检查 KG 实体")
    print(f"  person:{{uuid}} 实体数: {len(uuid_entities)}")
    print(f"  普通名字人物实体数: {len(plain_persons)}")

    # 检查目标 person:{uuid} 实体
    if entity_name in uuid_entities:
        data = uuid_entities[entity_name]
        print(f"\n  ✅ {entity_name} 存在")
        print(f"    entity_type: {data.get('entity_type', 'N/A')}")
        print(f"    description: {data.get('description', 'N/A')[:120]}")
    else:
        print(f"\n  ❌ {entity_name} 不存在")

    # 检查是否有以人名为名的实体
    if person_name in plain_persons:
        data = plain_persons[person_name]
        print(f"\n  ⚠️ 发现同名实体 '{person_name}'")
        print(f"    entity_type: {data.get('entity_type', 'N/A')}")
        print(f"    description: {data.get('description', 'N/A')[:120]}")
        print(f"\n  结论: LLM 提取了 '{person_name}' 作为独立实体，")
        print(f"        没有与 {entity_name} 自动合并")
        return "NO_MERGE"
    else:
        print(f"\n  ✅ 没有发现 '{person_name}' 独立实体")
        # 检查 person:{uuid} 的 description 是否更新（说明合并了）
        if entity_name in uuid_entities:
            desc = uuid_entities[entity_name].get("description", "")
            if "西柏坡" in desc or "旅行" in desc or "纪念馆" in desc:
                print(f"  ✅ {entity_name} 的 description 包含文档内容 — 自动合并成功！")
                return "MERGED"
            else:
                print(f"  ℹ️ {entity_name} 的 description 未包含文档内容")
                print(f"     LLM 可能没有将 '{person_name}' 关联到 {entity_name}")
                return "NO_MERGE"
        return "UNKNOWN"


# ============================================================
# P1-2: 注入 person:{uuid} 实体 + ainsert 替换人名的文档
# ============================================================

def test_p1_2_replaced_document():
    """P1-2: 先注入 person:{uuid}，再用 ainsert 入库替换人名后的文档。"""
    print("\n" + "=" * 60)
    print("P1-2: 替换人名文档 → LLM 实体提取 → 是否与 person:{uuid} 合并")
    print("=" * 60)

    test_uuid = "p1-test-002"
    entity_name = f"person:{test_uuid}"
    person_name = "王建国"
    uuid_format = f"{entity_name}({person_name})"

    ingester = get_ingester()

    # Step 1: 注入 person:{uuid} 实体
    print(f"\nStep 1: 注入 {entity_name}, name={person_name}")
    r1 = ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description=f"{person_name}, detected in photo: test_photo_2",
        source_id="test_p1_2",
    )
    print(f"  结果: {r1}")
    time.sleep(3)

    # Step 2: 用 ainsert 入库替换人名后的文档
    doc_content = f"""
    西柏坡之行回忆（替换版）

    {uuid_format}在2009年6月参加了西柏坡之行。这次旅行非常有意义，
    大家一起参观了西柏坡纪念馆。{uuid_format}对历史非常感兴趣，
    在纪念馆里仔细观看了每一件展品，特别是关于解放战争时期的历史资料。

    同行的还有其他几位朋友，大家一起度过了愉快的时光。
    这次旅行让{uuid_format}对革命历史有了更深刻的理解。
    """

    print(f"\nStep 2: ainsert 文档（人名替换为 '{uuid_format}'）")
    print(f"  文档长度: {len(doc_content)} 字符")

    rag = get_rag()
    from niu_api.internal.lightrag_manager import call_async

    track_id = call_async(rag.ainsert(doc_content, doc_id="test_p1_2_replaced_doc"))
    print(f"  ainsert 返回: {track_id}")

    # 等待 LLM 处理完成
    print("  等待 LLM 处理...")
    time.sleep(30)

    # Step 3: 检查 KG 中的实体
    uuid_entities, plain_persons = find_person_entities()

    print("\nStep 3: 检查 KG 实体")
    print(f"  person:{{uuid}} 实体数: {len(uuid_entities)}")
    print(f"  普通名字人物实体数: {len(plain_persons)}")

    # 列出所有 person:{uuid} 实体
    for nid, data in uuid_entities.items():
        if "p1-test" in nid:
            print(f"  {nid}: type={data.get('entity_type', 'N/A')}, desc={data.get('description', 'N/A')[:80]}")

    # 列出所有普通名字人物实体
    for nid, data in plain_persons.items():
        print(f"  {nid}: type={data.get('entity_type', 'N/A')}, desc={data.get('description', 'N/A')[:80]}")

    # 检查目标 person:{uuid} 实体
    if entity_name in uuid_entities:
        data = uuid_entities[entity_name]
        desc = data.get("description", "")
        print(f"\n  ✅ {entity_name} 存在")
        print(f"    entity_type: {data.get('entity_type', 'N/A')}")
        print(f"    description: {desc[:200]}")

        # 检查 description 是否包含文档内容（说明合并了）
        if "西柏坡" in desc or "旅行" in desc or "纪念馆" in desc:
            print(f"\n  ✅ {entity_name} 的 description 包含文档内容 — 自动合并成功！")
            return "MERGED"
        else:
            print(f"\n  ℹ️ {entity_name} 的 description 未包含文档内容")
    else:
        print(f"\n  ❌ {entity_name} 不存在")

    # 检查 LLM 是否提取了 person:{uuid} 格式的实体
    # 或者提取了普通名字
    if person_name in plain_persons:
        print(f"\n  ⚠️ LLM 提取了 '{person_name}' 作为独立实体（忽略了 person: 前缀）")
        return "LLM_IGNORED_PREFIX"

    # 检查是否有新的 person: 开头实体
    new_uuid_entities = {k: v for k, v in uuid_entities.items() if "p1-test" not in k}
    if new_uuid_entities:
        print("\n  ℹ️ 发现新的 person:{uuid} 实体:")
        for nid, data in new_uuid_entities.items():
            print(f"    {nid}: desc={data.get('description', 'N/A')[:80]}")

    return "UNKNOWN"


# ============================================================
# P1-3: 照片入库 + 改名 + 验证 KG 同步
# ============================================================

def test_p1_3_name_person_kg_sync():
    """P1-3: 注入 person:{uuid} → 改名 → 验证 KG description 更新。"""
    print("\n" + "=" * 60)
    print("P1-3: 改名 → KG description 更新验证")
    print("=" * 60)

    test_uuid = "p1-test-003"
    entity_name = f"person:{test_uuid}"

    ingester = get_ingester()

    # Step 1: 注入原始实体
    print(f"\nStep 1: 注入 {entity_name}, name=未命名人物_3")
    ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description="未命名人物_3, detected in photo: DSC_3272",
        source_id="test_p1_3",
    )
    time.sleep(3)

    nx = get_nx_graph()
    if entity_name in nx.nodes():
        data = dict(nx.nodes[entity_name])
        print(f"  原始 description: {data.get('description', 'N/A')[:100]}")
    else:
        print("  ❌ 注入失败")
        return False

    # Step 2: 改名（当前代码的行为）
    print("\nStep 2: 改名为 '陈志远'（当前代码行为）")
    ingester.inject_entity(
        name=entity_name,
        entity_type="person",
        description="Renamed to: 陈志远",
        source_id="test_p1_3_rename",
    )
    time.sleep(3)

    nx = get_nx_graph()
    if entity_name in nx.nodes():
        data = dict(nx.nodes[entity_name])
        desc = data.get("description", "")
        print(f"  改名后 description: {desc[:200]}")
        print(f"  改名后 entity_type: {data.get('entity_type', 'N/A')}")

        if "detected in photo" in desc:
            print("\n  ✅ 照片信息保留 — description 包含 'detected in photo'")
        else:
            print("\n  ❌ 照片信息丢失 — description 不包含 'detected in photo'")
            print("     当前代码写 description='Renamed to: 陈志远'，覆盖了原始信息")

        if "陈志远" in desc:
            print("  ✅ 新名字存在 — description 包含 '陈志远'")
        else:
            print("  ❌ 新名字丢失")

    # Step 3: 改名（修复后的行为）
    print("\nStep 3: 改名为 '陈志远'（修复后行为 — 保留照片信息）")
    new_desc = "陈志远, detected in photo: DSC_3272"
    ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description=new_desc,
        source_id="test_p1_3_fix",
    )
    time.sleep(3)

    nx = get_nx_graph()
    if entity_name in nx.nodes():
        data = dict(nx.nodes[entity_name])
        desc = data.get("description", "")
        print(f"  修复后 description: {desc[:200]}")
        print(f"  修复后 entity_type: {data.get('entity_type', 'N/A')}")

        if "陈志远" in desc and "detected in photo" in desc:
            print("\n  ✅ 修复成功 — 名字和照片信息都保留")
            return True
        else:
            print("\n  ❌ 修复失败")
            return False
    else:
        print("  ❌ 实体不存在")
        return False


# ============================================================
# 主测试流程
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="P1 集成测试")
    parser.add_argument("--step", type=int, default=0, help="执行指定步骤 (1-3)，0=全部")
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
        results["P1-1"] = test_p1_1_raw_document()

    if args.step == 0 or args.step == 2:
        results["P1-2"] = test_p1_2_replaced_document()

    if args.step == 0 or args.step == 3:
        results["P1-3"] = test_p1_3_name_person_kg_sync()

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
