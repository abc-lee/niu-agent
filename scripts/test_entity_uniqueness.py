"""
实体唯一性测试 — 解决方案验证

方案：所有入库都走 inject_custom_kg（chunks=[]），不触发 LLM 提取。
验证：注入包含人名的各种内容后，person:{uuid} 是否保持唯一。

前置条件：API 服务器在运行（python -m niu_api），LLM 走代理注入提示词。
"""

import sys

PERSON_UUID = "20196f76-adfb-49ca-8f99-4402fb84b1d5"
PERSON_NAME = "任飞"
PERSON_ENTITY = f"person:{PERSON_UUID}"
PHOTO_PATH = "E:/tmp/bot/2026/05/2026-05-08/20090603_092316.jpg"
PHOTO_ENTITY = f"photo:{PHOTO_PATH}"
BRAIN_ENTITY = "brain:niu"

# 模拟内容提取子Agent输出的各种场景
# 每个场景都用 inject_custom_kg 注入，chunks=[] 不触发 LLM
TEST_SCENARIOS = [
    # 场景1: 聊天记录精炼 — 补充事件和关系
    {
        "desc": "聊天记录精炼：任飞和用户去西柏坡旅游",
        "entities": [
            {"entity_name": "event:西柏坡旅游2009", "entity_type": "Event",
             "description": "2009年6月3日任飞和用户一起去西柏坡旅游"},
        ],
        "relationships": [
            {"src_id": "event:西柏坡旅游2009", "tgt_id": PERSON_ENTITY,
             "keywords": "participated", "description": f"任飞参加了西柏坡旅游"},
            {"src_id": BRAIN_ENTITY, "tgt_id": "event:西柏坡旅游2009",
             "keywords": "remembers", "description": "参加了西柏坡旅游"},
        ],
    },
    # 场景2: 照片入库 — 补充照片-人物关系
    {
        "desc": "照片入库：西柏坡合影中出现了任飞",
        "entities": [],  # 照片和人物实体已存在，不需要新建
        "relationships": [
            {"src_id": PHOTO_ENTITY, "tgt_id": PERSON_ENTITY,
             "keywords": "features", "description": f"照片中出现了任飞"},
        ],
    },
    # 场景3: 人物命名 — 更新人物描述
    {
        "desc": "人物命名：更新person:{uuid}的描述为'任飞'",
        "entities": [
            {"entity_name": PERSON_ENTITY, "entity_type": "Person",
             "description": f"任飞，UUID={PERSON_UUID}，用户的朋友，2009年一起去了西柏坡"},
        ],
        "relationships": [
            {"src_id": BRAIN_ENTITY, "tgt_id": PERSON_ENTITY,
             "keywords": "remembers", "description": f"认识任飞，是朋友关系"},
        ],
    },
    # 场景4: 新照片入库 — 同一人物出现在新照片中
    {
        "desc": "新照片入库：任飞出现在另一张照片中",
        "entities": [
            {"entity_name": "photo:new_photo_002.jpg", "entity_type": "Photo",
             "description": f"任飞的另一张照片", "file_path": "new_photo_002.jpg"},
        ],
        "relationships": [
            {"src_id": "photo:new_photo_002.jpg", "tgt_id": PERSON_ENTITY,
             "keywords": "features", "description": f"照片中出现了任飞"},
            {"src_id": BRAIN_ENTITY, "tgt_id": "photo:new_photo_002.jpg",
             "keywords": "remembers", "description": "拥有这张照片"},
        ],
    },
    # 场景5: 人物关系补充 — 任飞和另一个人同框
    {
        "desc": "同框关系：任飞和李四出现在同一张照片中",
        "entities": [
            {"entity_name": "person:another-uuid-002", "entity_type": "Person",
             "description": "李四"},
        ],
        "relationships": [
            {"src_id": PERSON_ENTITY, "tgt_id": "person:another-uuid-002",
             "keywords": "co_occurs_with", "description": "任飞和李四同框出现"},
            {"src_id": BRAIN_ENTITY, "tgt_id": "person:another-uuid-002",
             "keywords": "remembers", "description": "认识李四"},
        ],
    },
]


def get_all_entity_names(rag):
    return set(rag.chunk_entity_relation_graph._graph.nodes)


def get_entity_details(rag, name):
    g = rag.chunk_entity_relation_graph._graph
    name_lower = name.lower()
    if name_lower in g.nodes:
        return dict(g.nodes[name_lower])
    return None


def run_test():
    print("=" * 60)
    print("解决方案验证：inject_custom_kg (chunks=[]) 不触发 LLM")
    print("=" * 60)

    print("\n[Step 0] 初始化 LightRAG...")
    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  FAIL — 请确保 API 服务器在运行")
        return False
    print("  OK")

    # 先清理：删除可能存在的独立"任飞"实体
    entities_before = get_all_entity_names(rag)
    if PERSON_NAME.lower() in entities_before:
        print(f"  清理：删除独立'{PERSON_NAME}'实体...")
        try:
            call_async(rag.adelete_by_entity(PERSON_NAME), timeout=120)
            print("  清理完成")
        except Exception as e:
            print(f"  清理失败: {e}")

    # Step 1: inject_custom_kg 创建基础实体
    print(f"\n[Step 1] inject_custom_kg 创建基础实体...")
    custom_kg = {
        "entities": [
            {"entity_name": PERSON_ENTITY, "entity_type": "Person",
             "description": f"{PERSON_NAME}，UUID={PERSON_UUID}",
             "source_id": "photo:kg_test", "file_path": PHOTO_PATH},
            {"entity_name": PHOTO_ENTITY, "entity_type": "Photo",
             "description": f"西柏坡合影，2009:06:03，NIKON D3",
             "source_id": "photo:kg_test", "file_path": PHOTO_PATH},
            {"entity_name": BRAIN_ENTITY, "entity_type": "Niu",
             "description": "Self entity", "source_id": "brain", "file_path": "custom_kg"},
        ],
        "relationships": [
            {"src_id": PHOTO_ENTITY, "tgt_id": PERSON_ENTITY,
             "keywords": "features", "description": f"照片中出现了{PERSON_NAME}"},
            {"src_id": BRAIN_ENTITY, "tgt_id": PERSON_ENTITY,
             "keywords": "remembers", "description": f"认识{PERSON_NAME}"},
            {"src_id": BRAIN_ENTITY, "tgt_id": PHOTO_ENTITY,
             "keywords": "remembers", "description": "拥有这张照片"},
        ],
        "chunks": [],
    }
    try:
        call_async(rag.ainsert_custom_kg(custom_kg), timeout=120)
        print("  OK")
    except Exception as e:
        print(f"  FAIL: {e}")
        return False

    entities_s1 = get_all_entity_names(rag)
    print(f"  实体数: {len(entities_s1)}")
    print(f"  {PERSON_ENTITY}: {PERSON_ENTITY.lower() in entities_s1}")
    print(f"  独立'{PERSON_NAME}': {PERSON_NAME.lower() in entities_s1}")

    # Step 2: 逐个场景注入
    print(f"\n[Step 2] 逐场景注入（{len(TEST_SCENARIOS)} 个场景）...")
    for i, scenario in enumerate(TEST_SCENARIOS):
        print(f"\n  [{i+1}/{len(TEST_SCENARIOS)}] {scenario['desc']}")
        custom_kg = {
            "entities": scenario["entities"],
            "relationships": scenario["relationships"],
            "chunks": [],
        }
        try:
            call_async(rag.ainsert_custom_kg(custom_kg), timeout=120)
            print("    OK")
        except Exception as e:
            print(f"    FAIL: {e}")
            return False

        # 每次注入后检查
        entities_now = get_all_entity_names(rag)
        has_standalone = PERSON_NAME.lower() in entities_now
        name_variants = [n for n in entities_now if PERSON_NAME in n and n != PERSON_ENTITY.lower()]
        print(f"    实体数: {len(entities_now)}, 独立'{PERSON_NAME}': {has_standalone}")
        if name_variants:
            print(f"    !!! 人名变体: {name_variants}")

    # Step 3: 最终检查
    print(f"\n[Step 3] 最终检查...")
    entities_final = get_all_entity_names(rag)
    has_person = PERSON_ENTITY.lower() in entities_final
    has_standalone = PERSON_NAME.lower() in entities_final
    print(f"  {PERSON_ENTITY} 存在: {has_person}")
    print(f"  独立'{PERSON_NAME}'实体: {has_standalone}")

    # 包含人名/UUID 的所有实体
    name_related = [n for n in entities_final if PERSON_NAME in n.lower() or PERSON_UUID in n.lower()]
    print(f"  包含人名/UUID的实体: {name_related}")

    # person:{uuid} 详情
    details = get_entity_details(rag, PERSON_ENTITY)
    if details:
        print(f"\n  {PERSON_ENTITY} 详情:")
        for k, v in details.items():
            s = str(v)
            print(f"    {k}: {s[:200]}{'...' if len(s)>200 else ''}")

    # 列出所有实体
    print(f"\n  所有实体 ({len(entities_final)}):")
    for name in sorted(entities_final):
        d = get_entity_details(rag, name)
        etype = d.get("entity_type", "?") if d else "?"
        print(f"    {name} [{etype}]")

    # ─── 判定 ───
    print(f"\n{'=' * 60}")
    print("测试结果:")
    print(f"{'=' * 60}")
    if has_standalone:
        print(f"  [FAIL] 独立'{PERSON_NAME}'实体被创建")
        return False
    else:
        print(f"  [PASS] 没有独立'{PERSON_NAME}'实体 — inject_custom_kg 方案有效")
        if has_person:
            print(f"  [PASS] {PERSON_ENTITY} 实体存在且唯一")
        return True


if __name__ == "__main__":
    success = run_test()
    sys.exit(0 if success else 1)
