"""
记忆提取器 - 从对话中自动提取重要信息
"""

import json
import os
import sqlite3
from typing import Optional
from loguru import logger

# 记忆提取提示词
EXTRACTION_PROMPT = """你是一个记忆提取助手。请从以下对话中提取值得长期记忆的信息。

**提取规则**：
1. 只提取用户明确表达的偏好、身份信息、重要事件
2. 不提取普通对话内容、临时问题、已知信息
3. 每条记忆应该是独立的、可检索的陈述句

**记忆类型**：
- preference: 用户偏好（如"用户喜欢简洁回答"）
- event: 重要事件（如"2026-03-28 项目X上线"）
- context: 知识上下文（如"合同.pdf是张三的购房合同"）
- pattern: 行为模式（如"用户每周一会整理文档"）

**对话内容**：
{messages}

请以 JSON 数组格式返回提取的记忆：
[
  {{"content": "记忆内容", "type": "记忆类型", "metadata": {{}}}}
]

如果没有值得记忆的内容，返回空数组 []。
"""


def get_db_path() -> str:
    """获取数据库路径

    优先使用 PROJECT_ROOT（程序目录，包含 niu.db），
    因为 messages 表在主程序的数据库中。
    """
    # 主程序设置了 PROJECT_ROOT 环境变量
    project_root = os.environ.get("PROJECT_ROOT")
    if project_root:
        return os.path.join(project_root, "niu.db")

    # 回退到 WORKSPACE_PATH（旧逻辑）
    workspace = os.environ.get("WORKSPACE_PATH", ".")
    return os.path.join(workspace, "niu.db")


def get_messages(session_id: str, limit: int = 10) -> list[dict]:
    """从数据库获取消息"""
    db_path = get_db_path()

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT role, content, created_at 
            FROM messages 
            WHERE session_id = ? 
            ORDER BY created_at DESC 
            LIMIT ?
        """,
            (session_id, limit),
        )

        messages = []
        for row in cursor.fetchall():
            messages.append({"role": row[0], "content": row[1], "created_at": row[2]})

        conn.close()

        # 反转顺序（从旧到新）
        messages.reverse()
        return messages

    except Exception as e:
        logger.error(f"获取消息失败: {e}")
        return []


def format_messages(messages: list[dict]) -> str:
    """格式化消息为可读文本"""
    lines = []
    for msg in messages:
        role = "用户" if msg["role"] == "user" else "助手"
        content = msg["content"]
        # 截断过长的消息
        if len(content) > 500:
            content = content[:500] + "..."
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def extract_memories_from_messages(
    session_id: str, limit: int = 10
) -> list[dict]:
    """从对话中提取记忆

    使用轻量级规则提取，不调用 LLM，节省资源。
    """
    # 1. 获取消息
    messages = get_messages(session_id, limit)
    if not messages:
        return []

    # 2. 使用规则提取（轻量级，不调用 LLM）
    memories = []

    for msg in messages:
        if msg["role"] != "user":
            continue

        content = msg["content"]

        # 规则 1: 用户偏好
        preference_keywords = ["喜欢", "不喜欢", "偏好", "习惯", "风格", "简洁", "详细"]
        if any(kw in content for kw in preference_keywords):
            memories.append(
                {
                    "content": f"用户表达偏好: {content}",
                    "type": "preference",
                    "metadata": {"source_message": content[:100]},
                }
            )

        # 规则 2: 重要事件
        event_keywords = ["会议", "任务", "提醒", "项目", "上线", "发布", "截止"]
        time_keywords = ["明天", "下周", "下个月", "今天", "下午", "上午"]
        if any(kw in content for kw in event_keywords) and any(
            kw in content for kw in time_keywords
        ):
            memories.append(
                {
                    "content": f"重要事件: {content}",
                    "type": "event",
                    "metadata": {"source_message": content[:100]},
                }
            )

        # 规则 3: 文件上下文
        file_keywords = ["文件", "文档", "报告", "合同", "PDF"]
        if any(kw in content for kw in file_keywords):
            memories.append(
                {
                    "content": f"文件上下文: {content}",
                    "type": "context",
                    "metadata": {"source_message": content[:100]},
                }
            )

        # 规则 4: 行为模式
        pattern_keywords = ["每天", "每周", "每月", "总是", "经常", "定期"]
        if any(kw in content for kw in pattern_keywords):
            memories.append(
                {
                    "content": f"行为模式: {content}",
                    "type": "pattern",
                    "metadata": {"source_message": content[:100]},
                }
            )

    # 3. 去重（简单的基于内容相似度）
    unique_memories = []
    seen_contents = set()
    for mem in memories:
        # 简单去重：内容前 50 字符相同则认为重复
        content_key = mem["content"][:50]
        if content_key not in seen_contents:
            seen_contents.add(content_key)
            unique_memories.append(mem)

    logger.info(f"提取完成: 共 {len(unique_memories)} 条记忆")
    return unique_memories
