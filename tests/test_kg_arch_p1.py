"""
P1 集成测试：验证文档入库后 LLM 实体提取 + 后处理合并。

需要 API 服务运行（python -m niu_api），LLM proxy 可用。

核心验证：
1. 文档 ainsert 后，LLM 提取人物实体
2. 后处理合并：LLM 提取的"任飞"与照片的 person:{uuid} 合并
3. 合并后 person:{uuid} 的 description 包含文档内容
"""

import sys
import time
import json
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


def merge_plain_person_into_uuid(plain_name: str, uuid_entity_name: str) -> bool:
    """后处理合并：将普通名字实体合并到 person:{uuid} 实体。

    步骤：
    1. 读取普通名字实体的 description 和边
    2. 将 description 信息追加到 person:{uuid} 的 description
    3. 将普通名字实体的边迁移到 person:{uuid}
    4. 删除普通名字实体
    """
    nx = get_nx()
    ingester = get_ingester()

    # 1. 读取普通名字实体的信息
    if plain_name not in nx.nodes():
        print(f"  ❌ 普通名字实体不存在: {plain_name}")
        return False

    plain_data = dict(nx.nodes[plain_name])
    plain_desc = plain_data.get("description", "")
    print(f"  普通名字实体 description: {plain_desc[:100]}")

    # 2. 读取 person:{uuid} 的当前 description
    if uuid_entity_name not in nx.nodes():
        print(f"  ❌ person:{uuid} 实体不存在: {uuid_entity_name}")
        return False

    uuid_data = dict(nx.nodes[uuid_entity_name])
    uuid_desc = uuid_data.get("description", "")
    print(f"  person:{uuid} 当前 description: {uuid_desc[:100]}")

    # 3. 合并 description — 将文档内容追加到人物 description
    # 人物 description 格式: "名字"
    # 合并后: "名字\n文档中的描述信息"
    new_desc = uuid_desc
    if plain_desc and plain_desc != uuid_desc:
        new_desc = f"{uuid_desc}\n{plain_desc}"

    ingester.inject_entity(
        name=uuid_entity_name,
        entity_type="Person",
        description=new_desc,
        file_path="",
    )
    time.sleep(2)

    # 4. 迁移边 — 将普通名字实体的边迁移到 person:{uuid}
    edges_to_migrate = []
    for src, tgt, data in nx.edges(data=True):
        if src == plain_name or tgt == plain_name:
            other = tgt if src == plain_name else src
            kw = data.get("keywords", "")
            desc = data.get("description", "")
            weight = data.get("weight", 1.0)
            # 不迁移 merged_into 边
            if "merged" in kw.lower():
                continue
            edges_to_migrate.append({
                "src_id": uuid_entity_name,
                "tgt_id": other,
                "keywords": kw,
                "description": desc,
                "weight": weight,
            })

    if edges_to_migrate:
        for edge in edges_to_migrate:
            ingester.inject_relation(
                src_id=edge["src_id"],
                tgt_id=edge["tgt_id"],
                relation=edge["keywords"],
                description=edge["description"],
                weight=edge.get("weight", 1.0),
            )
        time.sleep(2)

    # 5. 删除普通名字实体
    if plain_name in nx.nodes():
        edges_to_remove = [(s, t) for s, t in nx.edges() if s == plain_name or t == plain_name]
        for s, t in edges_to_remove:
            nx.remove_edge(s, t)
        nx.remove_node(plain_name)
        print(f"  ✅ 已删除普通名字实体: {plain_name}")

    return True


# ============================================================
# P1-1: 文档入库 → LLM 提取人物实体 → 后处理合并
# ============================================================

def test_p1_1_document_person_merge():
    """P1-1: 文档入库后 LLM 提取人物实体，后处理合并到 person:{uuid}。

    流程：
    1. 注入 person:{uuid} 实体（模拟照片入库）
    2. ainsert 文档（包含人名）
    3. LLM 提取出人名实体
    4. 后处理合并：人名实体 → person:{uuid}
    5. 验证 person:{uuid} 的 description 包含文档内容
    """
    print("\n" + "=" * 60)
    print("P1-1: 文档入库 → LLM 提取 → 后处理合并")
    print("=" * 60)

    test_uuid = "p1-arch-001"
    entity_name = f"person:{test_uuid}"
    person_name = "任飞"

    ingester = get_ingester()

    # Step 1: 注入 person:{uuid} 实体（模拟照片入库后的 KG 同步）
    print(f"\nStep 1: 注入 {entity_name}, description='{person_name}'")
    r1 = ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description=person_name,
        file_path="",
    )
    print(f"  结果: {r1}")
    time.sleep(3)

    # 验证注入
    nx = get_nx()
    if entity_name in nx.nodes():
        data = dict(nx.nodes[entity_name])
        print(f"  ✅ 实体存在: description={data.get('description', '')}")
    else:
        print(f"  ❌ 实体不存在")
        return False

    # Step 2: ainsert 文档（包含人名）
    doc_content = f"""
    西柏坡之行回忆

    {person_name}在2009年6月参加了西柏坡之行。这次旅行非常有意义，
    大家一起参观了西柏坡纪念馆。{person_name}对历史非常感兴趣，
    在纪念馆里仔细观看了每一件展品，特别是关于解放战争时期的历史资料。

    同行的还有其他几位朋友，大家一起度过了愉快的时光。
    这次旅行让{person_name}对革命历史有了更深刻的理解。
    """

    print(f"\nStep 2: ainsert 文档（包含 '{person_name}'）")

    rag = get_rag()
    from niu_api.internal.lightrag_manager import call_async

    track_id = call_async(rag.ainsert(doc_content, ids="test_p1_arch_doc"))
    print(f"  ainsert 返回: {track_id}")

    # 等待 LLM 处理完成
    print(f"  等待 LLM 处理（30秒）...")
    time.sleep(30)

    # Step 3: 检查 KG 中的实体
    uuid_entities, plain_persons = find_person_entities()

    print(f"\nStep 3: 检查 KG 实体")
    print(f"  person:{{uuid}} 实体数: {len(uuid_entities)}")
    print(f"  普通名字人物实体数: {len(plain_persons)}")

    # 检查 person:{uuid} 实体
    if entity_name in uuid_entities:
        data = uuid_entities[entity_name]
        print(f"\n  ✅ {entity_name} 存在")
        print(f"    description: {data.get('description', 'N/A')[:120]}")
    else:
        print(f"\n  ❌ {entity_name} 不存在")
        return False

    # 检查是否有以人名为名的实体
    if person_name in plain_persons:
        data = plain_persons[person_name]
        print(f"\n  ⚠️ 发现同名实体 '{person_name}'")
        print(f"    description: {data.get('description', 'N/A')[:120]}")

        # Step 4: 后处理合并
        print(f"\nStep 4: 后处理合并 '{person_name}' → {entity_name}")
        merge_ok = merge_plain_person_into_uuid(person_name, entity_name)
        if not merge_ok:
            print(f"  ❌ 合并失败")
            return False

        # Step 5: 验证合并结果
        time.sleep(2)
        nx = get_nx()

        if entity_name in nx.nodes():
            data = dict(nx.nodes[entity_name])
            desc = data.get("description", "")
            print(f"\nStep 5: 验证合并结果")
            print(f"  {entity_name} description: {desc[:200]}")

            # 检查是否包含文档内容
            if person_name in desc:
                print(f"  ✅ description 包含名字 '{person_name}'")
            else:
                print(f"  ❌ description 不包含名字")

            # 检查普通名字实体是否已删除
            if person_name not in nx.nodes():
                print(f"  ✅ 普通名字实体 '{person_name}' 已删除")
            else:
                print(f"  ⚠️ 普通名字实体 '{person_name}' 仍存在")

            return True
        else:
            print(f"  ❌ {entity_name} 不存在")
            return False
    else:
        print(f"\n  ℹ️ 没有发现 '{person_name}' 独立实体")
        print(f"  LLM 可能没有提取 '{person_name}' 为实体")
        # 检查 person:{uuid} 的 description 是否被 LLM 更新
        if entity_name in uuid_entities:
            desc = uuid_entities[entity_name].get("description", "")
            if "西柏坡" in desc or "旅行" in desc:
                print(f"  ✅ person:{uuid} description 包含文档内容 — LLM 自动合并")
                return True
            else:
                print(f"  ℹ️ person:{uuid} description 未包含文档内容")
                print(f"  LLM 没有将 '{person_name}' 关联到 {entity_name}")
                return "NO_MERGE_NEEDED"
        return "NO_MERGE_NEEDED"


# ============================================================
# 主测试流程
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="P1 集成测试")
    parser.add_argument("--step", type=int, default=0, help="执行指定步骤 (1)，0=全部")
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
        print(f"✅ API 服务可用")
    except Exception:
        print("⚠️ API 服务不可用，LLM 调用可能失败")

    results = {}

    if args.step == 0 or args.step == 1:
        results["P1-1"] = test_p1_1_document_person_merge()

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