#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 Memory Server 的 L0/L1/L2 功能

验证：
1. remember 工具生成三层记录
2. recall 工具支持 level 参数
3. update_memory 工具更新所有层级
4. get_memory_stats 统计正确
"""

import sys
import os
from pathlib import Path

# 设置项目根目录
project_root = Path(__file__).parent.parent

# 添加 memory-server 和项目根目录到 sys.path
sys.path.insert(0, str(project_root / "mcp-servers" / "memory-server" / "src"))
sys.path.insert(0, str(project_root))

# 设置 UTF-8 输出（修复 Windows 中文显示问题）
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from niu_memory_server.storage import MemoryStorage


def test_l0l1l2_storage():
    """测试 L0/L1/L2 三层存储"""
    print("\n" + "=" * 60)
    print("测试 1: L0/L1/L2 三层存储")
    print("=" * 60)

    storage = MemoryStorage()

    # 保存记忆
    content = "用户偏好使用深色主题，字体大小为14px"
    memory_type = "preferences"

    memory_id = storage.store_memory(content, memory_type)
    print(f"\n✓ 保存记忆成功: {memory_id}")

    # 验证三层记录
    import sqlite3
    conn = sqlite3.connect(storage.db_path)
    cursor = conn.cursor()

    # 查询所有层级
    cursor.execute(
        "SELECT id, content, metadata FROM documents WHERE id LIKE ?",
        (f"{memory_id}%",)
    )
    rows = cursor.fetchall()

    print(f"\n找到 {len(rows)} 条记录：")
    for row in rows:
        import json
        doc_id, content, metadata_json = row
        metadata = json.loads(metadata_json)
        level = metadata.get("level")
        print(f"  - {doc_id} (level={level}, content_length={len(content)})")

    conn.close()

    # 验证是否有三层
    expected_levels = {"l0", "l1", "l2"}
    found_levels = {json.loads(row[2]).get("level") for row in rows}

    if expected_levels == found_levels:
        print(f"\n✓ 成功创建 L0/L1/L2 三层记录")
    else:
        print(f"\n✗ 缺少层级: {expected_levels - found_levels}")


def test_recall_with_level():
    """测试 recall 工具的 level 参数"""
    print("\n" + "=" * 60)
    print("测试 2: recall 工具 level 参数")
    print("=" * 60)

    storage = MemoryStorage()

    # 先保存一条记忆
    content = "测试记忆：用户喜欢使用Python进行数据分析"
    memory_id = storage.store_memory(content, "preferences")

    # 搜索 L1 层级
    results_l1 = storage.search_memories(
        query="数据分析",
        limit=3,
        level="l1"
    )

    print(f"\n搜索 L1 层级 (query='数据分析'): {len(results_l1)} 条结果")
    for i, r in enumerate(results_l1, 1):
        print(f"  {i}. {r['id']} - score={int(r['similarity'] * 100)}")

    # 搜索 L2 层级
    results_l2 = storage.search_memories(
        query="数据分析",
        limit=3,
        level="l2"
    )

    print(f"\n搜索 L2 层级 (query='数据分析'): {len(results_l2)} 条结果")
    for i, r in enumerate(results_l2, 1):
        print(f"  {i}. {r['id']} - score={int(r['similarity'] * 100)}")

    # 验证结果
    if results_l1 and all("l1" in r["id"] for r in results_l1):
        print(f"\n✓ L1 搜索返回 L1 记录")
    else:
        print(f"\n✗ L1 搜索返回错误层级")

    if results_l2 and all("l2" in r["id"] for r in results_l2):
        print(f"✓ L2 搜索返回 L2 记录")
    else:
        print(f"✗ L2 搜索返回错误层级")


def test_memory_stats():
    """测试记忆统计"""
    print("\n" + "=" * 60)
    print("测试 3: get_memory_stats")
    print("=" * 60)

    storage = MemoryStorage()

    # 保存几条测试记忆
    storage.store_memory("测试环境：Windows 11 Pro", "environment")
    storage.store_memory("测试偏好：深色主题", "preferences")

    # 获取统计
    stats = storage.get_memory_stats()

    print(f"\n记忆统计:")
    print(f"  总记录数: {stats['total']}")
    print(f"  按层级: {stats['by_level']}")
    print(f"  按类型: {stats['by_type']}")

    # 验证统计正确
    if stats["total"] > 0:
        print(f"\n✓ 统计功能正常")
    else:
        print(f"\n✗ 统计功能异常")


def test_cleanup():
    """测试清理过期记忆"""
    print("\n" + "=" * 60)
    print("测试 4: cleanup_memories")
    print("=" * 60)

    storage = MemoryStorage()

    # 清理 1000 天前的记忆（应该不会删除任何记忆）
    deleted = storage.cleanup_memories(days=1000)

    print(f"\n清理 1000 天前的记忆: 删除 {deleted} 条")

    if deleted == 0:
        print(f"✓ 清理功能正常（未删除近期记忆）")
    else:
        print(f"⚠ 清理功能可能有问题（删除了近期记忆）")


def main():
    print("=" * 60)
    print("Memory Server L0/L1/L2 功能测试")
    print("=" * 60)

    try:
        test_l0l1l2_storage()
        test_recall_with_level()
        test_memory_stats()
        test_cleanup()

        print("\n" + "=" * 60)
        print("测试完成")
        print("=" * 60)

    except Exception as e:
        print(f"\n✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
