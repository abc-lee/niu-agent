"""
TDD 测试脚本: KG 照片入库/改名 3 个 Bug

Bug 1: "未命名人物"残留 — sync_photo_to_kg Path 1 content 包含人物名称
Bug 2: 改名不清理旧 KG 实体 — name_person 不合并旧 auto_label 实体
Bug 3: Photo entityType="Other" — LLM 自动提取不识别 Photo 类型

测试照片: E:\\tmp\\2009.6.4西柏坡

TDD 流程: RED → GREEN → IMPROVE

直接调用 Python 函数（不通过 ToolRegistry call_tool 包装器），
因为 call_tool 返回 list[TextContent] 而非 dict，不适合断言。
"""

import sys
import os
import uuid
import time
import json

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# MCP server workdirs 加入 sys.path
MCP_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "mcp-servers.yaml")
if os.path.exists(MCP_CONFIG_PATH):
    import yaml
    with open(MCP_CONFIG_PATH, "r", encoding="utf-8") as f:
        mcp_config = yaml.safe_load(f) or {}
    for server_name, server_conf in mcp_config.items():
        if isinstance(server_conf, dict) and "workdir" in server_conf:
            workdir = os.path.normpath(os.path.join(PROJECT_ROOT, server_conf["workdir"]))
            if os.path.exists(workdir) and workdir not in sys.path:
                sys.path.insert(0, workdir)

TEST_PHOTO_DIR = r"E:\tmp\2009.6.4西柏坡"

_lightrag_inited = False


def _init_lightrag():
    """初始化 LightRAG + ToolRegistry（只做一次）"""
    global _lightrag_inited
    if _lightrag_inited:
        return

    # 1. 初始化 LightRAG
    from niu_api.internal.lightrag_manager import get_lightrag
    rag = get_lightrag()
    if rag is None:
        print("ERROR: LightRAG not available, cannot run tests")
        sys.exit(1)
    print(f"[INIT] LightRAG initialized")

    # 2. 初始化 ToolRegistry（sync_photo_to_kg 依赖 registry 中的 lightrag 工具）
    from agent.mcp_loader import load_mcp_tools
    registry = load_mcp_tools()
    print(f"[INIT] ToolRegistry loaded: {len(registry._tools)} tools")

    _lightrag_inited = True


def _get_test_photo():
    """获取一张测试照片路径"""
    if not os.path.exists(TEST_PHOTO_DIR):
        print(f"ERROR: Test photo directory not found: {TEST_PHOTO_DIR}")
        sys.exit(1)

    photos = [f for f in os.listdir(TEST_PHOTO_DIR)
              if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    if not photos:
        print(f"ERROR: No photos found in {TEST_PHOTO_DIR}")
        sys.exit(1)

    return os.path.join(TEST_PHOTO_DIR, photos[0])


def _list_kg_entities(entity_type_filter=""):
    """查询 KG 中的实体列表"""
    from niu_api.internal.lightrag_adapter import LightRAGAdapter
    adapter = LightRAGAdapter()
    result = adapter.list_entities(
        list_type="entities",
        entity_type=entity_type_filter,
        limit=200,
    )
    if result.get("status") != "ok":
        print(f"WARNING: list_entities failed: {result}")
        return []
    return result.get("data", [])


def _delete_kg_entity(entity_name):
    """删除 KG 实体"""
    from niu_api.internal.lightrag_adapter import LightRAGAdapter
    adapter = LightRAGAdapter()
    return adapter.delete_entity(entity_name)


def _merge_kg_entities(source_entities, target_entity):
    """合并 KG 实体"""
    from niu_api.internal.lightrag_adapter import LightRAGAdapter
    adapter = LightRAGAdapter()
    return adapter.merge_entities(source_entities=source_entities, target_entity=target_entity)


def _cleanup_test_entities(test_file_path, test_person_id, auto_label=""):
    """清理测试产生的 KG 实体"""
    # 删除照片实体
    photo_entity = f"photo:{test_file_path}"
    try:
        _delete_kg_entity(photo_entity)
        print(f"[CLEANUP] Deleted photo entity: {photo_entity}")
    except Exception as e:
        print(f"[CLEANUP] Failed to delete photo entity: {e}")

    # 删除人物实体
    person_entity = f"person:{test_person_id}"
    try:
        _delete_kg_entity(person_entity)
        print(f"[CLEANUP] Deleted person entity: {person_entity}")
    except Exception as e:
        print(f"[CLEANUP] Failed to delete person entity: {e}")

    # 删除可能的"未命名人物"实体
    if auto_label:
        try:
            _delete_kg_entity(auto_label)
            print(f"[CLEANUP] Deleted auto_label entity: {auto_label}")
        except Exception as e:
            print(f"[CLEANUP] Failed to delete {auto_label}: {e}")


# ============== Test 1: ingest_photo — KG 实体正确性 (Bug 1 + Bug 3) ==============

def test_ingest_photo_kg_entities():
    """RED: 验证照片入库后 KG 中:
    1. 无"未命名人物"残留实体
    2. 照片实体 entityType="Photo"
    3. 人物实体为 person:{uuid} 格式
    """
    print("\n" + "=" * 60)
    print("TEST 1: ingest_photo KG entities (Bug 1 + Bug 3)")
    print("=" * 60)

    _init_lightrag()
    test_photo = _get_test_photo()
    print(f"[TEST] Using photo: {test_photo}")

    # 1. 直接调用 ingest_photo
    from niu_photo_server import ingest_photo
    result = ingest_photo(file_path=test_photo)
    print(f"[TEST] ingest_photo result: status={result.get('status')}")

    if result.get("status") != "success":
        print(f"FAIL: ingest_photo failed: {result}")
        return False

    test_file_path = result.get("file_path", "")
    detected_persons = result.get("detected_persons", [])
    test_person_id = detected_persons[0]["id"] if detected_persons else ""
    auto_label = detected_persons[0].get("name", "") if detected_persons else ""

    print(f"[TEST] file_path: {test_file_path}")
    print(f"[TEST] detected_persons: {json.dumps(detected_persons, ensure_ascii=False, default=str)[:200]}")

    # 等待 LightRAG 处理完成（lightrag_insert_custom_kg 是同步的，不需要等待）
    time.sleep(2)

    # 2. 获取 KG 中的实体列表
    kg_entities = _list_kg_entities()
    print(f"[TEST] KG entities count: {len(kg_entities)}")

    # 打印所有实体
    for e in kg_entities[:30]:
        print(f"  - id={e.get('id')}, type={e.get('entity_type')}, desc={e.get('description', '')[:50]}")

    # 3. 验证: 无"未命名人物"实体 (Bug 1)
    unnamed_entities = [e for e in kg_entities
                        if e.get("id", "").startswith("未命名人物")]
    if unnamed_entities:
        print(f"FAIL Bug 1: Found unnamed entities: {unnamed_entities}")
        bug1_pass = False
    else:
        print(f"PASS Bug 1: No unnamed entities found")
        bug1_pass = True

    # 4. 验证: 照片实体 entityType="Photo" (Bug 3)
    photo_entities = [e for e in kg_entities
                      if e.get("id", "").startswith("photo:")]

    if not photo_entities:
        # 也搜索文件路径相关的实体
        photo_entities = [e for e in kg_entities
                          if test_file_path in e.get("id", "") or
                          os.path.basename(test_file_path) in e.get("id", "")]

    if not photo_entities:
        print(f"FAIL Bug 3: No photo entities found in KG")
        bug3_pass = False
    else:
        wrong_type = [pe for pe in photo_entities if pe.get("entity_type") != "Photo"]
        if wrong_type:
            print(f"FAIL Bug 3: Photo entities with wrong type: {wrong_type}")
            bug3_pass = False
        else:
            print(f"PASS Bug 3: Photo entities have correct type 'Photo'")
            bug3_pass = True

    # 5. 验证: 人物实体为 person:{uuid} 格式
    person_entities = [e for e in kg_entities
                       if e.get("id", "").startswith("person:")]
    if not person_entities:
        print(f"FAIL: No person:{uuid} entities found in KG")
        person_pass = False
    else:
        print(f"PASS: Found person:{uuid} entities: {[e.get('id') for e in person_entities]}")
        person_pass = True

    # 清理测试数据
    _cleanup_test_entities(test_file_path, test_person_id, auto_label)

    passed = bug1_pass and bug3_pass and person_pass
    print(f"\nTEST 1 RESULT: {'PASS' if passed else 'FAIL'}")
    return passed


# ============== Test 2: name_person — 旧实体清理 (Bug 2) ==============

def test_name_person_kg_cleanup():
    """RED: 验证改名后旧"未命名人物"实体被合并/删除"""
    print("\n" + "=" * 60)
    print("TEST 2: name_person KG cleanup (Bug 2)")
    print("=" * 60)

    _init_lightrag()
    test_photo = _get_test_photo()

    # 1. 先入库一张照片（创建"未命名人物"实体）
    from niu_photo_server import ingest_photo
    ingest_result = ingest_photo(file_path=test_photo)
    print(f"[TEST] ingest_photo result: status={ingest_result.get('status')}")

    if ingest_result.get("status") != "success":
        print(f"FAIL: ingest_photo failed: {ingest_result}")
        return False

    detected_persons = ingest_result.get("detected_persons", [])
    if not detected_persons:
        print(f"FAIL: No persons detected in photo")
        return False

    person_id = detected_persons[0]["id"]
    test_file_path = ingest_result.get("file_path", "")
    auto_label = detected_persons[0].get("name", "")

    print(f"[TEST] person_id: {person_id}, auto_label: {auto_label}")

    # 等待 LightRAG 处理
    time.sleep(5)

    # 2. 调用 name_person 改名
    from niu_photo_server import name_person
    test_name = f"测试人物_{uuid.uuid4().hex[:6]}"
    name_result = name_person(person_id=person_id, name=test_name)
    print(f"[TEST] name_person result: {name_result}")

    if name_result.get("status") != "success":
        print(f"FAIL: name_person failed: {name_result}")
        _cleanup_test_entities(test_file_path, person_id, auto_label)
        return False

    returned_auto_label = name_result.get("auto_label", auto_label)
    print(f"[TEST] Returned auto_label: {returned_auto_label}")

    # 等待 LightRAG 处理
    time.sleep(5)

    # 3. 查询 KG: 旧 auto_label 实体应不存在
    kg_entities = _list_kg_entities()
    old_entities = [e for e in kg_entities if e.get("id") == returned_auto_label]

    if old_entities:
        print(f"FAIL Bug 2: Old entity '{returned_auto_label}' still exists: {old_entities}")
        bug2_pass = False
    else:
        print(f"PASS Bug 2: Old entity '{returned_auto_label}' not found (cleaned up)")
        bug2_pass = True

    # 4. 查询 KG: person:{uuid} 实体存在
    person_entity = [e for e in kg_entities if e.get("id") == f"person:{person_id}"]
    if not person_entity:
        print(f"FAIL: person:{person_id} entity not found in KG")
        person_pass = False
    else:
        print(f"PASS: person:{person_id} entity found: {person_entity}")
        person_pass = True

    # 清理测试数据
    _cleanup_test_entities(test_file_path, person_id, returned_auto_label)

    passed = bug2_pass and person_pass
    print(f"\nTEST 2 RESULT: {'PASS' if passed else 'FAIL'}")
    return passed


# ============== Test 3: 照片实体类型 (Bug 3 独立验证) ==============

def test_photo_entity_type_is_photo():
    """RED: 验证照片节点 entityType 为 Photo 而非 Other"""
    print("\n" + "=" * 60)
    print("TEST 3: Photo entity type (Bug 3)")
    print("=" * 60)

    _init_lightrag()
    test_photo = _get_test_photo()

    # 1. 入库照片
    from niu_photo_server import ingest_photo
    ingest_result = ingest_photo(file_path=test_photo)
    print(f"[TEST] ingest_photo result: status={ingest_result.get('status')}")

    if ingest_result.get("status") != "success":
        print(f"FAIL: ingest_photo failed: {ingest_result}")
        return False

    file_path = ingest_result.get("file_path", "")
    detected_persons = ingest_result.get("detected_persons", [])
    test_person_id = detected_persons[0]["id"] if detected_persons else ""
    auto_label = detected_persons[0].get("name", "") if detected_persons else ""

    # 等待 LightRAG 处理
    time.sleep(5)

    # 2. 搜索 KG 中 Photo 类型的实体
    photo_entities = _list_kg_entities(entity_type_filter="Photo")
    print(f"[TEST] Photo-type entities: {len(photo_entities)}")

    # 也搜索文件路径相关的实体
    all_entities = _list_kg_entities()
    file_related = [e for e in all_entities
                    if file_path in e.get("id", "") or
               os.path.basename(file_path) in e.get("id", "") or
                    e.get("id", "").startswith("photo:")]
    print(f"[TEST] File-related entities: {len(file_related)}")

    # 3. 验证 entityType
    bug3_pass = True
    for pe in file_related:
        if pe.get("entity_type") != "Photo":
            print(f"FAIL Bug 3: Entity '{pe.get('id')}' type is '{pe.get('entity_type')}', expected 'Photo'")
            bug3_pass = False

    if bug3_pass and file_related:
        print(f"PASS Bug 3: All photo-related entities have type 'Photo'")
    elif not file_related:
        # 如果没有找到照片实体，检查是否有 photo: 前缀的实体
        photo_prefix_entities = [e for e in all_entities if e.get("id", "").startswith("photo:")]
        if photo_prefix_entities:
            for pe in photo_prefix_entities:
                if pe.get("entity_type") != "Photo":
                    print(f"FAIL Bug 3: photo: entity type is '{pe.get('entity_type')}'")
                    bug3_pass = False
                else:
                    print(f"PASS Bug 3: photo: entity type is 'Photo'")
        else:
            print(f"FAIL Bug 3: No photo entities found at all")
            bug3_pass = False

    # 清理测试数据
    _cleanup_test_entities(file_path, test_person_id, auto_label)

    print(f"\nTEST 3 RESULT: {'PASS' if bug3_pass else 'FAIL'}")
    return bug3_pass


# ============== Main ==============

def main():
    """运行所有 TDD 测试"""
    print("=" * 60)
    print("TDD Tests: KG Photo Bugs (RED phase)")
    print("=" * 60)

    results = {}

    # Test 1: Bug 1 + Bug 3
    try:
        results["test1_ingest_photo"] = test_ingest_photo_kg_entities()
    except Exception as e:
        print(f"TEST 1 EXCEPTION: {e}")
        import traceback
        traceback.print_exc()
        results["test1_ingest_photo"] = False

    # Test 2: Bug 2
    try:
        results["test2_name_person"] = test_name_person_kg_cleanup()
    except Exception as e:
        print(f"TEST 2 EXCEPTION: {e}")
        import traceback
        traceback.print_exc()
        results["test2_name_person"] = False

    # Test 3: Bug 3 (独立验证)
    try:
        results["test3_photo_type"] = test_photo_entity_type_is_photo()
    except Exception as e:
        print(f"TEST 3 EXCEPTION: {e}")
        import traceback
        traceback.print_exc()
        results["test3_photo_type"] = False

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    all_pass = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")
        if not passed:
            all_pass = False

    print(f"\nOverall: {'ALL PASS' if all_pass else 'SOME FAILED (expected in RED phase)'}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())