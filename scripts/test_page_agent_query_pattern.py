#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 Page-Agent 查询模式的递归检索

测试场景：
1. 用户说"帮我搜索Python教程" → 应触发递归 → 命中 browser automation 工具
2. 用户说"打开GitHub" → 应触发递归 → 命中 browser automation 工具
3. 用户说"帮我填个表单" → 应触发递归 → 命中 browser automation 工具
"""

import sys
from pathlib import Path

# 设置 UTF-8 输出
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.vector_search import VectorSearchAdapter


def test_browser_automation_queries():
    """测试浏览器自动化相关的查询"""
    print("=" * 70)
    print("测试：浏览器自动化查询的递归检索")
    print("=" * 70)

    vs = VectorSearchAdapter()

    test_queries = [
        ("帮我搜索Python教程", "browser automation"),
        ("打开GitHub", "browser automation"),
        ("查查最新的新闻", "browser automation"),
        ("帮我填个表单", "browser automation"),
        ("把这个网页保存下来", "browser automation"),
        ("帮我买张机票", "browser automation"),
        ("search for Python tutorials", "browser automation"),
        ("open GitHub website", "browser automation"),
    ]

    passed = 0
    for query, expected_category in test_queries:
        # 注意：不要传filter参数，否则第一轮检索会过滤掉query_pattern，导致递归不触发
        results = vs.search(
            query=query,
            limit=3,
            min_score=0.0
            # 不要加 filter，让递归机制自动工作
        )

        if results:
            top_result = results[0]
            matched_category = top_result.metadata.get("category", "")
            tool_name = top_result.metadata.get("name", top_result.id)
            tool_desc = top_result.metadata.get("description", "")
            score = top_result.score

            # 检查是否匹配到 browser automation 相关工具
            is_match = (
                "browse" in tool_name.lower() or
                "browser automation" in tool_desc.lower()
            )

            status = "PASS" if (is_match and score >= 0.5) else "FAIL"
            if is_match and score >= 0.5:
                passed += 1

            print(f"\n[{status}] '{query}'")
            print(f"  匹配: {tool_name}")
            print(f"  相似度: {score:.4f}")
            print(f"  类别: {matched_category}")
        else:
            print(f"\n[FAIL] '{query}'")
            print(f"  无匹配结果")

    print(f"\n{'='*70}")
    print(f"测试结果: {passed}/{len(test_queries)} 通过")
    print(f"{'='*70}")

    return passed, len(test_queries)


if __name__ == "__main__":
    passed, total = test_browser_automation_queries()
    sys.exit(0 if passed == total else 1)
