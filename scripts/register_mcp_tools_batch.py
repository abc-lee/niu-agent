#!/usr/bin/env python3
"""
MCP 工具注册脚本（分批次版）

每次注册5个工具，避免服务过载。
用法：python scripts/register_mcp_tools_batch.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger


async def register_batch(client, tools_batch, batch_num):
    """注册一批工具"""
    import httpx

    register_data = []
    for tool in tools_batch:
        parts = tool["name"].split("/", 1)
        server_name = parts[0] if len(parts) > 1 else "unknown"
        tool_name = parts[1] if len(parts) > 1 else tool["name"]

        register_data.append({
            "server_name": server_name,
            "tool_name": tool_name,
            "description": tool.get("description", ""),
            "input_schema": tool.get("input_schema", {}),
        })

    try:
        resp = await client.post(
            "http://127.0.0.1:9876/api/inject/mcp-tools/batch",
            json=register_data,
            timeout=120.0
        )

        if resp.status_code != 200:
            print(f"  [Batch {batch_num}] Failed: status {resp.status_code}")
            return 0, len(tools_batch)

        result = resp.json()
        results = result.get("results", [])
        success = sum(1 for r in results if r.get("status") == "success")

        print(f"  [Batch {batch_num}] Success: {success}/{len(tools_batch)}")
        return success, len(tools_batch) - success

    except Exception as e:
        print(f"  [Batch {batch_num}] Error: {e}")
        return 0, len(tools_batch)


async def main():
    print("=" * 60)
    print("MCP Tools Registration (Batch Mode: 5 tools/batch)")
    print("=" * 60)
    print()

    # 1. 加载工具
    from agent.mcp_client import load_mcp_configs, list_mcp_tools

    print("Loading MCP tools...")
    load_mcp_configs()
    tools = await list_mcp_tools(force_reload=True)
    print(f"[OK] Found {len(tools)} tools\n")

    # 2. 检查已注册的工具
    import sqlite3
    import json

    db_path = "E:/tmp/bot/vectors.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.execute("SELECT id FROM documents WHERE metadata LIKE '%mcp_tool%'")
    registered = {row[0] for row in cursor.fetchall()}
    conn.close()

    # 过滤已注册的工具
    tools_to_register = []
    for tool in tools:
        parts = tool["name"].split("/", 1)
        tool_name = parts[1] if len(parts) > 1 else tool["name"]
        doc_id = f"mcp_tool:{parts[0]}:{tool_name}"

        if doc_id not in registered:
            tools_to_register.append(tool)

    print(f"Already registered: {len(registered)} tools")
    print(f"To register: {len(tools_to_register)} tools\n")

    if not tools_to_register:
        print("All tools already registered!")
        return

    # 3. 分批次注册
    import httpx

    batch_size = 5
    total_success = 0
    total_failed = 0

    async with httpx.AsyncClient() as client:
        for i in range(0, len(tools_to_register), batch_size):
            batch = tools_to_register[i:i+batch_size]
            batch_num = (i // batch_size) + 1

            success, failed = await register_batch(client, batch, batch_num)
            total_success += success
            total_failed += failed

            # 批次间等待
            if i + batch_size < len(tools_to_register):
                print(f"  Waiting 5 seconds...")
                await asyncio.sleep(5)

    # 4. 总结
    print()
    print("=" * 60)
    print(f"Registration Complete:")
    print(f"  - Total processed: {len(tools_to_register)}")
    print(f"  - Success: {total_success}")
    print(f"  - Failed: {total_failed}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
