#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 Page-Agent 完整功能
"""

import requests
import json
import time

BASE_URL = "http://localhost:9876"
PAGE_AGENT_API = "http://localhost:38402"

def test_1_check_page_agent_status():
    """测试 1: 检查 Page-Agent 服务状态"""
    print("\n" + "="*80)
    print("测试 1: 检查 Page-Agent 服务状态")
    print("="*80)

    try:
        response = requests.get(f"{PAGE_AGENT_API}/status", timeout=5)
        data = response.json()
        print(f"✅ 服务运行中")
        print(f"   Connected: {data.get('connected')}")
        print(f"   Busy: {data.get('busy')}")
        return data.get('connected', False)
    except Exception as e:
        print(f"❌ 服务未启动: {e}")
        return False


def test_2_execute_simple_task():
    """测试 2: 执行简单任务（同步）"""
    print("\n" + "="*80)
    print("测试 2: 执行简单任务（同步）")
    print("="*80)

    task = "打开百度首页，返回页面标题"

    print(f"任务: {task}")
    print("开始执行...")

    start_time = time.time()

    try:
        response = requests.post(
            f"{PAGE_AGENT_API}/execute",
            json={"task": task},
            timeout=30
        )

        elapsed = time.time() - start_time

        if response.status_code == 200:
            data = response.json()
            print(f"✅ 执行成功 (耗时: {elapsed:.1f}s)")
            print(f"   结果: {data.get('data', '')[:200]}")
            return True
        else:
            print(f"❌ 执行失败: HTTP {response.status_code}")
            print(f"   错误: {response.text[:200]}")
            return False
    except requests.Timeout:
        print(f"❌ 超时（30秒）")
        return False
    except Exception as e:
        print(f"❌ 错误: {e}")
        return False


def test_3_async_task_notification():
    """测试 3: 异步任务通知机制"""
    print("\n" + "="*80)
    print("测试 3: 异步任务通知机制")
    print("="*80)

    # 清空 pending_alerts
    try:
        requests.get(f"{BASE_URL}/api/pending-alerts/clear", timeout=5)
        print("已清空 pending_alerts 队列")
    except:
        print("⚠️  无法清空队列，继续测试")

    # 发送异步任务通知
    notify_payload = {
        "type": "task_complete",
        "result": "测试任务完成：成功打开了百度首页，标题是'百度一下，你就知道'"
    }

    print(f"发送异步任务通知...")
    print(f"负载: {json.dumps(notify_payload, ensure_ascii=False)}")

    try:
        response = requests.post(
            f"{BASE_URL}/api/async-task/notify",
            json=notify_payload,
            timeout=60
        )

        if response.status_code == 200:
            data = response.json()
            print(f"✅ 通知成功")
            print(f"   主 Agent 回复: {data.get('reply', '')[:100]}")

            # 检查 pending_alerts
            time.sleep(2)
            alerts_response = requests.get(f"{BASE_URL}/api/pending-alerts", timeout=5)

            if alerts_response.status_code == 200:
                alerts = alerts_response.json()
                print(f"   pending_alerts 队列: {len(alerts)} 条消息")

                if alerts:
                    print(f"   最新消息: {alerts[-1].get('content', '')[:100]}")
                    return True
                else:
                    print(f"⚠️  队列为空")
                    return False
            else:
                print(f"⚠️  无法检查队列")
                return False
        else:
            print(f"❌ 通知失败: HTTP {response.status_code}")
            print(f"   错误: {response.text[:200]}")
            return False
    except Exception as e:
        print(f"❌ 错误: {e}")
        return False


def test_4_mcp_tool_execution():
    """测试 4: 通过 MCP 工具调用（主 Agent 方式）"""
    print("\n" + "="*80)
    print("测试 4: 通过 MCP 工具调用")
    print("="*80)

    # 这里需要实际调用主 Agent 的聊天接口
    # 由于主 Agent 需要启动，我们直接测试工具注册

    try:
        from agent.tool_registry import get_registry

        registry = get_registry()

        # 检查工具是否注册
        tools = registry.get_schemas()
        page_agent_tools = [t for t in tools if t['name'].startswith('page-agent')]

        print(f"✅ 找到 {len(page_agent_tools)} 个 Page-Agent 工具:")
        for tool in page_agent_tools:
            print(f"   - {tool['name']}")

        # 测试 get_status
        print("\n测试 get_status 工具:")
        status_func = registry.get("page-agent-mcp/get_status")
        if status_func:
            result = status_func()
            print(f"   结果: {result}")
            return True
        else:
            print(f"❌ 工具未注册")
            return False

    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    print("\n" + "="*80)
    print("Page-Agent 完整功能测试")
    print("="*80)

    results = {}

    # 测试 1: 服务状态
    results['status'] = test_1_check_page_agent_status()

    if not results['status']:
        print("\n❌ Page-Agent 服务未启动，跳过后续测试")
        print("请先启动: node mcp-servers/page-agent-mcp/src/index.js")
        return

    # 测试 2: 同步执行
    results['sync'] = test_2_execute_simple_task()

    # 测试 3: 异步通知
    results['async'] = test_3_async_task_notification()

    # 测试 4: MCP 工具
    results['mcp'] = test_4_mcp_tool_execution()

    # 汇总
    print("\n" + "="*80)
    print("测试结果汇总")
    print("="*80)

    for name, passed in results.items():
        status = "✅ 通过" if passed else "❌ 失败"
        print(f"{name:20s} {status}")

    total = len(results)
    passed = sum(1 for v in results.values() if v)

    print(f"\n总计: {passed}/{total} 通过")

    if passed == total:
        print("\n🎉 所有测试通过！")
    else:
        print("\n⚠️  部分测试失败，请检查日志")


if __name__ == "__main__":
    main()
