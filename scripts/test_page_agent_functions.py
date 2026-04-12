#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Page-Agent 功能测试脚本
测试 4 个核心功能：
1. 服务状态检查
2. 浏览器自动化任务
3. 异步通知机制
4. 知识库集成
"""

import requests
import json
import time
from datetime import datetime
import sys
import io

# 设置 UTF-8 输出
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 配置
PAGE_AGENT_API = "http://localhost:38402"
MAIN_API = "http://localhost:9876"

def print_test(test_name, success, message="", details=None):
    """打印测试结果"""
    status = "✅ 通过" if success else "❌ 失败"
    print(f"\n{status} | {test_name}")
    if message:
        print(f"   {message}")
    if details:
        print(f"   详情: {json.dumps(details, ensure_ascii=False, indent=2)}")


def test_1_status_check():
    """测试 1: 服务状态检查"""
    print("\n" + "="*60)
    print("测试 1: 服务状态检查")
    print("="*60)

    try:
        # 测试 Page-Agent API
        start = time.time()
        response = requests.get(f"{PAGE_AGENT_API}/status", timeout=5)
        elapsed = time.time() - start

        if response.status_code == 200:
            data = response.json()
            print_test(
                "Page-Agent 状态检查",
                True,
                f"响应时间: {elapsed:.2f}s",
                data
            )

            # 测试主 API
            response = requests.get(f"{MAIN_API}/health", timeout=5)
            if response.status_code == 200:
                data = response.json()
                print_test(
                    "主 API 状态检查",
                    True,
                    f"服务状态: {data.get('status')}",
                    data
                )
                return True
            else:
                print_test("主 API 状态检查", False, f"HTTP {response.status_code}")
                return False
        else:
            print_test("Page-Agent 状态检查", False, f"HTTP {response.status_code}")
            return False

    except Exception as e:
        print_test("服务状态检查", False, str(e))
        return False


def test_2_browser_task():
    """测试 2: 浏览器自动化任务"""
    print("\n" + "="*60)
    print("测试 2: 浏览器自动化任务")
    print("="*60)

    # 检查是否已连接
    try:
        status_resp = requests.get(f"{PAGE_AGENT_API}/status", timeout=5)
        status = status_resp.json()

        if not status.get("connected"):
            print_test("浏览器连接检查", False, "浏览器未连接")
            return False

        if status.get("busy"):
            print_test("浏览器状态检查", False, "浏览器正在执行任务")
            return False

        print_test("浏览器状态检查", True, "已连接且空闲")
    except Exception as e:
        print_test("浏览器状态检查", False, str(e))
        return False

    # 执行简单任务（获取标题）
    try:
        task = "打开 https://example.com，获取页面标题并返回"
        print(f"\n执行任务: {task}")

        start = time.time()
        response = requests.post(
            f"{PAGE_AGENT_API}/execute",
            json={"task": task},
            timeout=60
        )
        elapsed = time.time() - start

        if response.status_code == 200:
            data = response.json()
            success = data.get("success", False)

            if success:
                print_test(
                    "浏览器任务执行",
                    True,
                    f"耗时: {elapsed:.2f}s",
                    {"result": data.get("data", "")[:200]}
                )
                return True
            else:
                print_test(
                    "浏览器任务执行",
                    False,
                    f"任务失败: {data.get('data', '')[:100]}"
                )
                return False
        else:
            print_test("浏览器任务执行", False, f"HTTP {response.status_code}")
            return False

    except requests.exceptions.Timeout:
        print_test("浏览器任务执行", False, "请求超时（60秒）")
        return False
    except Exception as e:
        print_test("浏览器任务执行", False, str(e))
        return False


def test_3_async_notification():
    """测试 3: 异步通知机制"""
    print("\n" + "="*60)
    print("测试 3: 异步通知机制")
    print("="*60)

    try:
        # 发送异步通知
        payload = {
            "type": "task_complete",
            "result": f"测试任务完成 - {datetime.now().isoformat()}"
        }

        print(f"\n发送通知: {payload}")

        start = time.time()
        response = requests.post(
            f"{MAIN_API}/api/async-task/notify",
            json=payload,
            timeout=60
        )
        elapsed = time.time() - start

        if response.status_code == 200:
            data = response.json()
            success = data.get("success", False)

            if success:
                print_test(
                    "异步通知发送",
                    True,
                    f"耗时: {elapsed:.2f}s",
                    {"reply": data.get("reply", "")[:100]}
                )
                return True
            else:
                print_test(
                    "异步通知发送",
                    False,
                    f"失败: {data.get('error', '')}"
                )
                return False
        else:
            print_test("异步通知发送", False, f"HTTP {response.status_code}")
            return False

    except requests.exceptions.Timeout:
        print_test("异步通知发送", False, "请求超时（60秒）")
        return False
    except Exception as e:
        print_test("异步通知发送", False, str(e))
        return False


def test_4_knowledge_base():
    """测试 4: 知识库集成"""
    print("\n" + "="*60)
    print("测试 4: 知识库集成")
    print("="*60)

    try:
        # 测试知识库搜索
        query = "MBTI人格测试"
        print(f"\n搜索知识库: {query}")

        start = time.time()
        response = requests.get(
            f"{MAIN_API}/kb/search",
            params={"q": query, "limit": 3},
            timeout=5
        )
        elapsed = time.time() - start

        if response.status_code == 200:
            data = response.json()
            success = data.get("success", False)

            if success:
                results = data.get("results", [])
                print_test(
                    "知识库搜索",
                    True,
                    f"找到 {len(results)} 条结果，耗时: {elapsed:.2f}s",
                    {"first_result": results[0]["title"] if results else "无结果"}
                )
                return True
            else:
                print_test("知识库搜索", False, "搜索失败")
                return False
        else:
            print_test("知识库搜索", False, f"HTTP {response.status_code}")
            return False

    except Exception as e:
        print_test("知识库搜索", False, str(e))
        return False


def main():
    """运行所有测试"""
    print("\n" + "="*60)
    print(" Page-Agent 功能测试套件")
    print(f" 测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)

    results = {
        "服务状态检查": test_1_status_check(),
        "浏览器自动化任务": test_2_browser_task(),
        "异步通知机制": test_3_async_notification(),
        "知识库集成": test_4_knowledge_base(),
    }

    # 总结
    print("\n" + "="*60)
    print(" 测试总结")
    print("="*60)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for test_name, result in results.items():
        status = "✅ 通过" if result else "❌ 失败"
        print(f"{status} | {test_name}")

    print(f"\n总计: {passed}/{total} 通过")

    if passed == total:
        print("\n🎉 所有测试通过！")
        return 0
    else:
        print(f"\n⚠️  {total - passed} 个测试失败")
        return 1


if __name__ == "__main__":
    exit(main())
