"""将现有图谱中的 person:{uuid} 实体迁移为人名实体。

读取 photos.db 获取 UUID→人名映射，然后对每个 person:{uuid} 实体：
1. 调用 lightrag_merge_entities([f"person:{uuid}"], name) 改名
2. 如果未命名，调用 lightrag_merge_entities([f"person:{uuid}"], auto_label)

此脚本应在重构代码部署后运行一次。

用法:
  python scripts/migrate_photo_kg_entities.py                    # dry-run 预览
  python scripts/migrate_photo_kg_entities.py --execute          # 实际执行
  python scripts/migrate_photo_kg_entities.py /path/to/photos.db  # 指定数据库路径
"""

import sqlite3
import sys
from pathlib import Path


def get_person_mapping(photos_db_path: str) -> dict:
    """从 photos.db 获取 UUID → (name, auto_label) 映射"""
    with sqlite3.connect(photos_db_path) as conn:
        cursor = conn.execute("SELECT id, name, auto_label FROM persons")
        mapping = {}
        for row in cursor.fetchall():
            person_id, name, auto_label = row
            target_name = name if name and not name.startswith("未命名人物") else auto_label
            if target_name:
                mapping[person_id] = target_name
    return mapping


def migrate_person_entities(photos_db_path: str, dry_run: bool = True):
    """迁移 person:{uuid} 实体为人名实体"""
    mapping = get_person_mapping(photos_db_path)
    print(f"Found {len(mapping)} persons in photos.db")

    if dry_run:
        print("\n[DRY RUN] Would migrate:")
        for uuid, name in mapping.items():
            old_name = f"person:{uuid}"
            print(f"  {old_name} → {name}")
        return

    # Only import tool_registry when actually executing
    try:
        from agent.tool_registry import get_registry
    except ImportError:
        print("[ERROR] Cannot import tool_registry. Run from project root with proper PYTHONPATH.")
        return

    registry = get_registry()
    merge_fn = registry.get("lightrag-server/lightrag_merge_entities")

    if not merge_fn:
        print("[ERROR] lightrag_merge_entities not available")
        return

    migrated = 0
    for uuid, name in mapping.items():
        old_name = f"person:{uuid}"
        try:
            result = merge_fn(source_entities=[old_name], target_entity=name)
            print(f"  [OK] {old_name} → {name}: {result}")
            migrated += 1
        except Exception as e:
            print(f"  [FAIL] {old_name} → {name}: {e}")

    print(f"\nMigrated {migrated}/{len(mapping)} person entities")
    if migrated < len(mapping):
        print("\n[WARNING] Not all entities were migrated. Re-run to retry failed ones.")
        print("merge_entities is idempotent — already-migrated entities will be skipped.")


if __name__ == "__main__":
    photos_db = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "REDACTED_WIN_PATH/photos.db"
    dry_run = "--execute" not in sys.argv

    print(f"=== 旧数据迁移: person:{{uuid}} → 人名 ===")
    print(f"photos.db: {photos_db}")
    print(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}\n")

    migrate_person_entities(photos_db, dry_run=dry_run)
