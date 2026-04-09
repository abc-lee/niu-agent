#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从导出的JSON文件注册所有MCP工具到向量库"""
import sys
import json
import sqlite3
import time
import numpy as np
from pathlib import Path

# 设置 UTF-8 输出（修复 Windows 中文显示问题）
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 添加项目根目录
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from agent.vector_search import VectorSearchAdapter

def main():
    print("=" * 70)
    print("注册完整的66个MCP工具到向量库")
    print("=" * 70)

    # 读取导出的工具定义
    json_file = Path(__file__).parent.parent / "logs" / "all_mcp_tools.json"
    with open(json_file, 'r', encoding='utf-8') as f:
        tools_by_server = json.load(f)

    # 展平为单个列表
    all_tools = []
    for server, tools in sorted(tools_by_server.items()):
        all_tools.extend(tools)

    print(f"\n从JSON读取了 {len(all_tools)} 个工具定义\n")

    # 获取向量库连接
    from agent.vector_search import get_vector_search
    vs = get_vector_search()
    conn = vs._get_connection()

    if conn is None:
        logger.error("✗ 向量库连接失败")
        return

    # 注册每个工具
    registered = 0
    for i, tool in enumerate(all_tools, 1):
        try:
            # 构建文档ID和内容
            doc_id = f"mcp_tool:{tool['server']}:{tool['name']}"
            content = f"{tool['name']}: {tool['description']}"

            # 元数据（符合L0/L1/L2规范）
            metadata = {
                "level": "l1",  # 小写
                "category": "mcp_tool",
                "name": tool["name"],
                "server": tool["server"],
                "description": tool["description"],
                "input_schema": tool["input_schema"]
            }

            # 获取向量
            embedding = vs._get_embedding(content)
            if embedding is None:
                logger.warning(f"[{i}/{len(all_tools)}] {tool['name']} - 向量生成失败")
                continue

            # ✅ L2归一化（符合L1规范）
            vec = np.array(embedding, dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            embedding_blob = vec.tobytes()

            # UPSERT
            conn.execute(
                """
                INSERT INTO documents (id, content, embedding, metadata)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    content = excluded.content,
                    embedding = excluded.embedding,
                    metadata = excluded.metadata
                """,
                (doc_id, content, embedding_blob, json.dumps(metadata, ensure_ascii=False)),
            )
            conn.commit()

            print(f"[{i}/{len(all_tools)}] {tool['server']}/{tool['name']} - ✓")
            registered += 1
            time.sleep(0.3)  # 避免过载

        except Exception as e:
            logger.error(f"[{i}/{len(all_tools)}] {tool['name']} - ✗ {e}")

    print("\n" + "=" * 70)
    print(f"✓ MCP 工具注册完成: {registered}/{len(all_tools)}")
    print("=" * 70)

if __name__ == "__main__":
    main()
