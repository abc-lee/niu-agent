#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test Page-Agent tool availability"""

import sys
import json
import io

# Set UTF-8 output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Add path
sys.path.insert(0, 'mcp-servers/page-agent-mcp/src')

# Test 1: Import module
print("=" * 60)
print("Test 1: Import niu_page_agent module")
print("=" * 60)
try:
    import niu_page_agent
    print("✓ 成功导入 niu_page_agent")
except ImportError as e:
    print(f"✗ 导入失败: {e}")
    sys.exit(1)

# Test 2: Get tool schemas
print("\n" + "=" * 60)
print("Test 2: Get tool schemas")
print("=" * 60)
try:
    schemas = niu_page_agent.get_tool_schemas()
    print(f"✓ 获取到 {len(schemas)} 个工具:")
    for schema in schemas:
        print(f"  - {schema['name']}")
except Exception as e:
    print(f"✗ 获取 schemas 失败: {e}")
    sys.exit(1)

# Test 3: Call get_status
print("\n" + "=" * 60)
print("Test 3: Call get_status to check connection")
print("=" * 60)
try:
    result = niu_page_agent.get_status()
    status = json.loads(result)
    print(f"✓ Hub 状态: connected={status['connected']}, busy={status['busy']}")

    if not status['connected']:
        print("⚠ Hub 未连接（Chrome 扩展未运行）")
        print("  请确保:")
        print("  1. Chrome 浏览器已启动")
        print("  2. Page-Agent 扩展已安装并启用")
        print("  3. 扩展已连接到 ws://localhost:38401")
except Exception as e:
    print(f"✗ 调用失败: {e}")
    sys.exit(1)

# Test 4: Call execute_task (simple test)
print("\n" + "=" * 60)
print("Test 4: Test execute_task availability")
print("=" * 60)
try:
    # Just check if the function exists and has correct signature
    import inspect
    sig = inspect.signature(niu_page_agent.execute_task)
    print(f"✓ execute_task function signature: {sig}")
    print("  Ready to accept task descriptions")
except Exception as e:
    print(f"✗ Function check failed: {e}")
    sys.exit(1)

print("\n" + "=" * 60)
print("All tests passed! Page-Agent tools are available")
print("=" * 60)
print("\nNote: Page-Agent is ready to use.")
print("Chrome extension status:", "Connected" if json.loads(niu_page_agent.get_status())['connected'] else "Not connected")
