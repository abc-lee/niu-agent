#!/usr/bin/env python3
"""测试递归查询功能"""
import sys
import io
from pathlib import Path

# UTF-8 输出
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 添加项目根目录
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.vector_search import get_vector_search

def test_recursive_query():
    """测试递归查询"""
    vs = get_vector_search()

    # 测试用例
    test_cases = [
        "提醒我五分钟后吃药",
        "帮我回忆一下之前的经验",
        "search for documents",
    ]

    print("=== 递归查询测试 ===\n")

    for query in test_cases:
        print(f'用户输入: "{query}"')

        # 第一轮：查询模式
        results = vs.search(
            query=query,
            limit=3,
            min_score=0.2,  # 降低阈值
            filter={'category': 'query_pattern'}
        )

        if results:
            for r in results:
                metadata = r.metadata if hasattr(r, 'metadata') else {}
                print(f'  → 匹配到查询模式: {metadata.get("id", "N/A")} (分数: {r.score if hasattr(r, "score") else 0:.3f})')

                is_recursive = metadata.get('is_recursive', False)
                refined_query = metadata.get('refined_query', '')

                if is_recursive and refined_query:
                    print(f'    → 递归查询关键词: "{refined_query}"')

                    # 第二轮：MCP 工具
                    results2 = vs.search(
                        query=refined_query,
                        limit=3,
                        min_score=0.2,
                        filter={'category': 'mcp_tool'}
                    )

                    if results2:
                        print(f'    → 匹配到 MCP 工具:')
                        for r2 in results2:
                            tool_name = f"{r2.metadata.get('server', '')}/{r2.metadata.get('name', '')}"
                            print(f'      - {tool_name} (分数: {r2.score if hasattr(r2, "score") else 0:.3f})')
                    else:
                        print('    → 未匹配到 MCP 工具')
                    
                    break  # 只处理第一个递归查询
        else:
            print('  → 未匹配到查询模式')

        print()

if __name__ == "__main__":
    test_recursive_query()
