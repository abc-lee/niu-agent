"""
检查实际 API 运行中的工具注入

连接到运行中的 API，检查工具是否正确注入
"""

import sys
import os
import json
import requests
from pathlib import Path

# 修复 Windows 控制台编码
if sys.platform == 'win32':
    import io
    if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if not isinstance(sys.stderr, io.TextIOWrapper) or sys.stderr.encoding != 'utf-8':
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

API_BASE = "http://127.0.0.1:9876"

print("=" * 60)
print("检查实际 API 运行中的工具注入")
print("=" * 60)

# 1. 检查 API 是否运行
print("\n[步骤 1] 检查 API 健康状态...")
try:
    resp = requests.get(f"{API_BASE}/health", timeout=5)
    if resp.status_code == 200:
        print(f"✓ API 正在运行: {resp.json()}")
    else:
        print(f"✗ API 返回错误: {resp.status_code}")
        sys.exit(1)
except Exception as e:
    print(f"✗ 无法连接到 API: {e}")
    print("  请确保 API 正在运行：python -m niu_api")
    sys.exit(1)

# 2. 发送测试消息
print("\n[步骤 2] 发送测试消息...")
test_message = "请列出所有可用的工具名称"

try:
    resp = requests.post(
        f"{API_BASE}/chat/sync",
        json={
            "message": test_message,
            "session_id": "test-browser-tool"
        },
        timeout=60
    )

    if resp.status_code == 200:
        result = resp.json()
        reply = result.get("reply", "")
        print("✓ API 响应成功")
        print(f"\n回复内容:\n{reply[:500]}...")

        # 检查是否提到 browser_navigate
        if "browser" in reply.lower() or "浏览器" in reply:
            print("\n✓ 回复中包含浏览器相关内容")
        else:
            print("\n⚠ 回复中未明确提及浏览器工具")
    else:
        print(f"✗ API 返回错误: {resp.status_code}")
        print(f"  响应: {resp.text}")
except Exception as e:
    print(f"✗ 请求失败: {e}")
    sys.exit(1)

# 3. 检查工具是否被调用
print("\n[步骤 3] 发送明确的浏览器工具调用请求...")
test_message2 = "使用 browser_navigate 工具访问 https://example.com"

try:
    resp = requests.post(
        f"{API_BASE}/chat/sync",
        json={
            "message": test_message2,
            "session_id": "test-browser-tool"
        },
        timeout=60
    )

    if resp.status_code == 200:
        result = resp.json()
        reply = result.get("reply", "")
        print("✓ API 响应成功")
        print(f"\n回复内容:\n{reply[:500]}...")

        # 检查是否有工具调用相关内容
        if "browser" in reply.lower() or "导航" in reply or "navigate" in reply.lower():
            print("\n✓ 回复中包含浏览器导航相关内容")
        else:
            print("\n⚠ 回复中未包含浏览器导航相关内容")

        # 检查是否有错误
        if "tool not found" in reply.lower() or "工具未找到" in reply:
            print("\n✗ 工具未找到错误")
        elif "error" in reply.lower() or "错误" in reply:
            print("\n⚠ 回复中包含错误信息")
    else:
        print(f"✗ API 返回错误: {resp.status_code}")
        print(f"  响应: {resp.text}")
except Exception as e:
    print(f"✗ 请求失败: {e}")

print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)
