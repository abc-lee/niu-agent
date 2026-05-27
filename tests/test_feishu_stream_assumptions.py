"""
TDD测试：验证飞书流式推送的核心数据假设

核心假设：
  push_incremental() 依赖 "前端读 DB 时，DB 中已有新的 assistant 消息"
  即：V4 逐轮 persist 在 Agent 处理期间，是否真的把 assistant 消息写入了 DB？

验证方法：
  1. 直接查 DB，看最近一次飞书交互的 rowid 分布和 assistant 消息出现时间
  2. 对比 _persist_one_msg 的写入时机 vs 前端调 get_context_messages 的时机

这个脚本不修改任何代码，只读取 DB 数据做分析。
"""

import sqlite3
import json
from pathlib import Path

DB_PATH = Path.home() / ".niu" / "messages.db"


def analyze_db():
    """直接读 DB，分析消息分布"""
    if not DB_PATH.exists():
        print(f"[ERROR] DB 不存在: {DB_PATH}")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # 1. 总览
    cursor = conn.execute("SELECT COUNT(*) FROM messages")
    total = cursor.fetchone()[0]
    cursor = conn.execute("SELECT MAX(rowid) FROM messages")
    max_rowid = cursor.fetchone()[0] or 0

    print(f"=== DB 总览 ===")
    print(f"总消息数: {total}")
    print(f"最大 rowid: {max_rowid}")
    print()

    # 2. 按角色统计
    print(f"=== 按角色统计 ===")
    cursor = conn.execute("SELECT role, COUNT(*), SUM(length(content)) FROM messages GROUP BY role")
    for row in cursor.fetchall():
        print(f"  {row[0]}: {row[1]} 条, 总内容长度={row[2]}")
    print()

    # 3. 逐条列出所有消息（按 rowid 升序）
    print(f"=== 所有消息详情（按 rowid 升序）===")
    cursor = conn.execute(
        "SELECT rowid, id, role, length(content) as content_len, "
        "substr(content, 1, 100) as preview, created_at, "
        "CASE WHEN tool_calls != '[]' THEN 1 ELSE 0 END as has_tool_calls, "
        "tool_call_id "
        "FROM messages ORDER BY rowid ASC"
    )
    rows = cursor.fetchall()

    assistant_text_rows = []  # 记录所有 assistant 文本消息的 rowid

    for row in rows:
        r = dict(row)
        marker = ""
        if r["role"] == "assistant" and r["content_len"] > 0:
            marker = " <<< ASSISTANT_TEXT"
            assistant_text_rows.append(r["rowid"])
        if r["role"] == "assistant" and r["content_len"] == 0 and r["has_tool_calls"]:
            marker = " [TOOL_CALLS_ONLY]"
        if r["role"] == "tool":
            marker = " [TOOL_RESULT]"

        print(f"  rowid={r['rowid']:3d}  role={r['role']:9s}  len={r['content_len']:5d}{marker}")
        print(f"           preview: {r['preview']}")
        print(f"           time: {r['created_at']}")
        if r["tool_call_id"]:
            print(f"           tool_call_id: {r['tool_call_id'][:30]}")
        print()

    # 4. 关键分析
    print("=" * 60)
    print("=== 关键分析 ===")
    print()
    print(f"assistant 文本消息共 {len(assistant_text_rows)} 条，rowid 分别为: {assistant_text_rows}")
    print()

    if len(assistant_text_rows) <= 1:
        print("[结论] assistant 文本消息只有 1 条（或 0 条）")
        print("       如果 Agent 处理完成后才写入最终消息，")
        print("       那 push_incremental 在处理期间读不到增量 assistant 消息！")
        print()
        print("       这意味着：当前的流式推送方案根本行不通，")
        print("       因为触发时（前端读 DB）DB 里还没有新的 assistant 内容。")
    else:
        print(f"[结论] assistant 文本消息有 {len(assistant_text_rows)} 条")
        print("       多轮工具调用中，每轮都产生了 assistant 消息写入 DB")
        print()

        # 分析间隔
        if len(assistant_text_rows) >= 2:
            print("  rowid 间隔分析:")
            for i in range(1, len(assistant_text_rows)):
                gap = assistant_text_rows[i] - assistant_text_rows[i-1]
                print(f"    {assistant_text_rows[i-1]} → {assistant_text_rows[i]}: gap={gap} (中间有 {gap-1} 条 tool/其他消息)")

    # 5. 验证 _persist_one_msg 写入时序
    print()
    print("=== 时序分析 ===")
    print()
    cursor = conn.execute(
        "SELECT rowid, role, created_at, length(content) "
        "FROM messages ORDER BY rowid ASC"
    )
    rows = cursor.fetchall()
    if len(rows) >= 2:
        for i in range(1, len(rows)):
            prev_time = rows[i-1]["created_at"]
            curr_time = rows[i]["created_at"]
            if prev_time and curr_time and prev_time[:19] != curr_time[:19]:
                print(f"  rowid {rows[i-1]['rowid']}→{rows[i]['rowid']}: "
                      f"{prev_time} → {curr_time}  (时间变化)")

    conn.close()


if __name__ == "__main__":
    analyze_db()