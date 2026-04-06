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

    # 准备多个L1摘要，覆盖不同场景
    l1_summaries = [
        {
            "title": "系统启动问题解决方案",
            "keywords": "启动,慢,卡顿,下载模型,端口占用",
            "summary": "系统启动需要25秒，包括模型加载和MCP工具预加载。首次启动会下载模型，可能需要15-30分钟。如果启动卡住，检查端口9876是否被占用，或尝试禁用GPU加速。",
            "entities": "",
            "type": "系统手册",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "五、故障排查 > 5.1 启动问题"
        },
        {
            "title": "人脸识别性能优化指南",
            "keywords": "人脸识别,慢,GPU加速,CUDA,DirectML",
            "summary": "人脸识别CPU模式每张照片需要2-3秒，GPU加速可提速10倍。安装onnxruntime-gpu（需NVIDIA GPU+CUDA）或onnxruntime-directml（Windows任意GPU）即可启用GPU加速。",
            "entities": "",
            "type": "系统手册",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "五、故障排查 > 5.2 人脸识别问题"
        },
        {
            "title": "GPU加速配置说明",
            "keywords": "GPU,CUDA,DirectML,性能,加速,配置",
            "summary": "系统自动检测GPU并选择最佳加速方案：CUDA（NVIDIA GPU）> DirectML（Windows GPU）> CPU。安装对应的onnxruntime版本即可启用，无需额外配置。",
            "entities": "",
            "type": "系统手册",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "三、依赖管理 > 3.2 GPU支持策略"
        },
        {
            "title": "定时任务故障排查",
            "keywords": "定时任务,提醒,失效,循环任务,通知",
            "summary": "定时任务存储在data/scheduled_tasks.db，后台线程每分钟检查一次。如果提醒未收到，检查任务状态和系统通知设置。循环任务需要正确的cron表达式。",
            "entities": "",
            "type": "系统手册",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "五、故障排查 > 5.3 定时任务问题"
        },
        {
            "title": "数据存储与备份",
            "keywords": "数据,备份,存储,数据库,恢复",
            "summary": "所有用户数据存储在data/目录，包括历史对话、知识库、知识图谱、定时任务。备份只需复制data/和~/.niu/目录。数据库文件可定期清理和压缩。",
            "entities": "",
            "type": "系统手册",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "五、故障排查 > 5.5 数据问题"
        },
        {
            "title": "内存优化策略",
            "keywords": "内存,占用,优化,模型卸载",
            "summary": "系统内存占用约1.5GB，人脸识别模型空闲5分钟后自动卸载释放326MB。如内存不足可禁用GPU加速或减少MCP服务器数量。推荐内存8GB以上。",
            "entities": "",
            "type": "系统手册",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "六、性能优化 > 6.1 内存优化"
        },
        {
            "title": "LLM配置指南",
            "keywords": "LLM,配置,API,Ollama,云端",
            "summary": "支持本地Ollama和云端API（OpenAI/Anthropic等）。配置文件在config/user-config.json，填写provider、model、api_key即可。首次启动会引导配置。",
            "entities": "",
            "type": "系统手册",
            "pointer": f"file:{manual_path.relative_to(Path(__file__).parent.parent)}",
            "section": "七、用户指南 > 7.2 LLM配置"
        },
        {
            "title": "系统架构说明",
            "keywords": "架构,单进程,模块,目录结构",
            "summary": "采用单进程架构，embedding和scheduler集成到主进程。所有模块运行在端口9876。目录包括主程序、模型、配置、用户数据。",
            "entities": "",
            "type": "系统手册",
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

        # 元数据
        metadata = {
            "level": "l1",
            "category": "document",
            "resource_type": "system_manual",
            "section": l1["section"],
            "title": l1["title"],
        }

        # 获取向量
        embedding = vs._get_embedding(content)
        if embedding is None:
            logger.error(f"向量生成失败: {l1['title']}")
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
