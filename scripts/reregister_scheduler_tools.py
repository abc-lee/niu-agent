"""
重新注册 scheduler-server MCP 工具（优化语义匹配）

Usage:
    python scripts/reregister_scheduler_tools.py
"""

import sys
import time
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def get_enhanced_tools_description():
    """获取优化后的工具描述（L1+L2双层结构）"""
    return [
        {
            "name": "schedule_task",
            # L1: 短描述，用于向量匹配（关键词+核心语义）
            "l1_description": "设置提醒、闹钟、定时任务。用户说'提醒我'、'定闹钟'、'几分钟后提醒'、'每天几点提醒'时使用",
            # L2: 完整描述，供LLM查看
            "l2_description": """创建定时任务、设置提醒、闹钟。支持单次和循环提醒。

**用户场景**（当用户说以下话时使用此工具）：
- "提醒我..."、"叫我..."、"通知我..."
- "定个闹钟"、"设置提醒"、"设个定时"
- "几分钟后提醒我"、"X分钟后叫我"
- "今晚几点..."、"明天几点..."、"每晚几点..."
- "每天...提醒我"、"每周...叫我"、"工作日..."
- "设置循环提醒"、"定期提醒"

**功能**：
- 单次提醒：指定具体时间提醒一次
- 循环提醒：每天、每周、工作日等定期提醒
- 定时闹钟：任意时间点的提醒

**参数**：
- content: 提醒内容（如"吃药"、"开会"、"起床"）
- scheduled_at: 首次触发时间（ISO格式）
- is_recurring: 是否循环（每天/每周等）
- cron_expr: 循环表达式（每天="0 8 * * *"，每周一="0 9 * * 1"，工作日="0 9 * * 1-5"）

**示例**：
- 用户说："5分钟后提醒我吃早餐" → 单次提醒
- 用户说："每晚10点提醒我打开洗碗机" → 循环提醒，cron="0 22 * * *"
- 用户说："每天早上8点叫我起床" → 循环提醒，cron="0 8 * * *"
- 用户说："工作日上午9点提醒我打卡" → 循环提醒，cron="0 9 * * 1-5"

**注意**：相对时间（5分钟后、明天）必须先计算出具体时间再调用此工具。""",
            "server": "scheduler-server"
        },
        {
            "name": "list_scheduled_tasks",
            "l1_description": "查询定时任务列表、查看提醒。用户说'我有哪些提醒'、'查看定时任务'、'已设置的闹钟'时使用",
            "l2_description": """查询定时任务列表、查看已设置的提醒。

**用户场景**：
- "我有哪些提醒？"
- "查看我的定时任务"
- "已设置的闹钟列表"
- "我设置了什么提醒？"

**参数**：
- status: 筛选状态（pending=待触发/triggered=已触发/cancelled=已取消）

**返回**：任务列表，包含ID、内容、时间、是否循环等信息。""",
            "server": "scheduler-server"
        },
        {
            "name": "cancel_task",
            "l1_description": "取消定时任务、删除提醒。用户说'取消提醒'、'删除定时任务'、'关闭闹钟'时使用",
            "l2_description": """取消定时任务、删除提醒、关闭闹钟。

**用户场景**：
- "取消提醒..."
- "删除定时任务..."
- "关闭闹钟..."
- "不要提醒我了..."

**参数**：
- task_id: 要取消的任务ID（从list_scheduled_tasks获取）

**返回**：取消结果。""",
            "server": "scheduler-server"
        },
        {
            "name": "update_task",
            "l1_description": "修改定时任务、调整提醒时间。用户说'修改提醒时间'、'改成几点'、'调整定时任务'时使用",
            "l2_description": """修改定时任务、调整提醒时间。

**用户场景**：
- "修改提醒时间..."
- "改成几点..."
- "调整定时任务..."

**参数**：
- task_id: 任务ID
- content: 新的提醒内容（可选）
- scheduled_at: 新的触发时间（可选）
- cron_expr: 新的循环表达式（可选）

**返回**：更新结果。""",
            "server": "scheduler-server"
        }
    ]


def reregister_tools():
    """重新注册工具到向量库（L1+L2双层结构）"""
    output = []
    output.append("=" * 60)
    output.append("Reregister scheduler-server MCP tools (L1 summary)")
    output.append("=" * 60)
    output.append("\nStrategy:")
    output.append("  - L1 (short desc) -> vector DB for matching")
    output.append("  - L2 (full desc)  -> metadata for LLM")
    output.append("=" * 60)

    # 使用 VectorSearchAdapter 的路径逻辑
    from agent.vector_search import VectorSearchAdapter
    db_path = VectorSearchAdapter._default_db_path()
    output.append(f"\nVector DB: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    tools = get_enhanced_tools_description()

    output.append(f"\nUpdating {len(tools)} tools:")
    for tool in tools:
        output.append(f"  - {tool['name']}")

    output.append("\nStart updating...")

    # 获取向量搜索适配器
    adapter = VectorSearchAdapter()

    for i, tool in enumerate(tools, 1):
        doc_id = f"mcp_tool:scheduler-server:{tool['name']}"

        # L1用于向量匹配
        l1_content = tool["l1_description"]

        # 获取新向量
        output.append(f"[{i}/{len(tools)}] {tool['name']}")
        output.append(f"    Getting embedding for L1 ({len(l1_content)} chars)...")

        embedding = adapter._get_embedding(l1_content)
        if embedding is None:
            output.append(f"    [FAIL] Failed to get embedding")
            continue

        import numpy as np
        embedding_blob = np.array(embedding, dtype=np.float32).tobytes()

        # L2存在metadata中
        import json
        metadata = {
            "type": "mcp_tool",
            "name": tool["name"],
            "description": tool["l2_description"],  # 完整描述
            "server": tool["server"]
        }

        # 更新文档：content + embedding
        cursor.execute("""
            UPDATE documents
            SET content = ?, embedding = ?, metadata = ?
            WHERE id = ?
        """, (l1_content, embedding_blob, json.dumps(metadata, ensure_ascii=False), doc_id))

        if cursor.rowcount > 0:
            l1_len = len(tool["l1_description"])
            l2_len = len(tool["l2_description"])
            output.append(f"    L1: {l1_len} chars -> L2: {l2_len} chars (saved {l2_len-l1_len} chars)")
            output.append(f"    [OK] Updated with new embedding")
        else:
            output.append(f"    [FAIL] Not found")

    conn.commit()
    conn.close()

    output.append("\n" + "=" * 60)
    output.append("Update completed!")
    output.append("=" * 60)

    # 输出到文件
    with open('reregister_tools_result.txt', 'w', encoding='utf-8') as f:
        f.write('\n'.join(output))

    print("Result saved to reregister_tools_result.txt")


if __name__ == "__main__":
    reregister_tools()
