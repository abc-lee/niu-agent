#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重新生成所有向量（使用新模型）

在更换embedding模型后运行此脚本。
用法：python scripts/regenerate_all_embeddings.py
"""

import sys
import sqlite3
import json
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.vector_search import VectorSearchAdapter

def regenerate_all_embeddings():
    """重新生成所有文档的向量"""

    print("=" * 60)
    print("Regenerate All Embeddings (New Model)")
    print("=" * 60)

    # 获取向量库
    adapter = VectorSearchAdapter()
    db_path = adapter.db_path

    print(f"\nVector DB: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. 统计现有文档
    cursor.execute("SELECT COUNT(*) FROM documents")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM documents WHERE embedding IS NOT NULL")
    with_embedding = cursor.fetchone()[0]

    print(f"\nTotal documents: {total}")
    print(f"Documents with embedding: {with_embedding}")

    # 2. 确认操作
    print("\n" + "=" * 60)
    print("WARNING: This will regenerate ALL embeddings using the new model.")
    print("Old embeddings will be replaced.")
    print("=" * 60)

    response = input("\nContinue? (yes/N): ").strip().lower()
    if response != 'yes':
        print("Cancelled.")
        conn.close()
        return

    # 3. 清空所有embedding
    print("\nClearing old embeddings...")
    cursor.execute("UPDATE documents SET embedding = NULL")
    conn.commit()
    print("[OK] Old embeddings cleared")

    # 4. 重新生成
    print("\nRegenerating embeddings...")

    cursor.execute("SELECT id, content FROM documents WHERE content IS NOT NULL")
    documents = cursor.fetchall()

    print(f"Processing {len(documents)} documents...\n")

    success = 0
    failed = 0

    for i, (doc_id, content) in enumerate(documents, 1):
        try:
            # 获取新向量
            embedding = adapter._get_embedding(content)

            if embedding is None:
                print(f"[{i}/{len(documents)}] {doc_id} - [FAIL] Failed to get embedding")
                failed += 1
                continue

            # 保存向量
            import numpy as np
            embedding_blob = np.array(embedding, dtype=np.float32).tobytes()

            cursor.execute(
                "UPDATE documents SET embedding = ? WHERE id = ?",
                (embedding_blob, doc_id)
            )

            success += 1

            # 每10个提交一次并显示进度
            if success % 10 == 0:
                conn.commit()
                print(f"[{i}/{len(documents)}] Processed {success} documents...")

            # 小延迟避免过载
            if i % 5 == 0:
                time.sleep(0.1)

        except Exception as e:
            print(f"[{i}/{len(documents)}] {doc_id} - [ERROR] {e}")
            failed += 1

    # 最终提交
    conn.commit()
    conn.close()

    # 5. 总结
    print("\n" + "=" * 60)
    print("Regeneration Complete!")
    print("=" * 60)
    print(f"Total: {len(documents)}")
    print(f"Success: {success}")
    print(f"Failed: {failed}")
    print("=" * 60)


if __name__ == "__main__":
    regenerate_all_embeddings()
