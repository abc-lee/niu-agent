#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清理向量库中的错误工具注册

问题：
1. 基础工具（memory-server, vector-store）被错误地注册到向量库
   - 这些工具已在 BASE_MCP_TOOLS 固定注入，不应该在向量库中
2. 不存在的工具（page-agent-server/browse_web）被注册
   - 这个服务器不存在，是幽灵工具

清理策略：
1. 删除所有 memory-server 工具
2. 删除所有 vector-store 工具
3. 删除所有 page-agent-server 工具
4. 删除浏览器相关的查询模式
"""

import sys
import sqlite3
import json
from pathlib import Path

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from agent.vector_search import get_vector_search


def cleanup_tools():
    """清理错误注册的 MCP 工具"""
    logger.info("开始清理错误注册的 MCP 工具...")

    vs = get_vector_search()
    conn = vs._get_connection()

    if conn is None:
        logger.error("✗ 向量库连接失败")
        return

    # 要删除的工具列表
    tools_to_delete = [
        # 基础工具（已在 BASE_MCP_TOOLS 固定注入）
        "memory-server:remember",
        "memory-server:recall",
        "memory-server:update_memory",
        "memory-server:get_memory_stats",
        "memory-server:cleanup_memories",
        "memory-server:link_memories",
        "vector-store:add_document",
        "vector-store:search_documents",
        "vector-store:get_document",
        "vector-store:delete_document",
        "vector-store:list_documents",

        # 不存在的工具
        "page-agent-server:browse_web",
    ]

    deleted_count = 0
    for tool in tools_to_delete:
        doc_id = f"mcp_tool:{tool}"
        cursor = conn.execute("SELECT id FROM documents WHERE id = ?", (doc_id,))
        if cursor.fetchone():
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            logger.info(f"✓ 已删除: {tool}")
            deleted_count += 1
        else:
            logger.info(f"  跳过（不存在）: {tool}")

    conn.commit()
    logger.info(f"✓ 工具清理完成: 删除 {deleted_count} 个错误注册")


def cleanup_query_patterns():
    """清理浏览器相关的查询模式"""
    logger.info("开始清理浏览器相关的查询模式...")

    vs = get_vector_search()
    conn = vs._get_connection()

    if conn is None:
        logger.error("✗ 向量库连接失败")
        return

    # 要删除的查询模式
    patterns_to_delete = [
        "query_pattern:browser_search",
        "query_pattern:browser_open",
        "query_pattern:browser_browse",
        "query_pattern:browser_form",
        "query_pattern:browser_extract",
        "query_pattern:browser_book",
        "query_pattern:browser_news",
    ]

    deleted_count = 0
    for pattern_id in patterns_to_delete:
        cursor = conn.execute("SELECT id FROM documents WHERE id = ?", (pattern_id,))
        if cursor.fetchone():
            conn.execute("DELETE FROM documents WHERE id = ?", (pattern_id,))
            logger.info(f"✓ 已删除: {pattern_id}")
            deleted_count += 1
        else:
            logger.info(f"  跳过（不存在）: {pattern_id}")

    conn.commit()
    logger.info(f"✓ 查询模式清理完成: 删除 {deleted_count} 个错误注册")


def verify_cleanup():
    """验证清理结果"""
    logger.info("验证清理结果...")

    vs = get_vector_search()
    conn = vs._get_connection()

    if conn is None:
        logger.error("✗ 向量库连接失败")
        return

    # 检查是否还有 MCP 工具
    cursor = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE id LIKE 'mcp_tool:%'"
    )
    count = cursor.fetchone()[0]
    logger.info(f"向量库中 MCP 工具数量: {count}")

    if count > 0:
        logger.warning("⚠ 向量库中仍有 MCP 工具，列表如下：")
        cursor = conn.execute(
            "SELECT id FROM documents WHERE id LIKE 'mcp_tool:%'"
        )
        for row in cursor.fetchall():
            logger.warning(f"  - {row[0]}")


def main():
    """主函数"""
    print("=" * 70)
    print("向量库清理脚本")
    print("=" * 70)

    # 1. 清理错误注册的工具
    print("\n" + "-" * 70)
    cleanup_tools()

    # 2. 清理错误注册的查询模式
    print("\n" + "-" * 70)
    cleanup_query_patterns()

    # 3. 验证清理结果
    print("\n" + "-" * 70)
    verify_cleanup()

    print("\n" + "=" * 70)
    print("✓ 向量库清理完成")
    print("=" * 70)


if __name__ == "__main__":
    main()
