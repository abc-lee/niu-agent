"""
实体命名格式测试 — 找到 LLM 能正确识别的人物命名格式

测试目标：
1. "未命名人物_1" 作为 entity_name，LLM 提取时能否识别为人物？
2. 改用 "人物_1"、"临时人物_1" 等格式，LLM 能否识别？
3. UUID 作为独立实体，与人物实体关联，是否可行？
4. 文件路径作为独立实体，与人物实体关联，是否可行？

核心原则：不改变 LLM 认识的人物实体结构，UUID 和文件路径作为独立实体与人物实体关联。

前置条件：API 服务器在运行（python -m niu_api）
"""

import sys

# ─── 常量 ───
PERSON_UUID = "20196f76-adfb-49ca-8f99-4402fb84b1d5"
PERSON_NAME = "任飞"
PHOTO_PATH = "REDACTED_WIN_PATH/2026/05/2026-05-08/20090603_092316.jpg"
BRAIN_ENTITY = "brain:niu"

# ─── 工具函数 ───

def get_all_entity_names(rag):
    return set(rag.chunk_entity_relation_graph._graph.nodes)


def get_entity_details(rag, name):
    g = rag.chunk_entity_relation_graph._graph
    name_lower = name.lower()
    if name_lower in g.nodes:
        return dict(g.nodes[name_lower])
    return None


def print_entities(rag, label=""):
    entities = get_all_entity_names(rag)
    print(f"  {label}实体数: {len(entities)}")
    for name in sorted(entities):
        d = get_entity_details(rag, name)
        etype = d.get("entity_type", "?") if d else "?"
        desc = str(d.get("description", ""))[:80] if d else ""
        print(f"    {name[:60]:60s} [{etype:15s}] {desc}")


def find_entities_containing(rag, keyword):
    """查找包含关键词的所有实体"""
    entities = get_all_entity_names(rag)
    return [n for n in entities if keyword.lower() in n.lower()]


def inject_kg(rag, call_async, entities, relationships, label=""):
    """注入 custom_kg (chunks=[])，不触发 LLM"""
    custom_kg = {
        "entities": entities,
        "relationships": relationships,
        "chunks": [],
    }
    try:
        call_async(rag.ainsert_custom_kg(custom_kg), timeout=120)
        print(f"  {label}OK")
        return True
    except Exception as e:
        print(f"  {label}FAIL: {e}")
        return False


def ainsert_text(rag, call_async, text, label=""):
    """通过 ainsert 注入文本，触发 LLM 提取"""
    try:
        call_async(rag.ainsert(text), timeout=300)
        print(f"  {label}OK")
        return True
    except Exception as e:
        print(f"  {label}FAIL: {e}")
        return False


def cleanup_entity(rag, call_async, name):
    """删除指定实体"""
    try:
        call_async(rag.adelete_by_entity(name), timeout=120)
        return True
    except Exception:
        return False


# ─── 测试用例 ───

def test_unnamed_person_via_prompt(rag, call_async):
    """
    测试1: 通过提示词注入告诉 LLM "未命名人物" 是人物实体
    - 在 brain_region_prompt 中注入规则："未命名人物_X 是人物实体的临时名字，属于 person 类型"
    - 注入 "未命名人物_1" 实体
    - ainsert 包含"未命名人物_1"的文本
    - 验证 LLM 是否将其识别为人物并合并到已有实体
    """
    print("\n" + "=" * 60)
    print("测试1: 提示词注入 — 告诉LLM未命名人物是人物实体")
    print("=" * 60)

    # 注意：这个测试需要 brain_region_prompt.py 中已注入相关规则
    # 当前 brain_region_prompt.py 中已有合并规则，但可能没有"未命名人物"的说明
    # 我们先测试当前状态，看已有的提示词是否足够

    test_name = "未命名人物_1"

    # 注入实体
    print(f"  [A] inject_custom_kg 创建 '{test_name}'...")
    ok = inject_kg(rag, call_async,
        entities=[
            {"entity_name": test_name, "entity_type": "person",
             "description": f"一个尚未命名的人物，UUID={PERSON_UUID}"},
        ],
        relationships=[
            {"src_id": BRAIN_ENTITY, "tgt_id": test_name,
             "keywords": "remembers", "description": f"认识{test_name}"},
        ],
        label="")
    if not ok:
        return False

    # ainsert 包含这个名字的文本
    print(f"  [B] ainsert 包含'{test_name}'的文本...")
    text = f"照片中出现了未命名人物_1，未命名人物_1站在西柏坡纪念馆前面，用NIKON D3拍摄。"
    ok = ainsert_text(rag, call_async, text, label="")
    if not ok:
        return False

    # 检查结果
    entities = get_all_entity_names(rag)
    has_original = test_name.lower() in entities
    details = get_entity_details(rag, test_name)
    # 看LLM是否创建了其他变体
    all_entities_list = sorted(entities)
    variants = [n for n in entities if "未命名" in n or "unnamed" in n.lower()]

    print(f"  结果:")
    print(f"    '{test_name}' 存在: {has_original}")
    if details:
        print(f"    entity_type: {details.get('entity_type', '?')}")
        print(f"    description: {str(details.get('description', ''))[:200]}")
    if variants:
        print(f"    包含'未命名'的实体: {variants}")
        for v in variants:
            vd = get_entity_details(rag, v)
            print(f"      {v} [{vd.get('entity_type','?') if vd else '?'}] desc={str(vd.get('description',''))[:100] if vd else ''}")

    # 清理
    cleanup_entity(rag, call_async, test_name)
    for v in variants:
        cleanup_entity(rag, call_async, v)

    return has_original


def test_unnamed_person_format_variants(rag, call_async):
    """
    测试1b: 如果提示词注入不够，测试不同命名格式
    - "人物_1" — 简化格式
    - "临时人物_1" — 带语义的格式
    - "Person_1" — 英文格式
    验证 ainsert 包含这些名字的文本后，LLM 是否识别为人物实体
    """
    print("\n" + "=" * 60)
    print("测试1b: 未命名人物的不同命名格式 — LLM 是否识别为人物")
    print("=" * 60)

    test_names = ["人物_1", "临时人物_1", "Person_1"]

    for i, test_name in enumerate(test_names):
        print(f"\n--- 格式 {i+1}: '{test_name}' ---")

        # 先注入这个实体
        print(f"  [A] inject_custom_kg 创建实体 '{test_name}'...")
        ok = inject_kg(rag, call_async,
            entities=[
                {"entity_name": test_name, "entity_type": "person",
                 "description": f"一个尚未命名的人物，UUID={PERSON_UUID}"},
            ],
            relationships=[
                {"src_id": BRAIN_ENTITY, "tgt_id": test_name,
                 "keywords": "remembers", "description": f"认识{test_name}"},
            ],
            label="")
        if not ok:
            continue

        # 用 ainsert 注入包含这个名字的文本
        print(f"  [B] ainsert 包含'{test_name}'的文本...")
        text = f"照片中出现了{test_name}，{test_name}站在西柏坡纪念馆前面，用NIKON D3拍摄。"
        ok = ainsert_text(rag, call_async, text, label="")
        if not ok:
            continue

        # 检查结果
        entities = get_all_entity_names(rag)
        has_original = test_name.lower() in entities
        details = get_entity_details(rag, test_name)
        variants = [n for n in entities if test_name.lower() != n and
                    any(c in n for c in test_name.replace("_", ""))]

        print(f"  结果:")
        print(f"    '{test_name}' 存在: {has_original}")
        if details:
            print(f"    entity_type: {details.get('entity_type', '?')}")
            print(f"    description: {str(details.get('description', ''))[:100]}")
        if variants:
            print(f"    LLM创建的变体: {variants}")
            for v in variants:
                vd = get_entity_details(rag, v)
                print(f"      {v} [{vd.get('entity_type','?') if vd else '?'}]")

        # 清理
        cleanup_entity(rag, call_async, test_name)
        for v in variants:
            cleanup_entity(rag, call_async, v)

    return True


def test_named_person_merge(rag, call_async):
    """
    测试2: 已命名人物的合并
    - 先注入 "任飞" 作为人物实体（LLM 自然格式）
    - UUID 放在描述里
    - 再 ainsert 包含"任飞"的文本
    - 验证 LLM 是否合并到已有实体
    """
    print("\n" + "=" * 60)
    print("测试2: 已命名人物 '任飞' — LLM 是否合并到已有实体")
    print("=" * 60)

    # 清理
    cleanup_entity(rag, call_async, PERSON_NAME)

    # 用 LLM 自然格式注入 "任飞"
    print(f"  [A] inject_custom_kg 创建 '任飞' (LLM自然格式)...")
    ok = inject_kg(rag, call_async,
        entities=[
            {"entity_name": PERSON_NAME, "entity_type": "person",
             "description": f"任飞，UUID={PERSON_UUID}，用户的朋友"},
        ],
        relationships=[
            {"src_id": BRAIN_ENTITY, "tgt_id": PERSON_NAME,
             "keywords": "remembers", "description": f"认识任飞"},
        ],
        label="")
    if not ok:
        return False

    # ainsert 包含"任飞"的文本
    print(f"  [B] ainsert 包含'任飞'的文本...")
    text = f"任飞和用户一起去了西柏坡旅游，用NIKON D3拍了很多照片。"
    ok = ainsert_text(rag, call_async, text, label="")
    if not ok:
        return False

    # 检查
    entities = get_all_entity_names(rag)
    has_renfei = PERSON_NAME.lower() in entities
    details = get_entity_details(rag, PERSON_NAME)
    # 看是否有UUID在描述中
    has_uuid_in_desc = False
    if details:
        desc = str(details.get("description", ""))
        has_uuid_in_desc = PERSON_UUID in desc

    print(f"  结果:")
    print(f"    '任飞' 存在: {has_renfei}")
    if details:
        print(f"    entity_type: {details.get('entity_type', '?')}")
        print(f"    description: {str(details.get('description', ''))[:200]}")
    print(f"    UUID在描述中: {has_uuid_in_desc}")

    # 清理
    cleanup_entity(rag, call_async, PERSON_NAME)

    return has_renfei


def test_uuid_as_independent_entity(rag, call_async):
    """
    测试3: UUID 作为独立实体，与人物实体关联
    - 注入 "任飞" 人物实体
    - 注入 UUID 独立实体（看 LLM 遇到 UUID 时自己会建什么类型）
    - 建立关联关系
    - ainsert 包含 UUID 的文本，看 LLM 如何处理
    """
    print("\n" + "=" * 60)
    print("测试3: UUID 作为独立实体与人物实体关联")
    print("=" * 60)

    # 清理
    cleanup_entity(rag, call_async, PERSON_NAME)
    cleanup_entity(rag, call_async, PERSON_UUID)

    # 先看 LLM 自己遇到 UUID 会建什么
    print(f"  [A] ainsert 包含UUID的文本 — 看 LLM 自己怎么处理...")
    text = f"人物标识符 {PERSON_UUID} 对应的是任飞，他们一起去了西柏坡。"
    ok = ainsert_text(rag, call_async, text, label="")
    if not ok:
        return False

    entities = get_all_entity_names(rag)
    uuid_related = find_entities_containing(rag, PERSON_UUID[:8])
    renfei_related = find_entities_containing(rag, "任飞")

    print(f"  LLM自己处理的结果:")
    print(f"    包含UUID的实体: {uuid_related}")
    print(f"    包含'任飞'的实体: {renfei_related}")
    for name in uuid_related + renfei_related:
        d = get_entity_details(rag, name)
        if d:
            print(f"      {name} [{d.get('entity_type','?')}] desc={str(d.get('description',''))[:100]}")

    # 清理LLM创建的
    for name in uuid_related + renfei_related:
        cleanup_entity(rag, call_async, name)

    # 现在测试我们主动建 UUID 实体
    print(f"\n  [B] 我们主动建 UUID 实体 + 人物实体 + 关联关系...")
    ok = inject_kg(rag, call_async,
        entities=[
            {"entity_name": PERSON_NAME, "entity_type": "person",
             "description": f"任飞，用户的朋友"},
            {"entity_name": PERSON_UUID, "entity_type": "identifier",
             "description": f"任飞的唯一标识符，对应人物实体'任飞'"},
        ],
        relationships=[
            {"src_id": PERSON_UUID, "tgt_id": PERSON_NAME,
             "keywords": "identifies", "description": f"标识符{PERSON_UUID}对应人物任飞"},
            {"src_id": BRAIN_ENTITY, "tgt_id": PERSON_NAME,
             "keywords": "remembers", "description": "认识任飞"},
        ],
        label="")
    if not ok:
        return False

    # ainsert 包含 UUID 的文本
    print(f"  [C] ainsert 包含UUID的文本 — 看是否合并到已有实体...")
    text = f"系统记录 {PERSON_UUID} 是任飞的标识。"
    ok = ainsert_text(rag, call_async, text, label="")
    if not ok:
        return False

    entities = get_all_entity_names(rag)
    uuid_related2 = find_entities_containing(rag, PERSON_UUID[:8])
    renfei_related2 = find_entities_containing(rag, "任飞")

    print(f"  我们建实体后 ainsert 的结果:")
    print(f"    包含UUID的实体: {uuid_related2}")
    print(f"    包含'任飞'的实体: {renfei_related2}")
    for name in uuid_related2 + renfei_related2:
        d = get_entity_details(rag, name)
        if d:
            print(f"      {name} [{d.get('entity_type','?')}] desc={str(d.get('description',''))[:100]}")

    # 清理
    for name in uuid_related2 + renfei_related2:
        cleanup_entity(rag, call_async, name)

    return True


def test_filepath_as_independent_entity(rag, call_async):
    """
    测试4: 文件路径作为独立实体，与人物实体关联
    - 注入 "任飞" 人物实体
    - 注入文件路径独立实体
    - 建立关联关系
    - ainsert 包含文件路径的文本，看 LLM 如何处理
    """
    print("\n" + "=" * 60)
    print("测试4: 文件路径作为独立实体与人物实体关联")
    print("=" * 60)

    # 清理
    cleanup_entity(rag, call_async, PERSON_NAME)
    # 文件路径可能以多种形式存在
    for variant in [PHOTO_PATH, PHOTO_PATH.replace("/", "\\")]:
        cleanup_entity(rag, call_async, variant)

    # 先看 LLM 自己遇到文件路径会建什么
    print(f"  [A] ainsert 包含文件路径的文本 — 看 LLM 自己怎么处理...")
    text = f"照片 {PHOTO_PATH} 是西柏坡合影，2009年6月3日用NIKON D3拍摄，照片中有任飞。"
    ok = ainsert_text(rag, call_async, text, label="")
    if not ok:
        return False

    entities = get_all_entity_names(rag)
    path_related = [n for n in entities if "20090603" in n or "西柏坡" in n.lower()]
    renfei_related = find_entities_containing(rag, "任飞")

    print(f"  LLM自己处理的结果:")
    print(f"    包含路径/照片的实体: {path_related}")
    print(f"    包含'任飞'的实体: {renfei_related}")
    for name in path_related + renfei_related:
        d = get_entity_details(rag, name)
        if d:
            print(f"      {name[:60]} [{d.get('entity_type','?')}] desc={str(d.get('description',''))[:100]}")

    # 清理LLM创建的
    for name in path_related + renfei_related:
        cleanup_entity(rag, call_async, name)

    # 现在测试我们主动建文件路径实体
    print(f"\n  [B] 我们主动建文件路径实体 + 人物实体 + 关联关系...")
    ok = inject_kg(rag, call_async,
        entities=[
            {"entity_name": PERSON_NAME, "entity_type": "person",
             "description": f"任飞，用户的朋友"},
            {"entity_name": PHOTO_PATH, "entity_type": "photo",
             "description": f"西柏坡合影照片，2009:06:03，NIKON D3"},
        ],
        relationships=[
            {"src_id": PHOTO_PATH, "tgt_id": PERSON_NAME,
             "keywords": "features", "description": f"照片中出现了任飞"},
            {"src_id": BRAIN_ENTITY, "tgt_id": PERSON_NAME,
             "keywords": "remembers", "description": "认识任飞"},
            {"src_id": BRAIN_ENTITY, "tgt_id": PHOTO_PATH,
             "keywords": "remembers", "description": "拥有这张照片"},
        ],
        label="")
    if not ok:
        return False

    # ainsert 包含文件路径的文本
    print(f"  [C] ainsert 包含文件路径的文本 — 看是否合并到已有实体...")
    text = f"照片 {PHOTO_PATH} 中任飞站在西柏坡纪念馆前面。"
    ok = ainsert_text(rag, call_async, text, label="")
    if not ok:
        return False

    entities = get_all_entity_names(rag)
    path_related2 = [n for n in entities if "20090603" in n or "西柏坡" in n.lower()]
    renfei_related2 = find_entities_containing(rag, "任飞")

    print(f"  我们建实体后 ainsert 的结果:")
    print(f"    包含路径/照片的实体: {path_related2}")
    print(f"    包含'任飞'的实体: {renfei_related2}")
    for name in path_related2 + renfei_related2:
        d = get_entity_details(rag, name)
        if d:
            print(f"      {name[:60]} [{d.get('entity_type','?')}] desc={str(d.get('description',''))[:100]}")

    # 清理
    for name in path_related2 + renfei_related2:
        cleanup_entity(rag, call_async, name)

    return True


# ─── 主流程 ───

def run_test():
    print("=" * 60)
    print("实体命名格式测试 — 找到 LLM 能正确识别的格式")
    print("=" * 60)

    print("\n[Init] 初始化 LightRAG...")
    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("  FAIL — 请确保 API 服务器在运行")
        return False
    print("  OK")

    # 逐个测试
    results = {}

    try:
        results["test1_prompt_inject"] = test_unnamed_person_via_prompt(rag, call_async)
    except Exception as e:
        print(f"  测试1异常: {e}")
        results["test1_prompt_inject"] = False

    try:
        results["test1b_format_variants"] = test_unnamed_person_format_variants(rag, call_async)
    except Exception as e:
        print(f"  测试1b异常: {e}")
        results["test1b_format_variants"] = False

    try:
        results["test2_named"] = test_named_person_merge(rag, call_async)
    except Exception as e:
        print(f"  测试2异常: {e}")
        results["test2_named"] = False

    try:
        results["test3_uuid"] = test_uuid_as_independent_entity(rag, call_async)
    except Exception as e:
        print(f"  测试3异常: {e}")
        results["test3_uuid"] = False

    try:
        results["test4_filepath"] = test_filepath_as_independent_entity(rag, call_async)
    except Exception as e:
        print(f"  测试4异常: {e}")
        results["test4_filepath"] = False

    # 汇总
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, result in results.items():
        status = "PASS" if result else "FAIL"
        print(f"  [{status}] {name}")

    return all(results.values())


if __name__ == "__main__":
    success = run_test()
    sys.exit(0 if success else 1)
