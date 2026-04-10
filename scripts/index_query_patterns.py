"""
索引查询模式到向量库

用途：
- 将用户习惯用语映射到标准查询
- 支持向量递归检索

遵循规范：
- docs/design-vector-recursive-query.md
- docs/spec-L1-summary.md

使用：
    python scripts/index_query_patterns.py
"""

import sys
import sqlite3
import json
import numpy as np
import io
from pathlib import Path

# 修复Windows控制台编码问题
if sys.platform == 'win32':
    if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if not isinstance(sys.stderr, io.TextIOWrapper) or sys.stderr.encoding != 'utf-8':
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 查询模式定义
# 格式：用户习惯用语 -> 标准查询 -> 目标工具类别
QUERY_PATTERNS = [
    # ==================== 记忆管理类 ====================
    {
        "id": "query_pattern:recall_memory_1",
        "content": "recall previous memories",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "memory recall remember",
            "target_category": "mcp_tool",
            "description": "User wants to recall or retrieve previous memories"
        }
    },
    {
        "id": "query_pattern:recall_memory_2",
        "content": "what did I say before",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "memory recall",
            "target_category": "mcp_tool",
            "description": "User asking about previous statements"
        }
    },
    {
        "id": "query_pattern:remember_this",
        "content": "remember this",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "save memory remember",
            "target_category": "mcp_tool",
            "description": "User wants to save something to memory"
        }
    },
    {
        "id": "query_pattern:remember_what_i_like",
        "content": "remember what I like",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "save preference memory",
            "target_category": "mcp_tool",
            "description": "User wants to save preferences"
        }
    },

    # ==================== 文档检索类 ====================
    {
        "id": "query_pattern:search_documents",
        "content": "search for documents",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "document search vector",
            "target_category": "mcp_tool",
            "description": "User wants to search documents"
        }
    },
    {
        "id": "query_pattern:find_documents",
        "content": "find documents about",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "document search",
            "target_category": "mcp_tool",
            "description": "User wants to find specific documents"
        }
    },
    {
        "id": "query_pattern:retrieve_knowledge",
        "content": "retrieve knowledge about",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "document search knowledge",
            "target_category": "mcp_tool",
            "description": "User wants to retrieve knowledge from database"
        }
    },

    # ==================== 文档添加类 ====================
    {
        "id": "query_pattern:add_document",
        "content": "add this document",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "add document vector store",
            "target_category": "mcp_tool",
            "description": "User wants to add a document to database"
        }
    },
    {
        "id": "query_pattern:save_document",
        "content": "save this document",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "en",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "add document",
            "target_category": "mcp_tool",
            "description": "User wants to save a document"
        }
    },

    # ==================== 中文查询模式 ====================
    # 记忆管理
    {
        "id": "query_pattern:zh_recall_1",
        "content": "检索之前的记忆",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "zh",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "memory recall remember",
            "target_category": "mcp_tool",
            "description": "用户想检索之前的记忆"
        }
    },
    {
        "id": "query_pattern:zh_recall_2",
        "content": "我之前说过什么",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "zh",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "memory recall",
            "target_category": "mcp_tool",
            "description": "用户询问之前说过的话"
        }
    },
    {
        "id": "query_pattern:zh_remember_1",
        "content": "记住这个",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "zh",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "save memory remember",
            "target_category": "mcp_tool",
            "description": "用户想记住某些内容"
        }
    },
    {
        "id": "query_pattern:zh_remember_2",
        "content": "记住我喜欢什么",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "zh",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "save preference memory",
            "target_category": "mcp_tool",
            "description": "用户想保存偏好"
        }
    },

    # 文档检索
    {
        "id": "query_pattern:zh_search_1",
        "content": "搜索文档",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "zh",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "document search vector",
            "target_category": "mcp_tool",
            "description": "用户想搜索文档"
        }
    },
    {
        "id": "query_pattern:zh_search_2",
        "content": "查找关于XX的文档",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "zh",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "document search",
            "target_category": "mcp_tool",
            "description": "用户想查找特定文档"
        }
    },
    {
        "id": "query_pattern:zh_retrieve_1",
        "content": "检索知识库",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "zh",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "document search knowledge",
            "target_category": "mcp_tool",
            "description": "用户想从知识库检索信息"
        }
    },

    # 文档添加
    {
        "id": "query_pattern:zh_add_1",
        "content": "添加这个文档",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "zh",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "add document vector store",
            "target_category": "mcp_tool",
            "description": "用户想添加文档"
        }
    },
    {
        "id": "query_pattern:zh_save_1",
        "content": "保存这个文档",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "zh",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "add document",
            "target_category": "mcp_tool",
            "description": "用户想保存文档"
        }
    },

    # ==================== 复杂场景 ====================
    {
        "id": "query_pattern:complex_memory_1",
        "content": "帮我回忆一下之前的经验",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "zh",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "memory recall experience",
            "target_category": "mcp_tool",
            "description": "用户想回忆之前的经验"
        }
    },
    {
        "id": "query_pattern:complex_search_1",
        "content": "在知识库里找找相关内容",
        "metadata": {
            "level": "l1",
            "category": "query_pattern",
            "language": "zh",
            "type": "query_pattern",
            "is_recursive": True,
            "refined_query": "document search knowledge",
            "target_category": "mcp_tool",
            "description": "用户想在知识库中查找内容"
        }
    },
]


def get_embedding(text: str) -> list[float] | None:
    """获取文本向量"""
    from niu_api.internal.embedding import get_model

    model = get_model()
    if model is None:
        return None

    embedding = model.encode([text], normalize_embeddings=True)
    return embedding[0].tolist()


def index_query_patterns():
    """索引查询模式到向量库"""

    print("=== 开始索引查询模式 ===\n")

    # 1. 连接向量库数据库
    print("1. 连接向量库...")
    db_path = Path.home() / ".niu" / "vectors.db"
    if not db_path.exists():
        print(f"   ✗ 数据库不存在: {db_path}")
        return 0, len(QUERY_PATTERNS)

    conn = sqlite3.connect(str(db_path))
    print("   ✓ 向量库连接成功")

    # 2. 写入向量库
    print(f"\n2. 写入 {len(QUERY_PATTERNS)} 个查询模式...")
    success_count = 0
    error_count = 0

    for pattern in QUERY_PATTERNS:
        pattern_id = pattern["id"]
        content = pattern["content"]
        metadata = pattern["metadata"]

        try:
            # 获取embedding
            print(f"   生成向量: {pattern_id}...")
            embedding = get_embedding(content)
            embedding_blob = None
            if embedding:
                embedding_blob = np.array(embedding, dtype=np.float32).tobytes()

            # 写入数据库
            metadata_json = json.dumps(metadata)

            conn.execute(
                "INSERT OR REPLACE INTO documents (id, content, embedding, metadata) VALUES (?, ?, ?, ?)",
                (pattern_id, content, embedding_blob, metadata_json),
            )
            conn.commit()

            success_count += 1
            print(f"   ✓ {pattern_id}: {content}")

        except Exception as e:
            error_count += 1
            print(f"   ✗ {pattern_id}: {e}")

    conn.close()

    # 3. 统计结果
    print("\n=== 索引完成 ===")
    print(f"成功: {success_count}")
    print(f"失败: {error_count}")
    print(f"总计: {len(QUERY_PATTERNS)}")

    # 4. 验证递归查询
    print("\n=== 验证递归查询 ===")

    from agent.vector_search import get_vector_search
    vs = get_vector_search()

    test_cases = [
        "检索之前的记忆",
        "帮我回忆一下之前的经验",
        "search for documents",
        "在知识库里找找相关内容",
    ]

    for test_query in test_cases:
        print(f"\n查询: '{test_query}'")

        # 第一轮检索
        results = vs.search(
            query=test_query,
            limit=3,
            min_score=0.3,
            filter={"category": "query_pattern"}
        )

        if results:
            for r in results:
                score = getattr(r, 'score', 0)
                metadata = getattr(r, 'metadata', {})
                is_recursive = metadata.get('is_recursive', False)
                refined_query = metadata.get('refined_query', '')

                print(f"  第一轮: {metadata.get('id', 'N/A')} (score: {score:.3f})")
                print(f"    is_recursive: {is_recursive}")

                if is_recursive and refined_query:
                    print(f"    refined_query: {refined_query}")

                    # 第二轮检索
                    print(f"\n  第二轮检索: '{refined_query}'")
                    results2 = vs.search(
                        query=refined_query,
                        limit=3,
                        min_score=0.3,
                        filter={"category": "mcp_tool"}
                    )

                    if results2:
                        for j, r2 in enumerate(results2, 1):
                            score2 = getattr(r2, 'score', 0)
                            metadata2 = getattr(r2, 'metadata', {})
                            tool_name = f"{metadata2.get('server', '')}/{metadata2.get('name', '')}"
                            print(f"    {j}. {tool_name} (score: {score2:.3f})")
                    else:
                        print("    无结果")
        else:
            print("  第一轮无结果")

    return success_count, error_count


if __name__ == "__main__":
    success, error = index_query_patterns()

    if error > 0:
        sys.exit(1)
    else:
        print("\n✅ 所有查询模式索引成功！")
        sys.exit(0)
