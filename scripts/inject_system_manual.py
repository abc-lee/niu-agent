#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
系统说明书注入脚本（符合L0/L1/L2规范）

用途：将系统说明书的L1摘要注入向量库
规范：docs/implementation-L0L1L2.md

执行条件：服务已停止（避免并发写入）
"""

import sys
import os
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


def inject_system_manual_l1():
    """
    注入系统说明书的L1摘要到向量库

    L1格式：{标题}|{关键词}|{摘要}|{实体}|{类型}|{指针}
    指针格式：file:docs/SYSTEM_MANUAL.md
    """

    manual_path = Path(__file__).parent.parent / "docs" / "SYSTEM_MANUAL.md"

    if not manual_path.exists():
        logger.error(f"系统说明书不存在: {manual_path}")
        return False

    logger.info("=" * 60)
    logger.info("注入系统说明书 L1 摘要到向量库")
    logger.info("=" * 60)
    logger.info(f"文件位置: {manual_path}")

    # 准备多个L1摘要，覆盖不同场景（英文，符合L1规范v2.0）
    l1_summaries = [
        {
            "title": "System Startup Troubleshooting",
            "keywords": "startup,slow,stuck,model download,port conflict",
            "summary": "System startup takes 25 seconds including model loading and MCP tool preload. First startup downloads models requiring 15-30 minutes. If startup hangs, check if port 9876 is occupied or try disabling GPU acceleration.",
            "entities": "",
            "type": "system_manual",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "五、故障排查 > 5.1 启动问题"
        },
        {
            "title": "Face Recognition Performance Optimization",
            "keywords": "face recognition,slow,GPU acceleration,CUDA,DirectML",
            "summary": "Face recognition CPU mode takes 2-3 seconds per photo. GPU acceleration provides 10x speedup. Install onnxruntime-gpu (NVIDIA GPU+CUDA required) or onnxruntime-directml (any Windows GPU) to enable GPU acceleration.",
            "entities": "",
            "type": "system_manual",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "五、故障排查 > 5.2 人脸识别问题"
        },
        {
            "title": "GPU Acceleration Configuration Guide",
            "keywords": "GPU,CUDA,DirectML,performance,acceleration,configuration",
            "summary": "System auto-detects GPU and selects optimal acceleration: CUDA (NVIDIA GPU) > DirectML (Windows GPU) > CPU. Install corresponding onnxruntime version to enable. No additional configuration required.",
            "entities": "",
            "type": "system_manual",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "三、依赖管理 > 3.2 GPU支持策略"
        },
        {
            "title": "Scheduled Task Troubleshooting",
            "keywords": "scheduled task,reminder,failed,recurring task,notification",
            "summary": "Scheduled tasks stored in data/scheduled_tasks.db. Background thread checks every minute. If reminder not received, check task status and system notification settings. Recurring tasks require correct cron expression.",
            "entities": "",
            "type": "system_manual",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "五、故障排查 > 5.3 定时任务问题"
        },
        {
            "title": "Data Storage and Backup",
            "keywords": "data,backup,storage,database,recovery",
            "summary": "All user data stored in data/ directory including chat history, knowledge base, knowledge graph, scheduled tasks. Backup requires only copying data/ and ~/.niu/ directories. Database files can be periodically cleaned and compressed.",
            "entities": "",
            "type": "system_manual",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "五、故障排查 > 5.5 数据问题"
        },
        {
            "title": "Memory Optimization Strategy",
            "keywords": "memory,usage,optimization,model unload",
            "summary": "System memory usage approximately 1.5GB. Face recognition model auto-unloads after 5 minutes idle releasing 326MB. If insufficient memory, disable GPU acceleration or reduce MCP servers. Recommended memory 8GB+.",
            "entities": "",
            "type": "system_manual",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "六、性能优化 > 6.1 内存优化"
        },
        {
            "title": "LLM Configuration Guide",
            "keywords": "LLM,configuration,API,Ollama,cloud",
            "summary": "Supports local Ollama and cloud APIs (OpenAI/Anthropic etc). Configuration file at config/user-config.json. Fill in provider, model, api_key. First startup provides guided configuration.",
            "entities": "",
            "type": "system_manual",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "七、用户指南 > 7.2 LLM配置"
        },
        {
            "title": "System Architecture Overview",
            "keywords": "architecture,single process,module,directory structure",
            "summary": "Single-process architecture with embedding and scheduler integrated into main process. All modules run on port 9876. Directories include main program, models, config, user data.",
            "entities": "",
            "type": "system_manual",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "二、架构设计"
        },
    ]

    # 获取向量库连接
    from agent.vector_search import get_vector_search
    vs = get_vector_search()
    conn = vs._get_connection()

    if conn is None:
        logger.error("向量库连接失败")
        return False

    success_count = 0

    for i, l1 in enumerate(l1_summaries, 1):
        logger.info(f"注入 L1 #{i}: {l1['title']}")

        # 构建L1内容（符合规范格式）
        # {标题}|{关键词}|{摘要}|{实体}|{类型}|{指针}
        content = "|".join([
            l1["title"],
            l1["keywords"],
            l1["summary"],
            l1["entities"],
            l1["type"],
            l1["pointer"]
        ])

        # 文档ID
        doc_id = f"system_manual:{l1['section'].replace(' > ', '-')}"

        # 元数据（符合L1规范 v3.0）
        metadata = {
            "level": "l1",
            "category": "document",
            "language": "en",
            "resource_type": "system_manual",
            "section": l1["section"],
            "title": l1["title"],
        }

        # 获取向量
        embedding = vs._get_embedding(content)
        if embedding is None:
            logger.error(f"向量生成失败: {l1['title']}")
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

        logger.info(f"  ✓ 已注入 (ID: {doc_id[:50]}...)")
        success_count += 1
        time.sleep(0.5)

    logger.info("=" * 60)
    logger.info(f"✓ 完成：{success_count}/{len(l1_summaries)} 条L1摘要已注入")
    logger.info("=" * 60)

    logger.info("\n使用方式：")
    logger.info("1. 用户提问（如：启动慢、人脸识别慢、GPU配置）")
    logger.info("2. 动态注入系统检索到匹配的L1摘要")
    logger.info("3. 根据L1指针（file:docs/SYSTEM_MANUAL.md）读取文件")
    logger.info("4. 根据L1的section字段定位到相关章节")
    logger.info("5. 主Agent读取完整内容并提供详细解决方案")

    return True


if __name__ == "__main__":
    # 检查服务是否运行
    try:
        import requests
        resp = requests.get("http://127.0.0.1:9876/health", timeout=1)
        logger.warning("⚠ 检测到服务正在运行，建议先停止服务再执行注入")
        confirm = input("\n服务运行中，是否继续？ [y/N]: ")
        if confirm.lower() != 'y':
            logger.info("已取消")
            sys.exit(0)
    except:
        pass  # 服务未运行，继续

    inject_system_manual_l1()
