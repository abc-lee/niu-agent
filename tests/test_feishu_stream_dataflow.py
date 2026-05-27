"""
TDD测试：验证飞书流式推送的核心数据流假设

假设1: 前端调用 GET /api/context/messages 时，DB 中是否有新的 assistant 消息
假设2: _feishu_waiting 标志在飞书消息入队后是否为 True
假设3: push_incremental() 能否读到增量 rowid 消息
假设4: Electron 端请求时 _feishu_waiting 是否为 False（不误触发）

测试方法：
1. 启动 API 服务
2. 通过飞书发消息触发 _on_message（需手动发）
3. 监控 DB 中 rowid 变化和 assistant 消息出现时间
4. 调用 GET /api/context/messages 模拟前端刷新
5. 验证 push_incremental 读到的增量数据
"""

import json
import time
import asyncio
import aiosqlite
from datetime import datetime
from pathlib import Path


DB_PATH = Path.home() / ".niu" / "messages.db"


async def watch_db_changes(duration_seconds=120, poll_interval=2):
    """
    持续监控 DB 变化，记录每条新消息的 rowid、role、content 摘要、出现时间。

    这是最关键的测试 — 看看在飞书消息入队后、Agent 处理期间，
    DB 中 assistant 消息到底什么时候出现、出现几次、内容是什么。
    """
    print(f"[Test] 开始监控 DB 变化，持续 {duration_seconds} 秒...")
    print(f"[Test] DB 路径: {DB_PATH}")

    if not DB_PATH.exists():
        print("[Test] DB 不存在！")
        return

    # 获取初始 max rowid
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT MAX(rowid) FROM messages")
        row = await cursor.fetchone()
        initial_rowid = row[0] if row and row[0] else 0

    print(f"[Test] 初始 max_rowid = {initial_rowid}")
    print(f"[Test] === 请在飞书上发一条消息 ===")
    print()

    seen_rowids = set()
    start_time = time.time()

    # 先记录当前所有消息的 rowid
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT rowid, id, role, content, created_at FROM messages ORDER BY rowid ASC")
        rows = await cursor.fetchall()
        for row in rows:
            seen_rowids.add(row["rowid"])

    print(f"[Test] 已有 {len(seen_rowids)} 条消息")
    print()

    events = []  # 记录所有新消息事件

    while time.time() - start_time < duration_seconds:
        await asyncio.sleep(poll_interval)

        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT rowid, id, role, content, created_at FROM messages WHERE rowid > ? ORDER BY rowid ASC",
                (initial_rowid,)
            )
            new_rows = await cursor.fetchall()

        for row in new_rows:
            if row["rowid"] not in seen_rowids:
                seen_rowids.add(row["rowid"])
                elapsed = time.time() - start_time
                content_preview = (row["content"] or "")[:80]
                event = {
                    "rowid": row["rowid"],
                    "id": row["id"],
                    "role": row["role"],
                    "content_preview": content_preview,
                    "content_len": len(row["content"] or ""),
                    "elapsed_sec": round(elapsed, 1),
                    "created_at": row["created_at"],
                }
                events.append(event)

                # 标记 assistant 消息
                marker = "<<< ASSISTANT" if row["role"] == "assistant" else ""
                has_tool_calls = ""
                if row["role"] == "assistant" and not (row["content"] or "").strip():
                    has_tool_calls = " [TOOL_CALLS_ONLY]"

                print(f"[{elapsed:.1f}s] rowid={row['rowid']} role={row['role']}{has_tool_calls} len={len(row['content'] or '')} {marker}")
                print(f"         content: {content_preview}")
                print()

    # 汇总
    print("=" * 60)
    print("[Summary] 事件时间线:")
    for e in events:
        marker = "ASSISTANT" if e["role"] == "assistant" else e["role"]
        print(f"  {e['elapsed_sec']}s  rowid={e['rowid']}  {marker}  len={e['content_len']}  '{e['content_preview']}'")

    # 关键问题：assistant 文本消息是否分段出现？
    assistant_text_events = [e for e in events if e["role"] == "assistant" and e["content_len"] > 0]
    print()
    print(f"[Key Finding] assistant 文本消息出现 {len(assistant_text_events)} 次:")
    for e in assistant_text_events:
        print(f"  {e['elapsed_sec']}s  rowid={e['rowid']}  len={e['content_len']}  '{e['content_preview']}'")

    if len(assistant_text_events) <= 1:
        print()
        print("[结论] assistant 消息只有1条（或0条），没有分段！")
        print("       这意味着 push_incremental 在前端刷新时读不到增量 assistant 消息")
        print("       因为 Agent 可能只在完成时才写一条完整的 assistant 消息到 DB")
    else:
        print()
        print(f"[结论] assistant 消息分段出现了 {len(assistant_text_events)} 次！")
        print("       push_incremental 理论上可以读到增量数据")

    return events


async def test_api_context_messages():
    """
    测试 GET /api/context/messages 的调用频率和时机。

    通过 SSE 事件触发，前端何时调用了这个 API。
    我们直接调 API 看 DB 状态。
    """
    import httpx

    print("[Test] 调用 GET /api/context/messages...")

    async with httpx.AsyncClient() as client:
        resp = await client.get("http://localhost:9876/api/context/messages?limit=100")
        if resp.status_code == 200:
            data = resp.json()
            msgs = data.get("messages", [])
            print(f"[Test] 返回 {len(msgs)} 条消息")
            for m in msgs[-5:]:
                content_preview = (m.get("content") or "")[:60]
                print(f"  id={m.get('id')} role={m.get('role')} len={len(m.get('content') or '')} '{content_preview}'")
        else:
            print(f"[Test] API 错误: {resp.status_code} {resp.text}")


if __name__ == "__main__":
    print("=" * 60)
    print("TDD测试：飞书流式推送数据流验证")
    print("=" * 60)
    print()
    print("核心假设验证:")
    print("1. Agent 处理过程中，DB 里是否有分段 assistant 消息？")
    print("2. 如果只有1条完整消息，push_incremental 拿不到增量数据")
    print("3. V4 逐轮 persist 是否真的在 Agent 处理期间写入 DB？")
    print()

    # 先检查当前 DB 状态
    print("[Step 0] 当前 DB 状态:")
    asyncio.run(test_api_context_messages())
    print()

    # 然后监控 DB 变化 — 需要用户在飞书上发消息
    print("[Step 1] 监控 DB 变化（请在飞书上发一条消息）:")
    events = asyncio.run(watch_db_changes(duration_seconds=120, poll_interval=1))