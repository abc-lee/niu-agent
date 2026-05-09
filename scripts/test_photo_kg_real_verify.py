"""照片 KG 重构后真实数据验证。

读取 ~/.niu/lightrag_storage/ 的 JSON 文件，验证：
1. 不存在 person:{uuid} 格式的实体
2. 照片实体 file_path 不为 unknown_source
3. 人物实体使用人名或 auto_label
"""

import json
from pathlib import Path


def load_entities():
    storage = Path.home() / ".niu" / "lightrag_storage"
    entities_file = storage / "kv_store_full_entities.json"
    if not entities_file.exists():
        print(f"[SKIP] {entities_file} not found")
        return {}
    with open(entities_file, "r", encoding="utf-8") as f:
        return json.load(f)


def test_no_person_uuid_entities():
    """不应存在 person:{uuid} 格式的实体"""
    entities = load_entities()
    if not entities:
        return

    person_uuid_entities = []
    for key, value in entities.items():
        name = value.get("entity_name", "") if isinstance(value, dict) else ""
        if name.startswith("person:"):
            person_uuid_entities.append(name)

    if person_uuid_entities:
        print(f"[FAIL] Found {len(person_uuid_entities)} person:uuid entities:")
        for name in person_uuid_entities[:10]:
            print(f"  - {name}")
    else:
        print("[PASS] No person:uuid entities found")


def test_photo_entities_have_file_path():
    """照片实体应有正确的 file_path"""
    entities = load_entities()
    if not entities:
        return

    unknown_source_count = 0
    for key, value in entities.items():
        if not isinstance(value, dict):
            continue
        name = value.get("entity_name", "")
        if name.startswith("photo:"):
            fp = value.get("file_path", "")
            if fp == "unknown_source" or not fp:
                unknown_source_count += 1
                print(f"  [WARN] {name}: file_path={fp}")

    if unknown_source_count:
        print(f"[FAIL] {unknown_source_count} photo entities with unknown_source")
    else:
        print("[PASS] All photo entities have valid file_path")


def test_person_entities_use_names():
    """人物实体应使用人名或 auto_label"""
    entities = load_entities()
    if not entities:
        return

    bad_persons = []
    for key, value in entities.items():
        if not isinstance(value, dict):
            continue
        name = value.get("entity_name", "")
        etype = value.get("entity_type", "")
        if etype == "person" and name.startswith("person:"):
            bad_persons.append(name)

    if bad_persons:
        print(f"[FAIL] {len(bad_persons)} person entities with person:uuid format")
    else:
        print("[PASS] All person entities use name/auto_label format")


if __name__ == "__main__":
    print("=== 照片 KG 重构后真实数据验证 ===\n")
    test_no_person_uuid_entities()
    test_photo_entities_have_file_path()
    test_person_entities_use_names()
    print("\n=== 验证完成 ===")
