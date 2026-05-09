"""
实体唯一性测试 — inject_custom_kg 带 chunks 时的行为

关键问题：inject_custom_kg 带 chunks 会触发 LLM 提取额外实体/关系，
LLM 是否会把"任飞"提取为独立实体？

前置条件：API 服务器在运行（python -m niu_api），LLM 走代理注入提示词。
"""

import sys

PERSON_UUID = "20196f76-adfb-49ca-8f99-4402fb84b1d5"
PERSON_NAME = "任飞"
PERSON_ENTITY = f"person:{PERSON_UUID}"
PHOTO_PATH = "REDACTED_WIN_PATH/2026/05/2026-05-08/20090603_092316.jpg"
PHOTO_ENTITY = f"photo:{PHOTO_PATH}"
BRAIN_ENTITY = "brain:niu"


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
    print("inject_custom_kg 带 chunks 测试 — LLM 是否会创建独立人名实体")
    print("=" * 60)

    print("\n[Step 0] 初始化...")
    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  FAIL — 请确保 API 服务器在运行")
        return False
    print("  OK")

    # 清理独立"任飞"实体
    entities_before = get_all_entity_names(rag)
    if PERSON_NAME.lower() in entities_before:
        print(f"  清理独立'{PERSON_NAME}'实体...")
        try:
            call_async(rag.adelete_by_entity(PERSON_NAME), timeout=120)
        except Exception as e:
            print(f"  清理失败: {e}")

    # Step 1: inject_custom_kg 带 chunks（触发 LLM 提取）
    print(f"\n[Step 1] inject_custom_kg 带 chunks — 触发 LLM 提取...")
    print(f"  chunks 中包含'任飞'，LLM 可能提取为独立实体")

    custom_kg = {
        "entities": [
            {"entity_name": PERSON_ENTITY, "entity_type": "Person",
             "description": f"{PERSON_NAME}，UUID={PERSON_UUID}，出现在照片{PHOTO_PATH}中",
             "source_id": "photo:kg_test", "file_path": PHOTO_PATH},
            {"entity_name": PHOTO_ENTITY, "entity_type": "Photo",
             "description": f"西柏坡合影，2009:06:03，NIKON D3，出现了{PERSON_NAME}",
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
        "chunks": [
            {
                "content": f"照片 20090603_092316: 西柏坡合影，2009:06:03，NIKON D3，出现了任飞。任飞是用户的朋友，2009年一起出游西柏坡。",
                "source_id": PHOTO_ENTITY,
                "file_path": PHOTO_PATH,
            },
        ],
    }

    try:
        call_async(rag.ainsert_custom_kg(custom_kg), timeout=300)
        print("  OK")
    except Exception as e:
        print(f"  FAIL: {e}")
        return False

    entities_s1 = get_all_entity_names(rag)
    has_person = PERSON_ENTITY.lower() in entities_s1
    has_standalone = PERSON_NAME.lower() in entities_s1
    name_variants = [n for n in entities_s1 if PERSON_NAME in n and n != PERSON_ENTITY.lower()]

    print(f"  实体数: {len(entities_s1)}")
    print(f"  {PERSON_ENTITY} 存在: {has_person}")
    print(f"  独立'{PERSON_NAME}'实体: {has_standalone}")
    if name_variants:
        print(f"  !!! 人名变体: {name_variants}")

    # Step 2: 再注入一条带 chunks 的内容
    print(f"\n[Step 2] 第二次 inject_custom_kg 带 chunks...")
    custom_kg2 = {
        "entities": [],
        "relationships": [
            {"src_id": BRAIN_ENTITY, "tgt_id": PERSON_ENTITY,
             "keywords": "remembers", "description": f"认识任飞，是朋友关系"},
        ],
        "chunks": [
            {
                "content": f"任飞和用户一起去了西柏坡，用NIKON D3拍了很多照片。person:{PERSON_UUID}是任飞在系统中的标识。",
                "source_id": f"chat:任飞_test",
                "file_path": "chat:任飞_test",
            },
        ],
    }

    try:
        call_async(rag.ainsert_custom_kg(custom_kg2), timeout=300)
        print("  OK")
    except Exception as e:
        print(f"  FAIL: {e}")
        return False

    entities_s2 = get_all_entity_names(rag)
    has_standalone2 = PERSON_NAME.lower() in entities_s2
    name_variants2 = [n for n in entities_s2 if PERSON_NAME in n and n != PERSON_ENTITY.lower()]

    print(f"  实体数: {len(entities_s2)}")
    print(f"  独立'{PERSON_NAME}'实体: {has_standalone2}")
    if name_variants2:
        print(f"  !!! 人名变体: {name_variants2}")

    # Step 3: 最终检查
    print(f"\n[Step 3] 最终检查...")
    entities_final = get_all_entity_names(rag)
    has_person = PERSON_ENTITY.lower() in entities_final
    has_standalone = PERSON_NAME.lower() in entities_final
    name_related = [n for n in entities_final if PERSON_NAME in n.lower() or PERSON_UUID in n.lower()]
    print(f"  {PERSON_ENTITY} 存在: {has_person}")
    print(f"  独立'{PERSON_NAME}'实体: {has_standalone}")
    print(f"  包含人名/UUID的实体: {name_related}")

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
        print(f"  [FAIL] inject_custom_kg 带 chunks 时，LLM 创建了独立'{PERSON_NAME}'实体")
        print(f"  结论: chunks=[] 是必须的，不能带 chunks")
        return False
    else:
        print(f"  [PASS] inject_custom_kg 带 chunks 也没有创建独立'{PERSON_NAME}'实体")
        print(f"  结论: inject_custom_kg 的显式实体优先，LLM 提取不会覆盖")
        return True


if __name__ == "__main__":
    success = run_test()
    sys.exit(0 if success else 1)
