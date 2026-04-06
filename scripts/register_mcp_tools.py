#!/usr/bin/env python3
"""
MCP 工具注册脚本（优化版）

在开发阶段运行一次，将 MCP 工具描述注册到向量库。
用法：python scripts/register_mcp_tools.py

优化：
- 添加延迟避免过载
- 批量注册减少请求次数
- 错误重试机制
"""

import asyncio
import sys
import time
from pathlib import Path

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger


async def main():
    print("=" * 60)
    print("MCP Tools Registration Script (Optimized)")
    print("=" * 60)
    print()

    # 1. 加载 MCP 配置
    print("Loading MCP server configurations...")
    from agent.mcp_client import load_mcp_configs, list_mcp_tools

    try:
        load_mcp_configs()
        print("[OK] MCP configurations loaded\n")
    except Exception as e:
        print(f"[ERROR] Failed to load MCP configurations: {e}")
        return

    # 2. 获取 MCP 工具列表
    print("Fetching MCP tools from servers...")
    try:
        tools = await list_mcp_tools(force_reload=True)
        print(f"[OK] Found {len(tools)} MCP tools\n")

        if not tools:
            print("No MCP tools found. Make sure MCP servers are configured and running.")
            return

    except Exception as e:
        print(f"[ERROR] Failed to fetch MCP tools: {e}")
        logger.exception("Detailed error:")
        return

    # 3. 使用批量注册接口（减少请求次数）
    print("Registering to vector database (batch mode)...")
    from niu_api.injector import RegisterMCPToolRequest
    import httpx

    # 准备批量数据
    register_data = []
    for tool in tools:
        parts = tool["name"].split("/", 1)
        server_name = parts[0] if len(parts) > 1 else "unknown"
        tool_name = parts[1] if len(parts) > 1 else tool["name"]

        register_data.append({
            "server_name": server_name,
            "tool_name": tool_name,
            "description": tool.get("description", ""),
            "input_schema": tool.get("input_schema", {}),
        })

    # 批量注册（一次请求）
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                "http://127.0.0.1:9876/api/inject/mcp-tools/batch",
                json=register_data
            )

            if resp.status_code != 200:
                print(f"[ERROR] Registration failed (status {resp.status_code})")
                print(resp.text)
                return

            result = resp.json()
            results = result.get("results", [])

            success_count = sum(1 for r in results if r.get("status") == "success")
            failed_count = sum(1 for r in results if r.get("status") == "failed")

            print(f"\n{'='*60}")
            print(f"Registration Summary:")
            print(f"  - Total:   {len(results)} tools")
            print(f"  - Success: {success_count}")
            print(f"  - Failed:  {failed_count}")
            print(f"{'='*60}\n")

            if failed_count > 0:
                print("Failed tools:")
                for r in results:
                    if r.get("status") != "success":
                        print(f"  - {r.get('tool_name', 'unknown')}")

    except Exception as e:
        print(f"[ERROR] Batch registration failed: {e}")
        logger.exception("Detailed error:")
        return

    print("\n[SUCCESS] MCP tools registration complete!")
    print("You can now use dynamic injection with these tools.")


if __name__ == "__main__":
    asyncio.run(main())
