"""
TDD测试：验证 Agent 多轮处理期间 DB 中是否有增量 assistant 消息

核心假设：push_incremental 能读到增量数据的前提是
  前端读 DB 时，DB 里已经有新的 assistant 消息。

测试方法：
  1. 清空 DB
  2. 通过 Electron API 发一条需要多轮工具调用的消息
  3. 在 Agent 处理期间，每隔1秒查询 DB 的 max_rowid 和 assistant 消息数
  4. 记录每条新消息出现的时机
  5. 如果在 Agent 处理期间 DB 中出现了多条 assistant 消息，
     说明 push_incremental 的设计思路是可行的
"""

import asyncio
import sqlite3
import time
from pathlib import Path

import httpx

DB_PATH = Path.home() / ".niu" / "messages.db"
API_BASE = "http://localhost:9876"


async def poll_db_during_chat(duration: int = 120, interval: float = 1.0):
    """在 Agent 处理期间持续轮询 DB，记录增量消息"""

    # 获取初始状态
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.execute("SELECT MAX(rowid) FROM messages")
    initial_rowid = cursor.fetchone()[0] or 0
    conn.close()

    print(f"初始 max_rowid = {initial_rowid}")
    print("开始轮询 DB...")
    print()

    seen_rowids = set()
    events = []
    start_time = time.time()

    # 先记录初始消息
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("SELECT rowid, role, length(content), substr(content, 1, 60) FROM messages WHERE rowid > ?", (initial_rowid,))
    for row in cursor.fetchall():
        seen_rowids.add(row[0])
    conn.close()

    while time.time() - start_time < duration:
        await asyncio.sleep(interval)

        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        # 获取 max_rowid
        cursor = conn.execute("SELECT MAX(rowid) FROM messages")
        max_rowid = cursor.fetchone()[0] or 0

        # 获取新增消息
        cursor = conn.execute(
            "SELECT rowid, role, length(content), substr(content, 1, 80) "
            "FROM messages WHERE rowid > ? ORDER BY rowid ASC",
            (initial_rowid,)
        )
        new_rows = cursor.fetchall()

        conn.close()

        elapsed = round(time.time() - start_time, 1)

        for row in new_rows:
            if row[0] not in seen_rowids:
                seen_rowids.add(row[0])
                marker = " <<< ASSISTANT" if row[1] == "assistant" and row[2] > 0 else ""
                print(f"[{elapsed}s] rowid={row[0]} role={row[1]} len={row[2]}{marker}")
                print(f"         {row[3]}")
                events.append({
                    "elapsed": elapsed,
                    "rowid": row[0],
                    "role": row[1],
                    "content_len": row[2],
                    "preview": row[3],
                })

        if max_rowid > initial_rowid and len(new_rows) == len(seen_rowids) - len(set()):
            pass  # no new messages this poll

    # 汇总
    print()
    print("=" * 60)
    assistant_events = [e for e in events if e["role"] == "assistant" and e["content_len"] > 0]
    print(f"Agent 处理期间出现的 assistant 文本消息: {len(assistant_events)} 次")
    for e in assistant_events:
        print(f"  {e['elapsed']}s  rowid={e['rowid']}  len={e['content_len']}  '{e['preview']}'")

    if len(assistant_events) >= 2:
        print()
        print("[结论] 多轮 assistant 消息分段写入 DB ✅")
        print("       push_incremental 的设计思路可行！")
        print("       问题是实现层的 bug，不是数据层的问题。")
    elif len(assistant_events) == 1:
        print()
        print("[结论] 只有1条 assistant 消息")
        print("       可能是 Agent 很快完成了，或只在最终才写入 DB")
    else:
        print()
        print("[结论] 0 条 assistant 消息！Agent 可能没有回复")

    return events


async def main():
    # 先清空 DB
    print("清空 DB...")
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{API_BASE}/api/chat/clear")
        print(f"  清空结果: {resp.json()}")
    print()

    # 发一条需要多轮工具调用的消息
    query = "帮我查一下知识图谱里有没有关于机器学习的文档，然后把结果整理成笔记保存到 ~/.niu/notes/test_ml_note.json"
    print(f"发送查询: {query}")
    print()

    # 并行：一个协程发消息，一个协程轮询 DB
    chat_task = asyncio.create_task(
        _send_chat(query)
    )
    poll_task = asyncio.create_task(
        poll_db_during_chat(duration=120, interval=0.5)
    )

    # 等两个都完成
    chat_result = await chat_task
    await poll_task

    print()
    print("=" * 60)
    print(f"Chat 完成耗时: {chat_result.get('elapsed', '?')}s")
    print(f"回复长度: {len(chat_result.get('reply', ''))} 字符")


async def _send_chat(message: str):
    """通过 Electron API 发送消息"""
    start = time.time()
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{API_BASE}/api/chat/session",
            json={"message": message},
        )
        elapsed = round(time.time() - start, 1)
        data = resp.json()
        data["elapsed"] = elapsed
        return data


if __name__ == "__main__":
    asyncio.run(main())
