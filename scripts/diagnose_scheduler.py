#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查定时任务系统状态"""

import sqlite3
import os
from datetime import datetime

output = []
output.append("=" * 60)
output.append("Scheduler System Diagnostic")
output.append("=" * 60)

# 1. 检查数据库
db_path = "REDACTED_WIN_PATH/scheduled_tasks.db"
if os.path.exists(db_path):
    output.append(f"\n[OK] Database exists: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 统计任务
    cursor.execute("SELECT COUNT(*) FROM scheduled_tasks")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE status='pending'")
    pending = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE status='triggered'")
    triggered = cursor.fetchone()[0]

    output.append(f"  - Total tasks: {total}")
    output.append(f"  - Pending: {pending}")
    output.append(f"  - Triggered: {triggered}")

    # 显示pending任务
    if pending > 0:
        output.append(f"\nPending tasks:")
        cursor.execute("""
            SELECT id, content, scheduled_at, is_recurring
            FROM scheduled_tasks
            WHERE status='pending'
            ORDER BY scheduled_at
        """)

        now = datetime.now()
        for row in cursor.fetchall():
            task_id, content, scheduled_at, is_recurring = row
            # 移除时区信息以进行比较
            scheduled_time_str = scheduled_at.replace('+08:00', '')
            scheduled_time = datetime.fromisoformat(scheduled_time_str)
            is_overdue = scheduled_time < now

            status_mark = "[OVERDUE]" if is_overdue else "[OK]"
            recurring_mark = "[RECURRING]" if is_recurring else "[ONCE]"

            output.append(f"  {status_mark} {recurring_mark} {content}")
            output.append(f"       Time: {scheduled_at}")
            output.append(f"       ID: {task_id[:8]}...")

    conn.close()
else:
    output.append(f"\n[ERROR] Database not found: {db_path}")

# 2. 检查待推送提醒
try:
    from niu_api.alerts import get_and_clear_pending_alerts

    # 注意：这会清空队列
    alerts = get_and_clear_pending_alerts()
    if alerts:
        output.append(f"\n[OK] Pending alerts: {len(alerts)}")
        for alert in alerts:
            output.append(f"  - {alert['content'][:50]}...")
    else:
        output.append(f"\n[INFO] No pending alerts")

except Exception as e:
    output.append(f"\n[ERROR] Cannot access alerts: {e}")

# 输出到文件
with open('scheduler_diagnostic.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(output))

print("Diagnostic result saved to scheduler_diagnostic.txt")
