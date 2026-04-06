"""
注册 scheduler-server MCP 工具到向量库

Usage:
    python scripts/register_scheduler_tools.py
"""

import sys
import os
import time
import json
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.vector_search import VectorSearchAdapter


def get_scheduler_tools_description():
    """获取 scheduler-server 工具描述"""
    return [
        {
            "name": "schedule_task",
            "description": "创建定时任务，支持单次和循环任务。参数：content(任务内容)、scheduled_at(触发时间ISO格式)、event_type(事件类型)、is_recurring(是否循环)、cron_expr(cron表达式)。支持每天、每周、工作日等循环提醒。",
            "server": "scheduler-server"
        },
        {
            "name": "list_scheduled_tasks",
            "description": "查询定时任务列表。参数：status(可选，筛选状态pending/triggered/cancelled)。返回任务列表，包含id、content、scheduled_at、is_recurring、cron_expr、status。",
            "server": "scheduler-server"
        },
        {
            "name": "cancel_task",
            "description": "取消定时任务。参数：task_id(任务ID)。返回取消结果。",
            "server": "scheduler-server"
        },
        {
            "name": "update_task",
            "description": "更新定时任务。参数：task_id(任务ID)、content(新任务内容)、scheduled_at(新触发时间)、cron_expr(新cron表达式)。返回更新结果。",
            "server": "scheduler-server"
        }
    ]


def register_tools():
    """注册工具到向量库"""
    print("=" * 60)
    print("注册 scheduler-server MCP 工具到向量库")
    print("=" * 60)

    # 初始化向量搜索适配器
    vector_search = VectorSearchAdapter()

    # 获取工具描述
    tools = get_scheduler_tools_description()

    print(f"\n准备注册 {len(tools)} 个工具：")
    for tool in tools:
        print(f"  - {tool['name']}")

    print("\n开始注册...")

    registered = 0
    for i, tool in enumerate(tools, 1):
        try:
            # 构建文档ID和内容
            doc_id = f"mcp_tool:scheduler-server:{tool['name']}"
            content = f"{tool['name']}: {tool['description']}"

            # 元数据
            metadata = {
                "type": "mcp_tool",
                "name": tool["name"],
                "description": tool["description"],
                "server": tool["server"]
            }

            # 检查是否已存在
            existing = vector_search.get_document(doc_id)
            if existing:
                print(f"[{i}/{len(tools)}] {tool['name']} - 已存在，跳过")
                continue

            # 添加到向量库
            vector_search.add_document(
                id=doc_id,
                content=content,
                metadata=metadata
            )

            print(f"[{i}/{len(tools)}] {tool['name']} - [OK]")
            registered += 1

            # 延迟，避免过载
            time.sleep(1)

        except Exception as e:
            print(f"[{i}/{len(tools)}] {tool['name']} - [FAIL] {e}")

    print("\n" + "=" * 60)
    print(f"注册完成：成功 {registered}/{len(tools)}")
    print("=" * 60)

    # 验证
    print("\n验证注册结果...")
    for tool in tools:
        doc_id = f"mcp_tool:scheduler-server:{tool['name']}"
        try:
            doc = vector_search.get_document(doc_id)
            if doc:
                print(f"  ✓ {tool['name']}")
            else:
                print(f"  ✗ {tool['name']} - 未找到")
        except Exception as e:
            print(f"  ✗ {tool['name']} - 错误: {e}")


if __name__ == "__main__":
    register_tools()
