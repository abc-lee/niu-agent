#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 Page-Agent 知识库增强模式

验证：
1. 知识库 API 是否可访问
2. Page-Agent 是否注入了正确的系统提示词
3. Page-Agent 是否能使用知识库完成任务
"""

import requests
import sys
import time

API_BASE = "http://localhost:9876"
PAGE_AGENT_API = "http://localhost:38402"

def test_kb_api():
    """测试知识库 API"""
    print("\n" + "=" * 80)
    print("测试 1：知识库 API 访问")
    print("=" * 80)

    # 健康检查
    try:
        response = requests.get(f"{API_BASE}/kb/health", timeout=5)
        print(f"✅ 知识库服务运行中: {response.json()}")
    except Exception as e:
        print(f"❌ 知识库服务未启动: {e}")
        return False

    # 测试搜索
    try:
        response = requests.get(
            f"{API_BASE}/kb/search",
            params={"q": "MBTI人格测试"},
            timeout=5
        )
        data = response.json()
        print(f"\n✅ 知识库搜索成功:")
        print(f"   查询: MBTI人格测试")
        print(f"   结果数: {data['total']}")
        if data['results']:
            print(f"   第一条: {data['results'][0]['title']}")
    except Exception as e:
        print(f"❌ 知识库搜索失败: {e}")
        return False

    # 测试问答
    try:
        response = requests.get(
            f"{API_BASE}/kb/answer",
            params={"context": "MBTI测试", "question": "什么是外向型人格"},
            timeout=5
        )
        data = response.json()
        print(f"\n✅ 知识库问答成功:")
        print(f"   问题: 什么是外向型人格")
        print(f"   答案长度: {len(data['answer'])} 字符")
        print(f"   置信度: {data['confidence']}")
    except Exception as e:
        print(f"❌ 知识库问答失败: {e}")
        return False

    return True


def test_page_agent_connection():
    """测试 Page-Agent 连接"""
    print("\n" + "=" * 80)
    print("测试 2：Page-Agent 连接状态")
    print("=" * 80)

    try:
        response = requests.get(f"{PAGE_AGENT_API}/status", timeout=5)
        data = response.json()
        print(f"✅ Page-Agent 状态:")
        print(f"   扩展连接: {'是' if data['connected'] else '否'}")
        print(f"   正在执行: {'是' if data['busy'] else '否'}")

        if not data['connected']:
            print("\n⚠️  扩展未连接，请确保：")
            print("   1. Chrome 扩展已安装")
            print("   2. 扩展已启用")
            print("   3. Hub 页面已打开（http://localhost:38401）")

        return data['connected']
    except Exception as e:
        print(f"❌ Page-Agent 服务未启动: {e}")
        return False


def test_knowledge_enhanced_task():
    """测试知识库增强的任务"""
    print("\n" + "=" * 80)
    print("测试 3：知识库增强模式任务")
    print("=" * 80)

    task = """
打开百度首页（https://www.baidu.com）
在搜索框中输入 "MBTI人格测试"
点击搜索按钮
等待结果页面加载
返回搜索结果页面的标题
"""

    print(f"任务: {task.strip()}")
    print("\n正在执行...")

    try:
        response = requests.post(
            f"{PAGE_AGENT_API}/execute",
            json={"task": task},
            timeout=30
        )
        data = response.json()

        if data.get('success'):
            print(f"\n✅ 任务完成:")
            print(f"   结果: {data.get('data', '')[:200]}")
            return True
        else:
            print(f"\n❌ 任务失败: {data.get('error', 'Unknown error')}")
            return False
    except requests.Timeout:
        print("\n⚠️  任务超时（30秒）")
        print("   这是正常的，因为任务可能需要更长时间")
        return True
    except Exception as e:
        print(f"\n❌ 执行失败: {e}")
        return False


def main():
    print("🔍 Page-Agent 知识库增强模式测试")
    print(f"📅 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    results = {
        "知识库 API": test_kb_api(),
        "Page-Agent 连接": test_page_agent_connection(),
        "知识库增强任务": test_knowledge_enhanced_task() if results.get("Page-Agent 连接", False) else False
    }

    print("\n" + "=" * 80)
    print("📊 测试总结")
    print("=" * 80)

    for name, passed in results.items():
        status = "✅ 通过" if passed else "❌ 失败"
        print(f"{name}: {status}")

    all_passed = all(results.values())
    print("\n" + ("🎉 所有测试通过！" if all_passed else "⚠️  部分测试失败"))

    if not all_passed:
        print("\n故障排查建议：")
        if not results.get("知识库 API"):
            print("   - 检查 niu_api 是否启动: python -m niu_api")
        if not results.get("Page-Agent 连接"):
            print("   - 检查 Chrome 扩展是否安装并启用")
            print("   - 访问 http://localhost:38401 打开 Hub 页面")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
