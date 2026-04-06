#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量优化所有MCP工具的L1描述

统一格式：功能 + 用户场景关键词
用法：python scripts/optimize_all_mcp_tools.py
"""

import sys
import sqlite3
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# MCP工具L1描述映射（关键词+用户场景）
TOOL_L1_MAPPINGS = {
    # config-manager
    "config-manager:add_user_preference": "添加用户偏好设置。用户说'记住我的偏好'、'我喜欢...'、'以后就这样'时使用",
    "config-manager:complete_setup": "完成初始设置向导。首次运行时设置工作目录和用户信息",
    "config-manager:copy_to_path": "复制文件到指定路径。用户说'复制到'、'拷贝到'时使用",
    "config-manager:get_full_memory": "获取完整系统记忆配置。用于调试或查看所有设置",
    "config-manager:get_identity": "获取助手身份设置（名字、性格）",
    "config-manager:get_llm_config": "获取LLM配置信息。用户说'查看API配置'、'当前模型是什么'时使用",
    "config-manager:get_storage_config": "获取存储配置（工作目录、数据库路径）",
    "config-manager:get_user_info": "获取用户信息（名字、偏好设置）",
    "config-manager:get_workspace": "获取工作目录路径。用户说'知识库在哪'、'文件存在哪'时使用",
    "config-manager:is_first_run": "检查是否首次运行。判断是否需要显示设置向导",
    "config-manager:list_files_in_workspace": "列出工作目录中的所有文件。用户说'有哪些文件'、'查看知识库文件'时使用",
    "config-manager:list_llm_presets": "列出所有可用的LLM预设模型列表",
    "config-manager:mkdir": "创建目录。用户说'新建文件夹'、'创建目录'时使用",
    "config-manager:move_to_path": "移动文件到指定路径。用户说'移动到'、'转移文件'时使用",
    "config-manager:set_llm_config": "设置LLM配置。用户说'切换模型'、'更换API'、'设置模型'时使用",
    "config-manager:set_storage_config": "设置存储配置（工作目录、数据库路径）",
    "config-manager:set_user_info": "设置用户信息。用户说'我叫...'、'我的名字是'时使用",
    "config-manager:set_workspace": "设置工作目录。用户说'换个目录'、'改存储位置'时使用",
    "config-manager:test_llm_connection": "测试LLM连接是否正常。用户说'测试连接'、'API能用吗'时使用",
    "config-manager:update_identity": "更新助手身份。用户说'改个名字'、'换个性格'、'叫我什么'时使用",

    # file-parser
    "file-parser:list_supported_formats": "列出支持的文件格式。用户说'能解析什么文件'、'支持哪些格式'时使用",
    "file-parser:parse_file": "解析文档文件提取文本。用户说'读取PDF'、'解析Word'、'提取文档内容'时使用",

    # kg-server
    "kg-server:create_concept": "在知识图谱中创建概念节点",
    "kg-server:create_document": "在知识图谱中创建文档节点",
    "kg-server:create_entity": "在知识图谱中创建实体节点（人物、组织等）",
    "kg-server:get_document": "从知识图谱获取文档内容",
    "kg-server:get_related_concepts": "获取文档相关的概念列表",
    "kg-server:get_related_entities": "获取文档提到的实体列表",
    "kg-server:link_document_concept": "关联文档和概念",
    "kg-server:link_document_entity": "关联文档和实体",
    "kg-server:link_entities": "创建实体间的关系",
    "kg-server:list_documents": "列出知识图谱中的所有文档",
    "kg-server:query_graph": "执行Cypher图查询。高级用户使用",
    "kg-server:search_documents": "搜索知识图谱文档。用户说'搜索知识'、'查找文档'时使用",

    # memory-server
    "memory-server:delete_memory": "删除记忆。用户说'删除记忆'、'忘掉这个'时使用",
    "memory-server:list_memories": "列出已存储的记忆列表",
    "memory-server:recall": "搜索相关记忆。用户说'我记得...'、'以前说过'、'回忆一下'时使用",

    # photo-server
    "photo-server:cleanup_deleted_photos": "清理已删除照片的数据库记录。维护数据库时使用",
    "photo-server:delete_person": "删除人物。用户说'删除这个人'、'移除人物'时使用",
    "photo-server:get_person_photos": "获取人物的多张照片。用户说'换一张'、'看不清'、'还有其他照片吗'时使用",
    "photo-server:get_unnamed_persons": "获取未命名人物列表。用户说'有哪些陌生人'、'未命名的人'时使用",
    "photo-server:ingest_document": "文档入库。用户说'入库文档'、'导入文件'、'拖入文件'时使用",
    "photo-server:ingest_documents": "批量文档入库。用户拖入多个文件时使用",
    "photo-server:ingest_photo": "照片入库（带人脸识别）。用户说'入库照片'、'导入照片'、'拖入照片'时使用",
    "photo-server:ingest_photos": "批量照片入库。用户拖入多张照片时使用",
    "photo-server:merge_persons": "合并两个人物。用户说'这是同一个人'、'合并人物'时使用",
    "photo-server:name_person": "为人物命名。用户说'这是张三'、'这个人叫李四'、'标记人物'时使用",
    "photo-server:search_persons": "搜索人物。用户说'找张三的照片'、'搜索某人'、'谁的照片'时使用",
    "photo-server:store_document_l1": "存储文档L1摘要到向量库",
    "photo-server:store_documents_l1": "批量存储文档L1摘要",
    "photo-server:unload_face_model": "卸载人脸模型释放内存。系统空闲时自动调用",

    # scheduler-server (已优化)
    "scheduler-server:schedule_task": "设置提醒、闹钟、定时任务。用户说'提醒我'、'定闹钟'、'几分钟后提醒'、'每天几点提醒'时使用",
    "scheduler-server:list_scheduled_tasks": "查询定时任务列表、查看提醒。用户说'我有哪些提醒'、'查看定时任务'、'已设置的闹钟'时使用",
    "scheduler-server:cancel_task": "取消定时任务、删除提醒。用户说'取消提醒'、'删除定时任务'、'关闭闹钟'时使用",
    "scheduler-server:update_task": "修改定时任务、调整提醒时间。用户说'修改提醒时间'、'改成几点'、'调整定时任务'时使用",

    # vector-store
    "vector-store:add_document": "添加文档到向量库。存储知识供后续搜索",
    "vector-store:count_documents": "统计向量库中的文档数量",
    "vector-store:delete_document": "删除向量库文档。用户说'删除知识'、'清除文档'时使用",
    "vector-store:get_document": "从向量库获取文档内容",
    "vector-store:list_documents": "列出向量库中的文档",
    "vector-store:search_documents": "语义搜索文档。用户说'搜索知识'、'查找相关内容'、'语义搜索'时使用",
}


def optimize_all_tools():
    """批量优化所有MCP工具的L1描述"""

    print("=" * 60)
    print("Optimize All MCP Tools (L1 Descriptions)")
    print("=" * 60)

    from agent.vector_search import VectorSearchAdapter
    import numpy as np
    import time

    adapter = VectorSearchAdapter()
    db_path = adapter.db_path

    print(f"\nVector DB: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. 获取所有MCP工具
    cursor.execute("""
        SELECT id, content, metadata
        FROM documents
        WHERE id LIKE 'mcp_tool:%'
        ORDER BY id
    """)

    tools = cursor.fetchall()
    print(f"\nFound {len(tools)} MCP tools")

    # 2. 优化每个工具
    print("\nOptimizing tools (batch mode with delay)...\n")

    optimized = 0
    skipped = 0
    failed = 0

    output_lines = []
    batch_size = 5  # 每批处理5个工具（避免超时）
    pause_time = 10  # 每批后暂停10秒

    for i, (doc_id, old_content, metadata_json) in enumerate(tools, 1):
        # 提取工具名（去掉 mcp_tool: 前缀）
        tool_key = doc_id.replace("mcp_tool:", "")

        # 获取新的L1描述
        if tool_key in TOOL_L1_MAPPINGS:
            l1_description = TOOL_L1_MAPPINGS[tool_key]

            try:
                # 获取新向量
                embedding = adapter._get_embedding(l1_description)
                if embedding is None:
                    output_lines.append(f"[{i}/{len(tools)}] {tool_key} - [FAIL] Failed to get embedding")
                    failed += 1
                    continue

                embedding_blob = np.array(embedding, dtype=np.float32).tobytes()

                # 更新content和embedding
                cursor.execute("""
                    UPDATE documents
                    SET content = ?, embedding = ?
                    WHERE id = ?
                """, (l1_description, embedding_blob, doc_id))

                optimized += 1
                output_lines.append(f"[{i}/{len(tools)}] {tool_key} - [OK]")

            except Exception as e:
                output_lines.append(f"[{i}/{len(tools)}] {tool_key} - [ERROR] {e}")
                failed += 1

        else:
            skipped += 1
            output_lines.append(f"[{i}/{len(tools)}] {tool_key} - [SKIP] No mapping")

        # 每批处理完后提交、暂停
        if (i % batch_size) == 0:
            conn.commit()
            print(f"Progress: {i}/{len(tools)} tools processed, pausing {pause_time}s...")
            time.sleep(pause_time)  # 暂停让embedding service恢复

    # 3. 提交更改
    conn.commit()
    conn.close()

    # 4. 输出结果到文件
    with open('optimize_result.txt', 'w', encoding='utf-8') as f:
        f.write('\n'.join(output_lines))
        f.write('\n\n')
        f.write('=' * 60 + '\n')
        f.write('Optimization Complete!\n')
        f.write('=' * 60 + '\n')
        f.write(f'Total tools: {len(tools)}\n')
        f.write(f'Optimized: {optimized}\n')
        f.write(f'Skipped: {skipped}\n')
        f.write(f'Failed: {failed}\n')
        f.write('=' * 60 + '\n')

    print(f"\nOptimization complete! Result saved to optimize_result.txt")
    print(f"Total: {len(tools)}, Optimized: {optimized}, Skipped: {skipped}, Failed: {failed}")


if __name__ == "__main__":
    optimize_all_tools()
