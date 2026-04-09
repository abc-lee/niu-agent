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
    tools = [
        # scheduler-server
        {
            "server": "scheduler-server",
            "name": "schedule_task",
            "description": "Create scheduled tasks, reminders, and alarms. Supports one-time and recurring reminders. Use when user says 'remind me', 'alarm', 'call me at', 'every day at'. Parameters: content (task content), scheduled_at (trigger time in ISO format), event_type (event type), is_recurring (recurring task), cron_expr (cron expression for recurring tasks).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Task content"},
                    "scheduled_at": {"type": "string", "description": "Trigger time in ISO format"},
                    "event_type": {"type": "string", "description": "Event type (default: reminder)"},
                    "is_recurring": {"type": "boolean", "description": "Recurring task"},
                    "cron_expr": {"type": "string", "description": "Cron expression (required for recurring tasks)"}
                },
                "required": ["content", "scheduled_at"]
            }
        },
        {
            "server": "scheduler-server",
            "name": "list_scheduled_tasks",
            "description": "Query scheduled task list. Parameters: status (optional, filter by status pending/triggered/cancelled). Returns task list containing id, content, scheduled_at, is_recurring, cron_expr, status.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status (pending/triggered/cancelled)"}
                }
            }
        },
        {
            "server": "scheduler-server",
            "name": "cancel_task",
            "description": "Cancel a scheduled task. Parameters: task_id (task ID). Returns cancellation result.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID"}
                },
                "required": ["task_id"]
            }
        },
        {
            "server": "scheduler-server",
            "name": "update_task",
            "description": "Update scheduled task. Parameters: task_id (task ID), content (new task content), scheduled_at (new trigger time), cron_expr (new cron expression). Returns update result.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID"},
                    "content": {"type": "string", "description": "New task content"},
                    "scheduled_at": {"type": "string", "description": "New trigger time"},
                    "cron_expr": {"type": "string", "description": "New cron expression"}
                },
                "required": ["task_id"]
            }
        },
        # file-parser
        {
            "server": "file-parser",
            "name": "parse_file",
            "description": "Parse document files (PDF, Word, PPT, Excel, Markdown, HTML). Extracts text content and metadata. Returns structured document content.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path"},
                    "extract_images": {"type": "boolean", "description": "Extract images"}
                },
                "required": ["file_path"]
            }
        },
        # photo-server
        {
            "server": "photo-server",
            "name": "process_photo",
            "description": "Process photos: ingest, face recognition, automatic classification. Supports batch processing. Returns photo ID, face information, classification results.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "photo_path": {"type": "string", "description": "Photo path"},
                    "enable_face_recognition": {"type": "boolean", "description": "Enable face recognition"}
                },
                "required": ["photo_path"]
            }
        },
        {
            "server": "photo-server",
            "name": "search_photos",
            "description": "Search photos: by time, person, scene, location. Supports combined queries. Returns matching photo list.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "person_name": {"type": "string", "description": "Person name"},
                    "start_date": {"type": "string", "description": "Start date"},
                    "end_date": {"type": "string", "description": "End date"}
                }
            }
        },
        # kg-server
        {
            "server": "kg-server",
            "name": "add_document",
            "description": "Add document to knowledge graph. Automatically extracts entities and relations. Parameters: title (document title), content (document content), source (source), tags (tags).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Document title"},
                    "content": {"type": "string", "description": "Document content"},
                    "source": {"type": "string", "description": "Source"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags"}
                },
                "required": ["title", "content"]
            }
        },
        {
            "server": "kg-server",
            "name": "search_knowledge",
            "description": "Search in knowledge graph. Supports semantic search and relation queries. Returns relevant documents, entities, relations.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Number of results"}
                },
                "required": ["query"]
            }
        },
        # vector-store
        {
            "server": "vector-store",
            "name": "add_document",
            "description": "Add document to vector store. Parameters: id (document ID), content (content), metadata (metadata). Automatically generates vector embedding.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Document ID"},
                    "content": {"type": "string", "description": "Document content"},
                    "metadata": {"type": "object", "description": "Metadata"}
                },
                "required": ["id", "content"]
            }
        },
        {
            "server": "vector-store",
            "name": "search",
            "description": "Vector semantic search. Parameters: query (query text), limit (number of results), min_score (minimum score), filter (metadata filter). Returns matching document list.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Query text"},
                    "limit": {"type": "integer", "description": "Number of results (default: 10)"},
                    "min_score": {"type": "number", "description": "Minimum similarity (default: 0.5)"},
                    "filter": {"type": "object", "description": "Metadata filter conditions"}
                },
                "required": ["query"]
            }
        },
        # memory-server
        {
            "server": "memory-server",
            "name": "extract_memories",
            "description": "Extract memories from conversation. Automatically identifies important information and stores structurally. Returns extracted memory list.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "conversation": {"type": "string", "description": "Conversation content"}
                },
                "required": ["conversation"]
            }
        },
        {
            "server": "memory-server",
            "name": "search_memories",
            "description": "Search memories. Supports semantic search and time range queries. Returns matching memory list.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Number of results"}
                },
                "required": ["query"]
            }
        },
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
    patterns = [
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

    print("\n" + "=" * 70)
    print("✓ 向量库初始化完成")
    print("=" * 70)


if __name__ == "__main__":
    main()
