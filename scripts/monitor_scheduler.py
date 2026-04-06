#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监控定时任务触发"""

import sqlite3
import time
from datetime import datetime
from pathlib import Path

db_path = "REDACTED_WIN_PATH/scheduled_tasks.db"

print("Monitoring scheduled tasks...")
print("=" * 60)

last_check = {}

for i in range(20):  # 监控20分钟
    now = datetime.now()
    print(f"\n[{now.strftime('%H:%M:%S')}] Check #{i+1}")

    if Path(db_path).exists():
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 查询所有pending任务
        cursor.execute("""
            SELECT id, content, scheduled_at, status
            FROM scheduled_tasks
            WHERE status='pending'
            ORDER BY scheduled_at
        """)

        tasks = cursor.fetchall()

        if tasks:
            print(f"  Pending tasks: {len(tasks)}")
            for task_id, content, scheduled_at, status in tasks:
                scheduled_time = datetime.fromisoformat(scheduled_at.replace('+08:00', '').replace('+08:00', ''))
                is_overdue = scheduled_time < now

                status_mark = "[OVERDUE]" if is_overdue else "[PENDING]"

                # 检查状态变化
                if task_id in last_check:
                    if last_check[task_id] != status:
                        print(f"    {status_mark} {content} - STATUS CHANGED: {last_check[task_id]} -> {status}")
                else:
                    print(f"    {status_mark} {content} at {scheduled_at}")

                last_check[task_id] = status
        else:
            print("  No pending tasks")

        # 检查最近触发的任务
        cursor.execute("""
            SELECT id, content, triggered_at
            FROM scheduled_tasks
            WHERE status='triggered'
            ORDER BY triggered_at DESC
            LIMIT 3
        """)

        triggered = cursor.fetchall()
        if triggered:
            print(f"  Recently triggered:")
            for task_id, content, triggered_at in triggered:
                print(f"    - {content} at {triggered_at}")

        conn.close()
    else:
        print("  Database not found")

    if i < 19:  # 不是最后一次
        time.sleep(60)  # 等待1分钟

print("\n" + "=" * 60)
print("Monitoring complete")
