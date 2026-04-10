#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
初始化向量库（符合L0/L1/L2规范）

用途：
1. 创建向量库表结构
2. 同步 Skills 到向量库（L1级别）
3. 注册 MCP 工具描述到向量库（L1级别）
4. 注入系统说明书 L1 摘要

执行条件：
- 服务已停止（避免并发写入）
- Python 环境已配置
- embedding 模型可用
"""

import sys
import os
import sqlite3
import json
import time
from pathlib import Path

# 设置 UTF-8 输出（修复 Windows 中文显示问题）
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
import numpy as np
from agent.vector_search import VectorSearchAdapter


def get_vector_db_path() -> str:
    """获取向量库路径"""
    # 1. 尝试从 memory.json 读取工作目录
    memory_path = Path.home() / ".niu" / "memory.json"
    if memory_path.exists():
        try:
            memory = json.loads(memory_path.read_text(encoding="utf-8"))
            workspace_path = memory.get("workspace", {}).get("path")
            if workspace_path and Path(workspace_path).exists():
                return str(Path(workspace_path) / "vectors.db")
        except Exception:
            pass

    # 2. 降级到 home 目录
    return str(Path.home() / ".niu" / "vectors.db")


def init_vector_db(db_path: str):
    """初始化向量库表结构"""
    logger.info(f"初始化向量库: {db_path}")

    # 确保目录存在
    db_dir = Path(db_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    # 创建数据库连接
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            embedding BLOB,
            metadata TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_id ON documents(id)")
    conn.commit()
    conn.close()

    logger.info("✓ 向量库表结构已创建")


def sync_skills():
    """同步 Skills 到向量库（L1级别）"""
    logger.info("同步 Skills 到向量库...")

    from agent.injector.sync import get_skill_sync

    skill_sync = get_skill_sync(auto_start=False)
    added, updated, deleted = skill_sync.scan_and_sync()

    logger.info(f"✓ Skills 同步完成: 新增 {added}, 更新 {updated}, 删除 {deleted}")


def register_mcp_tools():
    """注册 MCP 工具描述到向量库（L1级别）"""
    logger.info("注册 MCP 工具描述...")

    # 定义所有 MCP 工具
    # 分层：
    # - 主Agent基础工具（11个）：memory-server (6) + vector-store (5)
    # - 子Agent专用工具：photo-server, scheduler-server, kg-server等
    tools = [
        # ==================== 主Agent基础工具 ====================
        # memory-server (6个)
        {
            "server": "memory-server",
            "name": "remember",
            "description": "Save long-term memory with auto-generated L0/L1/L2 layers. Use when user says 'remember this', 'remember what I like', 'keep this in mind'. Parameters: content (memory content), metadata (optional metadata including memory_type, importance).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Memory content"},
                    "metadata": {"type": "object", "description": "Optional metadata"}
                },
                "required": ["content"]
            }
        },
        {
            "server": "memory-server",
            "name": "recall",
            "description": "Retrieve memories using semantic search. Use when user says 'recall previous memories', 'what did I say before', 'search memories'. Parameters: query (search query), limit (number of results), memory_type (optional filter).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Number of results"},
                    "memory_type": {"type": "string", "description": "Filter by memory type"}
                },
                "required": ["query"]
            }
        },
        {
            "server": "memory-server",
            "name": "update_memory",
            "description": "Update existing memory. Parameters: memory_id (memory ID), content (new content), metadata (new metadata).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "Memory ID"},
                    "content": {"type": "string", "description": "New content"},
                    "metadata": {"type": "object", "description": "New metadata"}
                },
                "required": ["memory_id"]
            }
        },
        {
            "server": "memory-server",
            "name": "get_memory_stats",
            "description": "Get memory statistics. Returns total count, count by type, oldest/newest memory info.",
            "input_schema": {
                "type": "object",
                "properties": {}
            }
        },
        {
            "server": "memory-server",
            "name": "cleanup_memories",
            "description": "Cleanup expired or outdated memories. Parameters: older_than_days (days threshold), dry_run (preview mode).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "older_than_days": {"type": "integer", "description": "Days threshold"},
                    "dry_run": {"type": "boolean", "description": "Preview mode"}
                }
            }
        },
        {
            "server": "memory-server",
            "name": "link_memories",
            "description": "Create association between two memories. Parameters: memory_a_id, memory_b_id, relation_type (e.g., 'related_to', 'causes', 'contradicts').",
            "input_schema": {
                "type": "object",
                "properties": {
                    "memory_a_id": {"type": "string", "description": "First memory ID"},
                    "memory_b_id": {"type": "string", "description": "Second memory ID"},
                    "relation_type": {"type": "string", "description": "Relation type"}
                },
                "required": ["memory_a_id", "memory_b_id"]
            }
        },

        # vector-store (5个)
        {
            "server": "vector-store",
            "name": "add_document",
            "description": "Add document to vector store with semantic embedding. Use when user says 'add this document', 'save this document'. Parameters: id (document ID), content (content), metadata (optional metadata), file_path (optional file path).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Document ID"},
                    "content": {"type": "string", "description": "Document content"},
                    "metadata": {"type": "object", "description": "Metadata"},
                    "file_path": {"type": "string", "description": "File path"}
                },
                "required": ["id", "content"]
            }
        },
        {
            "server": "vector-store",
            "name": "search_documents",
            "description": "Semantic search in vector store. Use when user says 'search for documents', 'find documents about', 'retrieve knowledge'. Parameters: query (search query), limit (number of results), min_score (minimum similarity), filter (metadata filter).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Number of results"},
                    "min_score": {"type": "number", "description": "Minimum similarity"},
                    "filter": {"type": "object", "description": "Metadata filter"}
                },
                "required": ["query"]
            }
        },
        {
            "server": "vector-store",
            "name": "get_document",
            "description": "Get document by ID. Parameters: id (document ID).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Document ID"}
                },
                "required": ["id"]
            }
        },
        {
            "server": "vector-store",
            "name": "delete_document",
            "description": "Delete document by ID. Parameters: id (document ID).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Document ID"}
                },
                "required": ["id"]
            }
        },
        {
            "server": "vector-store",
            "name": "list_documents",
            "description": "List all documents, optionally filtered by metadata. Parameters: filter (metadata filter), limit (max results).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "filter": {"type": "object", "description": "Metadata filter"},
                    "limit": {"type": "integer", "description": "Max results"}
                }
            }
        },

        # ==================== 以下工具不索引到向量库 ====================
        # scheduler-server (4个) - 子Agent专用，主Agent通过chat-with-event-manager委托
        # file-parser (2个) - 底层操作，不暴露
        # photo-server (14个) - 子Agent专用，主Agent通过chat-with-file-processor委托
        # kg-server (12个) - 底层操作，不暴露
        # config-manager (20个) - 已删除，用bash+file操作替代

        # 注意：以上工具不会在向量库中索引，主Agent无法通过向量检索发现这些工具
        # 这是符合架构设计的：
        # - 主Agent只直接调用基础工具（memory-server + vector-store）
        # - 其他操作通过子Agent委托
    ]

    # 获取向量库连接
    from agent.vector_search import get_vector_search
    vs = get_vector_search()
    conn = vs._get_connection()

    if conn is None:
        logger.error("✗ 向量库连接失败")
        return

    # 导入 numpy
    import numpy as np

    registered = 0
    for i, tool in enumerate(tools, 1):
        try:
            # 构建文档ID和内容
            doc_id = f"mcp_tool:{tool['server']}:{tool['name']}"
            content = f"{tool['name']}: {tool['description']}"

            # 元数据（符合L1规范 v3.0）
            metadata = {
                "level": "l1",
                "category": "mcp_tool",
                "language": "en",
                "name": tool["name"],
                "server": tool["server"],
                "description": tool["description"],
                "input_schema": tool["input_schema"]
            }

            # 获取向量
            embedding = vs._get_embedding(content)
            if embedding is None:
                logger.warning(f"[{i}/{len(tools)}] {tool['name']} - 向量生成失败")
                continue

            # L2归一化
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

            logger.info(f"[{i}/{len(tools)}] {tool['name']} - ✓")
            registered += 1
            time.sleep(0.5)  # 避免过载

        except Exception as e:
            logger.error(f"[{i}/{len(tools)}] {tool['name']} - ✗ {e}")

    logger.info(f"✓ MCP 工具注册完成: {registered}/{len(tools)}")


def register_query_patterns():
    """注册递归查询模式"""
    logger.info("注册查询模式...")

    # 查询模式定义（符合L1规范 v3.0）
    # 包含英文和中文查询模式，支持主Agent基础工具
    patterns = [
        # ==================== 记忆管理类（英文）====================
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
                "description": "User wants to recall previous memories"
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

        # ==================== 记忆管理类（中文）====================
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

        # ==================== 文档检索类（英文）====================
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

        # ==================== 文档检索类（中文）====================
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

        # ==================== 原有查询模式（保留）====================
        {
            "id": "query_pattern:reminder_time",
            "content": "remind me in X minutes",
            "metadata": {
                "level": "l1",
                "category": "query_pattern",
                "language": "en",
                "type": "query_pattern",
                "is_recursive": True,
                "refined_query": "schedule task",
                "description": "Remind user after X minutes"
            }
        },
        {
            "id": "query_pattern:reminder_short",
            "content": "remind me later",
            "metadata": {
                "level": "l1",
                "category": "query_pattern",
                "language": "en",
                "type": "query_pattern",
                "is_recursive": True,
                "refined_query": "schedule task",
                "description": "Remind user shortly"
            }
        },
        {
            "id": "query_pattern:reminder_daily",
            "content": "remind me every day",
            "metadata": {
                "level": "l1",
                "category": "query_pattern",
                "language": "en",
                "type": "query_pattern",
                "is_recursive": True,
                "refined_query": "recurring task",
                "description": "Daily recurring reminder"
            }
        },
        {
            "id": "query_pattern:reminder_workday",
            "content": "remind me on workdays",
            "metadata": {
                "level": "l1",
                "category": "query_pattern",
                "language": "en",
                "type": "query_pattern",
                "is_recursive": True,
                "refined_query": "workday reminder recurring task",
                "description": "Workday recurring reminder"
            }
        },
        {
            "id": "query_pattern:reminder_en_time",
            "content": "set a reminder",
            "metadata": {
                "level": "l1",
                "category": "query_pattern",
                "language": "en",
                "type": "query_pattern",
                "is_recursive": True,
                "refined_query": "schedule task"
            }
        },
        {
            "id": "query_pattern:reminder_en_alarm",
            "content": "set alarm",
            "metadata": {
                "level": "l1",
                "category": "query_pattern",
                "language": "en",
                "type": "query_pattern",
                "is_recursive": True,
                "refined_query": "schedule alarm"
            }
        },
        {
            "id": "query_pattern:document_ingest",
            "content": "ingest this document",
            "metadata": {
                "level": "l1",
                "category": "query_pattern",
                "language": "en",
                "type": "query_pattern",
                "is_recursive": True,
                "refined_query": "document ingestion",
                "description": "Ingest document to knowledge base"
            }
        },
        {
            "id": "query_pattern:photo_ingest",
            "content": "process photos",
            "metadata": {
                "level": "l1",
                "category": "query_pattern",
                "language": "en",
                "type": "query_pattern",
                "is_recursive": True,
                "refined_query": "photo ingestion",
                "description": "Ingest photos to gallery"
            }
        },
    ]

    # 获取向量库路径
    db_path = get_vector_db_path()
    vs = VectorSearchAdapter(db_path)

    # 连接数据库
    conn = sqlite3.connect(db_path)
    registered = 0

    for i, pattern in enumerate(patterns, 1):
        try:
            # 获取向量
            embedding = vs._get_embedding(pattern["content"])
            if embedding is None:
                logger.warning(f"[{i}/{len(patterns)}] {pattern['id']} - 向量生成失败")
                continue

            # L2归一化
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
                (pattern["id"], pattern["content"], embedding_blob,
                 json.dumps(pattern["metadata"], ensure_ascii=False)),
            )
            conn.commit()

            logger.info(f"[{i}/{len(patterns)}] {pattern['id']} - ✓")
            registered += 1
            time.sleep(0.3)  # 避免过载

        except Exception as e:
            logger.error(f"[{i}/{len(patterns)}] {pattern['id']} - ✗ {e}")

    logger.info(f"✓ 查询模式注册完成: {registered}/{len(patterns)}")


def inject_system_manual():
    """注入系统说明书 L1 摘要"""
    logger.info("注入系统说明书 L1 摘要...")

    # 调用现有脚本
    import subprocess
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "inject_system_manual.py")],
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        logger.info("✓ 系统说明书注入完成")
    else:
        logger.error(f"✗ 系统说明书注入失败: {result.stderr}")


def init_query_patterns():
    """
    初始化 Query Patterns（通过 TDD 流水线生成）

    注意：需要 LLM API 支持，会调用 pipeline.py 生成 patterns
    """
    from pathlib import Path
    logger.info("初始化 Query Patterns...")

    # 尝试运行流水线脚本
    pipeline_path = Path(__file__).parent / "query_pattern" / "pipeline.py"
    if not pipeline_path.exists():
        logger.warning("Query Pattern 流水线脚本不存在，跳过")
        return

    import subprocess
    result = subprocess.run(
        [sys.executable, str(pipeline_path)],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent)
    )

    if result.returncode == 0:
        logger.info("✓ Query Patterns 初始化完成")
        print(result.stdout)  # 打印流水线输出
    else:
        logger.error(f"✗ Query Patterns 初始化失败: {result.stderr}")


def main():
    """主函数"""
    print("=" * 70)
    print("向量库初始化脚本（符合L0/L1/L2规范）")
    print("=" * 70)

    # 检查服务是否运行
    try:
        import requests
        resp = requests.get("http://127.0.0.1:9876/health", timeout=1)
        logger.warning("⚠ 检测到服务正在运行，建议先停止服务再执行初始化")
        confirm = input("\n服务运行中，是否继续？ [y/N]: ")
        if confirm.lower() != 'y':
            logger.info("已取消")
            return
    except:
        pass  # 服务未运行，继续

    # 1. 初始化向量库
    db_path = get_vector_db_path()
    init_vector_db(db_path)

    # 2. 同步 Skills
    print("\n" + "-" * 70)
    sync_skills()

    # 3. 注册 MCP 工具
    print("\n" + "-" * 70)
    register_mcp_tools()

    # 4. 注册查询模式
    print("\n" + "-" * 70)
    register_query_patterns()

    # 5. 注入系统说明书
    print("\n" + "-" * 70)
    inject_system_manual()

    # 6. 初始化 Query Patterns（可选，需要 LLM API，耗时较长）
    print("\n" + "-" * 70)
    print("Query Patterns 初始化需要 LLM API，耗时较长（约 5-10 分钟）")
    confirm = input("是否初始化 Query Patterns？[y/N]: ")
    if confirm.lower() == 'y':
        init_query_patterns()

    print("\n" + "=" * 70)
    print("✓ 向量库初始化完成")
    print("=" * 70)


if __name__ == "__main__":
    main()
