#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单独优化scheduler工具（避免批量导致崩溃）
"""

import sys
import sqlite3
import json
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.vector_search import VectorSearchAdapter
import numpy as np

# Scheduler工具L1描述
SCHEDULER_TOOLS = {
    "scheduler-server:schedule_task": "设置提醒、闹钟、定时任务。用户说'提醒我'、'定闹钟'、'几分钟后提醒'、'每天几点提醒'时使用",
    "scheduler-server:list_scheduled_tasks": "查询定时任务列表、查看提醒。用户说'我有哪些提醒'、'查看定时任务'、'已设置的闹钟'时使用",
    "scheduler-server:cancel_task": "取消定时任务、删除提醒。用户说'取消提醒'、'删除定时任务'、'关闭闹钟'时使用",
    "scheduler-server:update_task": "修改定时任务、调整提醒时间。用户说'修改提醒时间'、'改成几点'、'调整定时任务'时使用",
}

print("Optimizing scheduler tools (one by one)...\n")

adapter = VectorSearchAdapter()
conn = sqlite3.connect(adapter.db_path)
cursor = conn.cursor()

for i, (tool_key, l1_desc) in enumerate(SCHEDULER_TOOLS.items(), 1):
    doc_id = f"mcp_tool:{tool_key}"

    print(f"[{i}/4] {tool_key}")
    print(f"  Getting embedding...")

    try:
        embedding = adapter._get_embedding(l1_desc)
        if embedding:
            embedding_blob = np.array(embedding, dtype=np.float32).tobytes()

            cursor.execute("""
                UPDATE documents
                SET content = ?, embedding = ?
                WHERE id = ?
            """, (l1_desc, embedding_blob, doc_id))

            conn.commit()
            print(f"  [OK] Updated\n")
        else:
            print(f"  [FAIL] No embedding\n")

    except Exception as e:
        print(f"  [ERROR] {e}\n")

    # 每个工具之间暂停5秒
    if i < 4:
        print("  Waiting 5s...\n")
        time.sleep(5)

conn.close()

print("=" * 60)
print("Done! Now test with: python test_semantic.py")
print("=" * 60)
