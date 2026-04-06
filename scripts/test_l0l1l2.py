#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试向量库 L0/L1/L2 支持

验证：
1. level 参数过滤功能
2. get_l2_content() 方法
3. 数据库索引创建
"""

import sys
import os
from pathlib import Path

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

# 设置 UTF-8 输出（修复 Windows 中文显示问题）
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from agent.vector_search import get_vector_search


def test_level_filter():
    """测试 level 参数过滤"""
    print("\n" + "=" * 60)
    print("测试 1: Level 参数过滤")
    print("=" * 60)

    vs = get_vector_search()

    # 测试搜索 L1 层级
    results = vs.search(
        query="照片处理",
        limit=5,
        min_score=0.3,
        level="l1"
    )

    print(f"\n搜索 '照片处理' (level=l1):")
    print(f"  找到 {len(results)} 个结果")

    for i, r in enumerate(results, 1):
        level = r.metadata.get("level", "unknown")
        category = r.metadata.get("category", "unknown")
        print(f"  {i}. {r.id} - level={level}, category={category}, score={int(r.score * 100)}")

    # 验证所有结果都是 l1
    if results:
        all_l1 = all(r.metadata.get("level") == "l1" for r in results)
        print(f"\n✓ 所有结果都是 l1 层级: {all_l1}")
    else:
        print("\n⚠ 未找到 L1 记录，可能向量库未初始化")


def test_get_l2_content():
    """测试 get_l2_content() 方法"""
    print("\n" + "=" * 60)
    print("测试 2: get_l2_content() 方法")
    print("=" * 60)

    vs = get_vector_search()

    # 先搜索一个 L1 记录
    results = vs.search(
        query="照片处理",
        limit=1,
        min_score=0.3,
        level="l1"
    )

    if not results:
        print("\n⚠ 未找到 L1 记录，无法测试 get_l2_content()")
        return

    l1_record = results[0]
    print(f"\n找到 L1 记录: {l1_record.id}")
    print(f"  内容长度: {len(l1_record.content)} 字符")

    # 获取对应的 L2 内容
    l2_content = vs.get_l2_content(l1_record.id)

    if l2_content:
        print(f"\n✓ 成功获取 L2 原文")
        print(f"  L2 内容长度: {len(l2_content)} 字符")
        print(f"  L2 预览: {l2_content[:200]}...")
    else:
        print(f"\n⚠ 未找到对应的 L2 记录（可能 L1 没有设置 pointer）")


def test_indexes():
    """测试数据库索引"""
    print("\n" + "=" * 60)
    print("测试 3: 数据库索引")
    print("=" * 60)

    vs = get_vector_search()
    conn = vs._get_connection()

    if conn is None:
        print("\n✗ 数据库连接失败")
        return

    # 查询索引列表
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
    )
    indexes = [row[0] for row in cursor.fetchall()]

    print(f"\n已创建的索引 ({len(indexes)} 个):")
    for idx in indexes:
        print(f"  - {idx}")

    expected_indexes = ["idx_level", "idx_category", "idx_server"]
    missing = [idx for idx in expected_indexes if idx not in indexes]

    if missing:
        print(f"\n✗ 缺少索引: {missing}")
    else:
        print(f"\n✓ 所有预期索引都已创建")


def main():
    print("=" * 60)
    print("向量库 L0/L1/L2 功能测试")
    print("=" * 60)

    try:
        test_level_filter()
        test_get_l2_content()
        test_indexes()

        print("\n" + "=" * 60)
        print("测试完成")
        print("=" * 60)

    except Exception as e:
        print(f"\n✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
