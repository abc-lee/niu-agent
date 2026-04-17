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
    """注册 MCP 工具描述到向量库（L1级别）

    注册策略：
    - 从 data/mcp_tools.json 读取工具定义
    - 从 config/mcp-servers.yaml 读取工具 visibility 配置
    - 只注册 visibility=dynamic 的工具到向量库
    - static 和 hidden 工具不存入向量库

    参考：docs/VECTOR_DB_INIT_CHECKLIST.md
    """
    logger.info("注册 MCP 工具描述...")

    # 读取工具定义
    json_file = Path(__file__).parent.parent / "data" / "mcp_tools.json"
    if not json_file.exists():
        logger.error("✗ 工具定义文件不存在，请先运行: python scripts/export_all_mcp_tools.py")
        logger.info("提示: 执行 'python scripts/export_all_mcp_tools.py' 生成工具定义")
        return

    with open(json_file, 'r', encoding='utf-8') as f:
        tools_by_server = json.load(f)

    # 展平为单个列表
    all_tools = []
    for server, tools in sorted(tools_by_server.items()):
        all_tools.extend(tools)

    logger.info(f"从 JSON 读取了 {len(all_tools)} 个工具定义")

    # 从 mcp-servers.yaml 读取 visibility 配置
    from pathlib import Path
    import yaml
    config_path = Path(__file__).parent.parent / "config" / "mcp-servers.yaml"
    visibility_map = {}  # "server/name" -> visibility
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            mcp_config = yaml.safe_load(f) or {}
        for server, server_cfg in mcp_config.items():
            if not isinstance(server_cfg, dict):
                continue
            tools_cfg = server_cfg.get("tools", {})
            for tool_name, tool_cfg in tools_cfg.items():
                full_name = f"{server}/{tool_name}"
                visibility_map[full_name] = tool_cfg.get("visibility", "dynamic")

    # 只注册 visibility=dynamic 的工具（static 和 hidden 不存入向量库）
    tools_to_register = []
    for tool in all_tools:
        full_name = f"{tool['server']}/{tool['name']}"
        vis = visibility_map.get(full_name, "dynamic")
        if vis == "dynamic":
            tools_to_register.append(tool)

    static_count = sum(1 for v in visibility_map.values() if v == "static")
    hidden_count = sum(1 for v in visibility_map.values() if v == "hidden")
    logger.info(f"需要注册 {len(tools_to_register)} 个工具（排除 {static_count} 个 static + {hidden_count} 个 hidden）")

    # 获取向量库连接
    from agent.vector_search import get_vector_search
    vs = get_vector_search()
    conn = vs._get_connection()

    if conn is None:
        logger.error("✗ 向量库连接失败")
        return

    # 注册每个工具
    registered = 0
    for i, tool in enumerate(tools_to_register, 1):
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
                logger.warning(f"[{i}/{len(tools_to_register)}] {tool['name']} - 向量生成失败")
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

            logger.info(f"[{i}/{len(tools_to_register)}] {tool['server']}/{tool['name']} - ✓")
            registered += 1
            time.sleep(0.5)  # 避免过载

        except Exception as e:
            logger.error(f"[{i}/{len(tools_to_register)}] {tool['name']} - ✗ {e}")

    logger.info(f"✓ MCP 工具注册完成: {registered}/{len(tools_to_register)}")


def register_query_patterns():
    """注册递归查询模式

    调用 scripts/index_query_patterns.py 的逻辑
    """
    logger.info("注册查询模式...")

    # 调用现有的索引脚本
    import subprocess
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "index_query_patterns.py")],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace'  # 替换无法解码的字符
    )

    if result.returncode == 0:
        logger.info("✓ 查询模式注册完成")
        # 打印输出（不包含模型加载日志）
        for line in result.stdout.split('\n'):
            if line.strip() and not line.startswith('Loading') and 'BertModel' not in line:
                logger.info(line)
    else:
        logger.error(f"✗ 查询模式注册失败: {result.stderr}")


def inject_system_manual():
    """注入系统说明书 L1 摘要"""
    logger.info("注入系统说明书 L1 摘要...")

    # 调用现有脚本
    import subprocess
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "inject_system_manual.py")],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace'  # 替换无法解码的字符
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
        encoding='utf-8',
        errors='replace',  # 替换无法解码的字符
        cwd=str(Path(__file__).parent)
    )

    if result.returncode == 0:
        logger.info("✓ Query Patterns 初始化完成")
        # 打印输出（过滤掉模型加载日志）
        for line in result.stdout.split('\n'):
            if line.strip() and not line.startswith('Loading') and 'BertModel' not in line:
                logger.info(line)
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
