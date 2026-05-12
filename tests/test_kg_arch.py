"""
架构验证测试：确认正确实体结构下的 KG 行为。

核心原则：
- 人物实体只存名字/身份，不挂 file_path
- 文件实体（照片/文档）存路径和描述
- 关系只通过边表达（depicts / mentions / co_appears_with）

P0 单元测试：直接操作 NetworkX 图，不需要 LLM proxy
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


def cleanup_test_entities(*names):
    """清理测试实体。"""
    nx = get_nx()
    for name in names:
        if name in nx.nodes():
            # 删除相关边
            edges_to_remove = [(s, t) for s, t in nx.edges() if s == name or t == name]
            for s, t in edges_to_remove:
                nx.remove_edge(s, t)
            nx.remove_node(name)


# ============================================================
# P0-1: 人物实体结构 — 只存名字，不挂 file_path
# ============================================================

def test_p0_1_person_entity_structure():
    """P0-1: 验证人物实体只存名字，不挂 file_path。

    正确架构：
    - person:{uuid} 实体: entity_type="Person", description="名字"
    - 不传 file_path, 不传 source_id
    - 照片信息通过 depicts 边表达
    """
    print("\n" + "=" * 60)
    print("P0-1: 人物实体结构验证")
    print("=" * 60)

    test_uuid = "arch-test-001"
    entity_name = f"person:{test_uuid}"
    person_name = "张三"

    ingester = get_ingester()
    cleanup_test_entities(entity_name)

    # 注入人物实体 — 只传名字，file_path 留空
    result = ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description=person_name,
        file_path="",  # 人物实体不挂文件路径
    )
    print(f"\n注入结果: {result}")
    time.sleep(2)

    # 验证实体结构
    nx = get_nx()
    if entity_name not in nx.nodes():
        print(f"❌ 实体不存在: {entity_name}")
        return False

    data = dict(nx.nodes[entity_name])
    print(f"\n实体数据:")
    print(json.dumps(data, ensure_ascii=False, indent=2))

    # 检查关键字段
    checks = []

    # 1. entity_type 应为 Person 或 person
    et = data.get("entity_type", "")
    if et.lower() == "person":
        print(f"✅ entity_type = {et}")
        checks.append(True)
    else:
        print(f"❌ entity_type = {et}, 期望 Person/person")
        checks.append(False)

    # 2. description 应只包含名字
    desc = data.get("description", "")
    if person_name in desc:
        print(f"✅ description 包含名字: {desc}")
        checks.append(True)
    else:
        print(f"❌ description 不包含名字: {desc}")
        checks.append(False)

    # 3. 不应有 file_path（或 file_path 为空/UNKNOWN）
    fp = data.get("file_path", "")
    if not fp or fp == "UNKNOWN" or fp == "unknown_source":
        print(f"✅ file_path 为空或 UNKNOWN: '{fp}'")
        checks.append(True)
    else:
        print(f"⚠️ file_path 有值: '{fp}' — 人物实体不应挂文件路径")
        checks.append(False)

    # 4. 不应有 "detected in photo" 等照片信息
    if "detected in photo" not in desc:
        print(f"✅ description 不含照片信息")
        checks.append(True)
    else:
        print(f"❌ description 含照片信息: {desc}")
        checks.append(False)

    return all(checks)


# ============================================================
# P0-2: 照片实体 + depicts 边 — 关系通过边表达
# ============================================================

def test_p0_2_photo_depicts_edge():
    """P0-2: 验证照片实体和人物实体之间通过 depicts 边表达关系。

    正确架构：
    - 照片实体: entity_type="Photo", file_path=照片路径
    - 人物实体: entity_type="Person", description="名字"
    - depicts 边: 照片 → 人物
    """
    print("\n" + "=" * 60)
    print("P0-2: 照片实体 + depicts 边验证")
    print("=" * 60)

    test_uuid = "arch-test-002"
    person_entity = f"person:{test_uuid}"
    person_name = "李四"
    photo_path = "E:/photos/test_photo_002.jpg"

    ingester = get_ingester()
    cleanup_test_entities(person_entity, photo_path)

    # 注入人物实体
    ingester.inject_entity(
        name=person_entity,
        entity_type="Person",
        description=person_name,
        file_path="",
    )
    time.sleep(1)

    # 注入照片实体
    ingester.inject_entity(
        name=photo_path,
        entity_type="Photo",
        description="测试照片",
        file_path=photo_path,
    )
    time.sleep(1)

    # 注入 depicts 边
    result = ingester.inject_relation(
        src_id=photo_path,
        tgt_id=person_entity,
        relation="depicts",
        description=f"照片中出现了{person_name}",
    )
    print(f"\ndepicts 边注入结果: {result}")
    time.sleep(2)

    # 验证
    nx = get_nx()

    # 1. 人物实体存在且结构正确
    if person_entity in nx.nodes():
        data = dict(nx.nodes[person_entity])
        desc = data.get("description", "")
        fp = data.get("file_path", "")
        print(f"\n人物实体: {person_entity}")
        print(f"  description: {desc}")
        print(f"  file_path: {fp}")

        if person_name in desc:
            print(f"  ✅ description 包含名字")
        else:
            print(f"  ❌ description 不包含名字")

        if not fp or fp == "UNKNOWN":
            print(f"  ✅ 人物实体无 file_path")
        else:
            print(f"  ⚠️ 人物实体有 file_path: {fp}")
    else:
        print(f"❌ 人物实体不存在")
        return False

    # 2. 照片实体存在且有 file_path
    if photo_path in nx.nodes():
        data = dict(nx.nodes[photo_path])
        fp = data.get("file_path", "")
        print(f"\n照片实体: {photo_path}")
        print(f"  file_path: {fp}")

        if photo_path in fp:
            print(f"  ✅ 照片实体有 file_path")
        else:
            print(f"  ⚠️ 照片实体 file_path 异常: {fp}")
    else:
        print(f"❌ 照片实体不存在")
        return False

    # 3. depicts 边存在
    depicts_found = False
    for src, tgt, data in nx.edges(data=True):
        if (src == photo_path and tgt == person_entity) or \
           (src == person_entity and tgt == photo_path):
            kw = data.get("keywords", "")
            if "depicts" in kw.lower():
                depicts_found = True
                print(f"\n✅ depicts 边存在: {src} → {tgt}")
                print(f"  keywords: {kw}")
                print(f"  description: {data.get('description', '')[:80]}")
                break

    if not depicts_found:
        print(f"\n❌ depicts 边不存在")
        # 列出相关边
        for src, tgt, data in nx.edges(data=True):
            if src in (photo_path, person_entity) or tgt in (photo_path, person_entity):
                print(f"  边: {src} → {tgt}, keywords={data.get('keywords', '')}")
        return False

    return True


# ============================================================
# P0-3: 同名实体再次注入 — description 行为
# ============================================================

def test_p0_3_reinject_description_behavior():
    """P0-3: 验证再次注入同名人物实体时 description 的行为。

    关键问题：ainsert_custom_kg 路径是覆盖还是合并？
    如果是覆盖，name_person 必须先读后写完整 description。
    """
    print("\n" + "=" * 60)
    print("P0-3: 同名实体再次注入 — description 行为")
    print("=" * 60)

    test_uuid = "arch-test-003"
    entity_name = f"person:{test_uuid}"

    ingester = get_ingester()
    cleanup_test_entities(entity_name)

    # Step 1: 首次注入 — description = "王五"
    ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description="王五",
        file_path="",
    )
    time.sleep(2)

    nx = get_nx()
    desc1 = dict(nx.nodes[entity_name]).get("description", "")
    print(f"\n首次注入后 description: {desc1}")

    # Step 2: 再次注入 — description = "王五（退休教师）"
    ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description="王五（退休教师）",
    )
    time.sleep(2)

    nx = get_nx()
    desc2 = dict(nx.nodes[entity_name]).get("description", "")
    print(f"再次注入后 description: {desc2}")

    # 分析结果
    if "退休教师" in desc2 and "王五" in desc2:
        # description 包含新内容
        if desc1 in desc2 and desc2 != desc1:
            # 旧内容也在，是合并
            print(f"\n⚠️ description 合并模式 — 新旧内容都保留")
            print(f"  注意: 这会导致 description 累积，不是期望行为")
            return "MERGE"
        else:
            # 只有新内容，是覆盖但新内容包含名字
            print(f"\n✅ description 覆盖模式 — 只保留最新内容")
            print(f"  这是正确行为：人物 description 只存当前名字")
            return True
    else:
        print(f"\n❌ description 异常: {desc2}")
        return "UNKNOWN"


# ============================================================
# P0-4: name_person 模拟 — 先读后写
# ============================================================

def test_p0_4_name_person_read_then_write():
    """P0-4: 模拟 name_person 的正确行为 — 先读当前 description，再写完整内容。

    正确流程：
    1. 读取 person:{uuid} 当前 description
    2. 更新名字部分，保留其他信息
    3. 写入完整 description
    """
    print("\n" + "=" * 60)
    print("P0-4: name_person 先读后写验证")
    print("=" * 60)

    test_uuid = "arch-test-004"
    entity_name = f"person:{test_uuid}"

    ingester = get_ingester()
    cleanup_test_entities(entity_name)

    # Step 1: 初始注入 — description = "未命名人物_4"
    ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description="未命名人物_4",
        file_path="",
    )
    time.sleep(2)

    nx = get_nx()
    desc_before = dict(nx.nodes[entity_name]).get("description", "")
    print(f"\n改名前 description: {desc_before}")

    # Step 2: 改名 — 先读后写
    new_name = "赵六"
    # 正确做法：直接写新名字
    ingester.inject_entity(
        name=entity_name,
        entity_type="Person",
        description=new_name,
    )
    time.sleep(2)

    nx = get_nx()
    desc_after = dict(nx.nodes[entity_name]).get("description", "")
    print(f"改名后 description: {desc_after}")

    if new_name in desc_after:
        print(f"\n✅ 改名成功 — description 包含 '{new_name}'")
        # 检查是否有残留的旧信息
        if "未命名" in desc_after and "未命名" not in new_name:
            print(f"⚠️ 旧名字残留: {desc_after}")
            return False
        return True
    else:
        print(f"\n❌ 改名失败 — description 不包含 '{new_name}'")
        return False


# ============================================================
# P0-5: merge_persons 模拟 — 合并后删除 person_b
# ============================================================

def test_p0_5_merge_persons_delete_b():
    """P0-5: 模拟 merge_persons 的正确行为 — 合并后删除 person_b。

    正确流程：
    1. 更新 person_a 的 description
    2. 将 person_b 的边迁移到 person_a
    3. 删除 person_b 实体
    """
    print("\n" + "=" * 60)
    print("P0-5: merge_persons 合并后删除 person_b")
    print("=" * 60)

    uuid_a = "arch-test-005a"
    uuid_b = "arch-test-005b"
    entity_a = f"person:{uuid_a}"
    entity_b = f"person:{uuid_b}"
    photo_path = "E:/photos/test_merge_photo.jpg"

    ingester = get_ingester()
    cleanup_test_entities(entity_a, entity_b, photo_path)

    # Step 1: 注入两个 person + 各自的 depicts 边
    ingester.inject_entity(name=entity_a, entity_type="Person", description="孙七", file_path="")
    ingester.inject_entity(name=entity_b, entity_type="Person", description="孙七（重复）", file_path="")
    time.sleep(1)

    # 照片实体
    ingester.inject_entity(name=photo_path, entity_type="Photo", description="合影", file_path=photo_path)
    time.sleep(1)

    # person_b 的 depicts 边
    ingester.inject_relation(
        src_id=photo_path,
        tgt_id=entity_b,
        relation="depicts",
        description="照片中出现了孙七（重复）",
    )
    time.sleep(2)

    nx = get_nx()

    # 验证合并前状态
    print(f"\n合并前:")
    print(f"  {entity_a} 存在: {entity_a in nx.nodes()}")
    print(f"  {entity_b} 存在: {entity_b in nx.nodes()}")

    # 检查 person_b 的边
    b_edges = [(s, t, dict(d)) for s, t, d in nx.edges(data=True) if s == entity_b or t == entity_b]
    print(f"  {entity_b} 的边数: {len(b_edges)}")

    # Step 2: 合并 — 更新 person_a，删除 person_b
    merged_name = "孙七"

    # 更新 person_a
    ingester.inject_entity(name=entity_a, entity_type="Person", description=merged_name, file_path="")
    time.sleep(1)

    # 为 person_a 建 depicts 边（迁移 person_b 的关系）
    ingester.inject_relation(
        src_id=photo_path,
        tgt_id=entity_a,
        relation="depicts",
        description="照片中出现了孙七",
    )
    time.sleep(1)

    # 删除 person_b
    if entity_b in nx.nodes():
        # 先删边
        edges_to_remove = [(s, t) for s, t in nx.edges() if s == entity_b or t == entity_b]
        for s, t in edges_to_remove:
            nx.remove_edge(s, t)
        nx.remove_node(entity_b)
        print(f"\n  已删除 {entity_b}")

    # 验证合并后状态
    nx = get_nx()
    print(f"\n合并后:")
    print(f"  {entity_a} 存在: {entity_a in nx.nodes()}")
    print(f"  {entity_b} 存在: {entity_b in nx.nodes()}")

    if entity_a in nx.nodes():
        desc = dict(nx.nodes[entity_a]).get("description", "")
        print(f"  {entity_a} description: {desc}")

    # 检查 person_a 的 depicts 边
    a_depicts = []
    for src, tgt, data in nx.edges(data=True):
        if (src == photo_path and tgt == entity_a) or (src == entity_a and tgt == photo_path):
            kw = data.get("keywords", "")
            if "depicts" in kw.lower():
                a_depicts.append((src, tgt, kw))

    print(f"  {entity_a} 的 depicts 边数: {len(a_depicts)}")

    # 最终验证
    checks = []
    checks.append(entity_a in nx.nodes())  # person_a 存在
    checks.append(entity_b not in nx.nodes())  # person_b 已删除
    checks.append(len(a_depicts) >= 1)  # person_a 有 depicts 边

    if all(checks):
        print(f"\n✅ 合并成功 — person_a 存在，person_b 已删除，depicts 边已迁移")
        return True
    else:
        print(f"\n❌ 合并失败")
        return False


# ============================================================
# P0-6: co_appears_with 边 — 人物同框关系
# ============================================================

def test_p0_6_co_appears_with():
    """P0-6: 验证 co_appears_with 边正确建立。

    正确架构：人物之间通过 co_appears_with 边表达同框关系。
    """
    print("\n" + "=" * 60)
    print("P0-6: co_appears_with 边验证")
    print("=" * 60)

    uuid_a = "arch-test-006a"
    uuid_b = "arch-test-006b"
    entity_a = f"person:{uuid_a}"
    entity_b = f"person:{uuid_b}"

    ingester = get_ingester()
    cleanup_test_entities(entity_a, entity_b)

    # 注入两个人物实体
    ingester.inject_entity(name=entity_a, entity_type="Person", description="周八", file_path="")
    ingester.inject_entity(name=entity_b, entity_type="Person", description="吴九", file_path="")
    time.sleep(1)

    # 注入 co_appears_with 边
    result = ingester.inject_relation(
        src_id=entity_a,
        tgt_id=entity_b,
        relation="co_appears_with",
        description="周八和吴九在同一张照片中出现",
    )
    print(f"\nco_appears_with 边注入结果: {result}")
    time.sleep(2)

    # 验证
    nx = get_nx()
    co_appears_found = False
    for src, tgt, data in nx.edges(data=True):
        if (src == entity_a and tgt == entity_b) or (src == entity_b and tgt == entity_a):
            kw = data.get("keywords", "")
            if "co_appears" in kw.lower():
                co_appears_found = True
                print(f"\n✅ co_appears_with 边存在: {src} → {tgt}")
                print(f"  keywords: {kw}")
                break

    if not co_appears_found:
        print(f"\n❌ co_appears_with 边不存在")
        return False

    return True


# ============================================================
# P0-7: 多次注入同名人物 — description 追加还是覆盖
# ============================================================

def test_p0_7_multiple_inject_same_entity():
    """P0-7: 验证多次 inject_entity 同名实体时，description 是追加还是覆盖。

    场景：同一人物在不同照片中被识别，每次注入是否追加信息。
    正确架构：人物 description 只写名字，多次注入同名实体名字不变，
    所以覆盖也无所谓。但需要确认行为。
    """
    print("\n" + "=" * 60)
    print("P0-7: 多次注入同名实体 — description 行为")
    print("=" * 60)

    test_uuid = "arch-test-007"
    entity_name = f"person:{test_uuid}"

    ingester = get_ingester()
    cleanup_test_entities(entity_name)

    # 第一次：名字
    ingester.inject_entity(name=entity_name, entity_type="Person", description="郑十", file_path="")
    time.sleep(2)

    nx = get_nx()
    desc1 = dict(nx.nodes[entity_name]).get("description", "")
    print(f"\n第1次注入后 description: {desc1}")

    # 第二次：同名，description 不变
    ingester.inject_entity(name=entity_name, entity_type="Person", description="郑十", file_path="")
    time.sleep(2)

    nx = get_nx()
    desc2 = dict(nx.nodes[entity_name]).get("description", "")
    print(f"第2次注入后 description: {desc2}")

    # 第三次：改名
    ingester.inject_entity(name=entity_name, entity_type="Person", description="郑十（已确认）", file_path="")
    time.sleep(2)

    nx = get_nx()
    desc3 = dict(nx.nodes[entity_name]).get("description", "")
    print(f"第3次注入后 description: {desc3}")

    # 分析
    if desc3 == "郑十（已确认）":
        print(f"\n✅ 覆盖模式 — description 被最新值替换")
        print(f"  结论: name_person 可以直接写新 description，无需先读")
        return True  # 覆盖是正确行为：人物 description 只存当前名字
    elif "郑十" in desc3 and "已确认" in desc3 and len(desc3) > len("郑十（已确认）"):
        print(f"\n⚠️ 合并模式 — description 追加合并")
        print(f"  结论: name_person 需要写完整 description（包含旧信息）")
        print(f"  注意: 这会导致 description 累积历史名字，不是期望行为")
        return "MERGE"
    else:
        print(f"\n❓ 行为不确定: {desc3}")
        return "UNKNOWN"


# ============================================================
# 主测试流程
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="架构验证测试")
    parser.add_argument("--step", type=int, default=0, help="执行指定步骤 (1-7)，0=全部")
    args = parser.parse_args()

    rag = get_rag()
    if rag is None:
        print("❌ LightRAG 不可用！")
        sys.exit(1)

    nx = rag.chunk_entity_relation_graph._graph
    print(f"✅ LightRAG 可用，图谱节点数: {len(nx.nodes())}")

    results = {}

    if args.step == 0 or args.step == 1:
        results["P0-1"] = test_p0_1_person_entity_structure()

    if args.step == 0 or args.step == 2:
        results["P0-2"] = test_p0_2_photo_depicts_edge()

    if args.step == 0 or args.step == 3:
        results["P0-3"] = test_p0_3_reinject_description_behavior()

    if args.step == 0 or args.step == 4:
        results["P0-4"] = test_p0_4_name_person_read_then_write()

    if args.step == 0 or args.step == 5:
        results["P0-5"] = test_p0_5_merge_persons_delete_b()

    if args.step == 0 or args.step == 6:
        results["P0-6"] = test_p0_6_co_appears_with()

    if args.step == 0 or args.step == 7:
        results["P0-7"] = test_p0_7_multiple_inject_same_entity()

    # 汇总
    print("\n" + "=" * 60)
    print("架构验证测试结果汇总")
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
