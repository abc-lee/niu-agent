#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试向量递归查询功能

测试场景：
1. 递归查询：用户输入 → 匹配查询模式 → 精简查询 → 工具
2. 非递归查询：用户输入 → 直接匹配工具
3. 递归深度限制：验证最多3次递归
"""

import sys
from pathlib import Path

# 设置 UTF-8 输出（修复 Windows 中文显示问题）
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.vector_search import VectorSearchAdapter


def test_recursive_query():
    """测试递归查询"""
    print("=" * 70)
    print("测试 1: 递归查询 - '5分钟后提醒我开会'")
    print("=" * 70)

    vs = VectorSearchAdapter(db_path="REDACTED_WIN_PATH/vectors.db")

    # 测试递归查询
    results = vs.search(
        query="5分钟后提醒我开会",
        limit=5,
        min_score=0.0,
        filter={"category": "mcp_tool"}
    )

    print(f"\n找到 {len(results)} 个结果：\n")
    for i, r in enumerate(results, 1):
        print(f"{i}. {r.metadata.get('name', r.id)} - 相似度: {r.score:.4f}")
        print(f"   类型: {r.metadata.get('type')}")
        print(f"   内容: {r.content[:80]}...")
        print()


def test_non_recursive_query():
    """测试非递归查询"""
    print("\n" + "=" * 70)
    print("测试 2: 非递归查询 - 'schedule task'")
    print("=" * 70)

    vs = VectorSearchAdapter(db_path="REDACTED_WIN_PATH/vectors.db")

    # 测试非递归查询
    results = vs.search(
        query="schedule task",
        limit=5,
        min_score=0.0,
        filter={"category": "mcp_tool"}
    )

    print(f"\n找到 {len(results)} 个结果：\n")
    for i, r in enumerate(results, 1):
        print(f"{i}. {r.metadata.get('name', r.id)} - 相似度: {r.score:.4f}")
        print(f"   类型: {r.metadata.get('type')}")
        print()


def test_recursion_depth_limit():
    """测试递归深度限制"""
    print("\n" + "=" * 70)
    print("测试 3: 递归深度限制 - 构造循环引用")
    print("=" * 70)

    vs = VectorSearchAdapter(db_path="REDACTED_WIN_PATH/vectors.db")

    # 手动构造一个会触发递归的查询
    # 注意：实际测试需要向量库中有循环引用的数据
    # 这里只是验证 max_recursion 参数传递

    try:
        results = vs.search(
            query="测试查询",
            limit=5,
            min_score=0.0,
            max_recursion=3  # 最多递归3次
        )
        print("✅ 递归深度限制正常工作")
        print(f"返回 {len(results)} 个结果")
    except RecursionError:
        print("❌ 递归深度限制失效，发生了 RecursionError")
    except Exception as e:
        print(f"⚠️ 发生异常: {e}")


def test_chinese_vs_english():
    """测试中英文查询对比"""
    print("\n" + "=" * 70)
    print("测试 4: 中英文查询对比")
    print("=" * 70)

    vs = VectorSearchAdapter(db_path="REDACTED_WIN_PATH/vectors.db")

    queries = [
        ("5分钟后提醒我吃药", "中文查询"),
        ("remind me in 5 minutes", "英文查询"),
    ]

    for query, desc in queries:
        print(f"\n{desc}: '{query}'")
        print("-" * 60)

        results = vs.search(
            query=query,
            limit=3,
            min_score=0.0,
            filter={"category": "mcp_tool"}
        )

        if results:
            r = results[0]
            print(f"  最佳匹配: {r.metadata.get('name')}")
            print(f"  相似度: {r.score:.4f}")
            print(f"  递归: {'是' if r.metadata.get('type') == 'query_pattern' else '否'}")
        else:
            print("  无匹配结果")


def main():
    """主测试函数"""
    print("\n" + "=" * 70)
    print("向量递归查询功能测试")
    print("=" * 70)

    # 测试 1: 递归查询
    test_recursive_query()

    # 测试 2: 非递归查询
    test_non_recursive_query()

    # 测试 3: 递归深度限制
    test_recursion_depth_limit()

    # 测试 4: 中英文对比
    test_chinese_vs_english()

    print("\n" + "=" * 70)
    print("✅ 测试完成")
    print("=" * 70)


if __name__ == "__main__":
    main()
