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
            "description": "创建定时任务，支持单次和循环任务。参数：content(任务内容)、scheduled_at(触发时间ISO格式)、event_type(事件类型)、is_recurring(是否循环)、cron_expr(cron表达式)。支持每天、每周、工作日等循环提醒。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "任务内容"},
                    "scheduled_at": {"type": "string", "description": "触发时间（ISO格式）"},
                    "event_type": {"type": "string", "description": "事件类型（默认reminder）"},
                    "is_recurring": {"type": "boolean", "description": "是否循环任务"},
                    "cron_expr": {"type": "string", "description": "cron表达式（循环任务必填）"}
                },
                "required": ["content", "scheduled_at"]
            }
        },
        {
            "server": "scheduler-server",
            "name": "list_scheduled_tasks",
            "description": "查询定时任务列表。参数：status(可选，筛选状态pending/triggered/cancelled)。返回任务列表，包含id、content、scheduled_at、is_recurring、cron_expr、status。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "筛选状态（pending/triggered/cancelled）"}
                }
            }
        },
        {
            "server": "scheduler-server",
            "name": "cancel_task",
            "description": "取消定时任务。参数：task_id(任务ID)。返回取消结果。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务ID"}
                },
                "required": ["task_id"]
            }
        },
        {
            "server": "scheduler-server",
            "name": "update_task",
            "description": "更新定时任务。参数：task_id(任务ID)、content(新任务内容)、scheduled_at(新触发时间)、cron_expr(新cron表达式)。返回更新结果。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务ID"},
                    "content": {"type": "string", "description": "新任务内容"},
                    "scheduled_at": {"type": "string", "description": "新触发时间"},
                    "cron_expr": {"type": "string", "description": "新cron表达式"}
                },
                "required": ["task_id"]
            }
        },
        # file-parser
        {
            "server": "file-parser",
            "name": "parse_file",
            "description": "解析文档文件（PDF、Word、PPT、Excel、Markdown、HTML等）。提取文本内容和元数据。返回结构化的文档内容。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径"},
                    "extract_images": {"type": "boolean", "description": "是否提取图片"}
                },
                "required": ["file_path"]
            }
        },
        # photo-server
        {
            "server": "photo-server",
            "name": "process_photo",
            "description": "处理照片：入库、人脸识别、自动分类。支持批量处理。返回照片ID、人脸信息、分类结果。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "photo_path": {"type": "string", "description": "照片路径"},
                    "enable_face_recognition": {"type": "boolean", "description": "是否启用人脸识别"}
                },
                "required": ["photo_path"]
            }
        },
        {
            "server": "photo-server",
            "name": "search_photos",
            "description": "搜索照片：按时间、人物、场景、地点等条件搜索。支持组合查询。返回匹配的照片列表。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索查询"},
                    "person_name": {"type": "string", "description": "人物姓名"},
                    "start_date": {"type": "string", "description": "开始日期"},
                    "end_date": {"type": "string", "description": "结束日期"}
                }
            }
        },
        # kg-server
        {
            "server": "kg-server",
            "name": "add_document",
            "description": "添加文档到知识图谱。自动提取实体和关系。参数：title(标题)、content(内容)、source(来源)、tags(标签)。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "文档标题"},
                    "content": {"type": "string", "description": "文档内容"},
                    "source": {"type": "string", "description": "来源"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "标签"}
                },
                "required": ["title", "content"]
            }
        },
        {
            "server": "kg-server",
            "name": "search_knowledge",
            "description": "在知识图谱中搜索。支持语义搜索和关系查询。返回相关文档、实体、关系。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索查询"},
                    "limit": {"type": "integer", "description": "返回数量限制"}
                },
                "required": ["query"]
            }
        },
        # vector-store
        {
            "server": "vector-store",
            "name": "add_document",
            "description": "添加文档到向量库。参数：id(文档ID)、content(内容)、metadata(元数据)。自动生成向量嵌入。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "文档ID"},
                    "content": {"type": "string", "description": "文档内容"},
                    "metadata": {"type": "object", "description": "元数据"}
                },
                "required": ["id", "content"]
            }
        },
        {
            "server": "vector-store",
            "name": "search",
            "description": "向量语义搜索。参数：query(查询文本)、limit(返回数量)、min_score(最低分数)、filter(元数据过滤)。返回匹配的文档列表。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "查询文本"},
                    "limit": {"type": "integer", "description": "返回数量（默认10）"},
                    "min_score": {"type": "number", "description": "最低相似度（默认0.5）"},
                    "filter": {"type": "object", "description": "元数据过滤条件"}
                },
                "required": ["query"]
            }
        },
        # config-manager
        {
            "server": "config-manager",
            "name": "read_config",
            "description": "读取用户配置或记忆。参数：config_type(config/preferences/memory)。返回配置内容。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "config_type": {"type": "string", "description": "配置类型（config/preferences/memory）"}
                },
                "required": ["config_type"]
            }
        },
        {
            "server": "config-manager",
            "name": "write_config",
            "description": "写入用户配置或记忆。参数：config_type(config/preferences/memory)、data(配置数据)。返回操作结果。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "config_type": {"type": "string", "description": "配置类型"},
                    "data": {"type": "object", "description": "配置数据"}
                },
                "required": ["config_type", "data"]
            }
        },
        # memory-server
        {
            "server": "memory-server",
            "name": "extract_memories",
            "description": "从对话中提取记忆。自动识别重要信息并结构化存储。返回提取的记忆列表。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "conversation": {"type": "string", "description": "对话内容"}
                },
                "required": ["conversation"]
            }
        },
        {
            "server": "memory-server",
            "name": "search_memories",
            "description": "搜索记忆。支持语义搜索和时间范围查询。返回匹配的记忆列表。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索查询"},
                    "limit": {"type": "integer", "description": "返回数量"}
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
                logger.warning(f"[{i}/{len(tools)}] {tool['name']} - 向量生成失败")
                continue

            embedding_blob = np.array(embedding, dtype=np.float32).tobytes()

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

    # 4. 注入系统说明书
    print("\n" + "-" * 70)
    inject_system_manual()

    print("\n" + "=" * 70)
    print("✓ 向量库初始化完成")
    print("=" * 70)


if __name__ == "__main__":
    main()
