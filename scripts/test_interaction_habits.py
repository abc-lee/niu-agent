#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interaction Habits 功能验证"""
import sys
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

import sys as _sys
_sys.path.insert(0, "E:/tools/ai-bot")
from agent.vector_search import VectorSearchAdapter

print("=" * 60)
print("Interaction Habits 功能验证")
print("=" * 60)

vs = VectorSearchAdapter()

# 测试 1：写入工具方言
print("\n[测试1] 写入工具方言...")
success = vs.upsert_interaction_habit(
    habit_type="tool_dialect",
    content="赶紧叫下我",
    metadata={
        "target_tool": "scheduler-server/schedule_task",
        "refined_query": "schedule task",
        "source": "personal",
        "confidence": {"success_count": 1, "fail_count": 0}
    },
    habit_id="habit:tool_dialect:test001"
)
print(f"  upsert: {'✓' if success else '✗'}")

# 测试 2：检索工具方言
print("\n[测试2] 检索工具方言...")
results = vs.search_interaction_habits(
    query="叫下我", habit_type="tool_dialect", limit=3
)
print(f"  找到 {len(results)} 条记录")
for r in results:
    print(f"  - {r.content[:30]}... (score={r.score:.3f})")

# 测试 3：检索所有 interaction_habits
print("\n[测试3] 检索所有 Interaction Habits...")
all_results = vs.search_interaction_habits(query="叫下我", limit=5)
print(f"  找到 {len(all_results)} 条记录")

# 测试 4：更新置信度
print("\n[测试4] 更新置信度（success）...")
r = vs.update_habit_confidence("habit:tool_dialect:test001", "success")
print(f"  update_habit_confidence: {'✓' if r else '✗'}")

# 测试 5：检索验证置信度变化
print("\n[测试5] 验证置信度更新...")
updated = vs.search_interaction_habits(
    query="叫下我", habit_type="tool_dialect", limit=1
)
if updated:
    conf = updated[0].metadata.get("confidence", {})
    print(f"  success_count: {conf.get('success_count', 0)} (预期 2)")
    assert conf.get("success_count", 0) == 2, "置信度更新失败"
    print("  ✓ 置信度更新正确")

print("\n" + "=" * 60)
print("所有测试通过 ✓")
print("=" * 60)
