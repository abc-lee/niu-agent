#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
向量库验证脚本

用途：验证向量库内容是否符合L0/L1/L2规范
"""

import sys
import json
from pathlib import Path

# 设置 UTF-8 输出（修复 Windows 中文显示问题）
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.vector_search import get_vector_search


def main():
    vs = get_vector_search()
    conn = vs._get_connection()

    cursor = conn.execute('SELECT id, metadata FROM documents')
    docs = cursor.fetchall()

    print('=' * 60)
    print('向量库内容验证')
    print('=' * 60)
    print(f'总文档数: {len(docs)}\n')

    # 按类型分组
    by_category = {}
    for doc_id, metadata_json in docs:
        metadata = json.loads(metadata_json) if metadata_json else {}
        level = metadata.get('level', 'unknown')
        category = metadata.get('category', 'unknown')
        key = f'{level}/{category}'

        if key not in by_category:
            by_category[key] = []
        by_category[key].append(doc_id)

    for key, ids in sorted(by_category.items()):
        print(f'{key}: {len(ids)} 条')
        for doc_id in ids[:3]:  # 只显示前3个
            print(f'  - {doc_id}')
        if len(ids) > 3:
            print(f'  ... 还有 {len(ids) - 3} 条')
        print()

    # 测试搜索
    print('=' * 60)
    print('搜索测试')
    print('=' * 60)

    test_queries = [
        ("启动慢", {"level": "l1", "category": "document"}),
        ("人脸识别", {"level": "l1", "category": "mcp_tool"}),
        ("照片处理", {"level": "l1", "category": "skill"}),
    ]

    for query, filter_dict in test_queries:
        print(f'\n查询: "{query}" (过滤: {filter_dict})')
        results = vs.search(query=query, limit=3, min_score=0.3, filter=filter_dict)
        print(f'  找到 {len(results)} 个结果:')
        for i, r in enumerate(results, 1):
            print(f'  {i}. {r.id} (分数: {int(r.score * 100)})')


if __name__ == "__main__":
    main()
