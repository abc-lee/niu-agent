#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manual verification: test real user queries against the recursive search"""
import sys
from pathlib import Path

# Add scripts/ to sys.path so query_pattern is importable as a top-level module
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_SCRIPTS_ROOT = _PROJECT_ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

# UTF-8 wrapper for Windows stdout — force UTF-8 regardless of current encoding
if sys.platform == "win32":
    import io
    # Re-wrap with UTF-8 encoding to handle Chinese characters
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from query_pattern.tools import recursive_search

TEST_QUERIES = [
    # schedule_task
    ("5分钟后提醒我吃药", "scheduler-server/schedule_task"),
    ("半小时后提醒我开会", "scheduler-server/schedule_task"),
    ("remind me in 10 minutes", "scheduler-server/schedule_task"),
    ("明天上午10点提醒我开会", "scheduler-server/schedule_task"),
    # cancel_task
    ("取消刚才的提醒", "scheduler-server/cancel_task"),
    ("删掉下午的定时任务", "scheduler-server/cancel_task"),
    # list_scheduled_tasks
    ("看看我有哪些定时任务", "scheduler-server/list_scheduled_tasks"),
    ("显示所有提醒", "scheduler-server/list_scheduled_tasks"),
    # update_task
    ("把提醒改成下午3点", "scheduler-server/update_task"),
]

print("=" * 70)
print("Manual Recursive Search Verification")
print("=" * 70)

passed = 0
for query, expected_tool in TEST_QUERIES:
    results, score = recursive_search(query)
    if results:
        matched_id = results[0].id
        matched_name = results[0].metadata.get("name", "")
        matched_server = results[0].metadata.get("server", "")
        matched_tool = f"{matched_server}/{matched_name}" if matched_name else matched_id
        tool_name = expected_tool.split("/")[-1]
        is_match = tool_name in (matched_id or "") or tool_name in (matched_name or "")
        status = "PASS" if is_match else "FAIL"
    else:
        matched_tool = "None"
        is_match = False
        status = "FAIL"

    hit = "PASS" if (is_match and score >= 0.5) else "FAIL"
    print(f"[{status}] '{query}' -> matched={matched_tool} score={score:.4f} [{hit}]")
    if is_match and score >= 0.5:
        passed += 1

print(f"\nResult: {passed}/{len(TEST_QUERIES)} passed")
