#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
快速测试：验证知识库注入是否工作

测试步骤：
1. 启动 niu_api（确保知识库 API 可访问）
2. 运行此脚本
3. 查看日志，确认知识库内容被注入
"""

import requests
import json

def test_kb_search():
    """测试知识库搜索 API"""
    print("\n" + "=" * 80)
    print("测试 1：知识库搜索 API")
    print("=" * 80)

    try:
        response = requests.get(
            "http://localhost:9876/kb/search",
            params={"q": "MBTI", "limit": 3},
            timeout=5
        )

        if response.status_code == 200:
            data = response.json()
            print(f"✅ 搜索成功")
            print(f"   结果数: {data['total']}")
            for i, result in enumerate(data['results'], 1):
                print(f"\n   结果 {i}: {result['title']}")
                print(f"   相关度: {result['relevance']}")
                print(f"   内容预览: {result['content'][:100]}...")
            return True
        else:
            print(f"❌ 搜索失败: HTTP {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ 连接失败: {e}")
        return False


def test_kb_answer():
    """测试知识库问答 API"""
    print("\n" + "=" * 80)
    print("测试 2：知识库问答 API")
    print("=" * 80)

    try:
        response = requests.get(
            "http://localhost:9876/kb/answer",
            params={
                "context": "MBTI人格测试",
                "question": "什么是外向型人格"
            },
            timeout=5
        )

        if response.status_code == 200:
            data = response.json()
            print(f"✅ 问答成功")
            print(f"   问题: 什么是外向型人格")
            print(f"   答案: {data['answer'][:200]}...")
            print(f"   置信度: {data['confidence']}")
            return True
        else:
            print(f"❌ 问答失败: HTTP {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ 连接失败: {e}")
        return False


def test_kb_injection():
    """测试知识库注入（需要 Page-Agent 扩展连接）"""
    print("\n" + "=" * 80)
    print("测试 3：知识库注入（需要 Page-Agent 扩展）")
    print("=" * 80)

    # 检查 Page-Agent 状态
    try:
        response = requests.get("http://localhost:38402/status", timeout=5)
        data = response.json()

        if not data['connected']:
            print("⚠️  Page-Agent 扩展未连接")
            print("   请确保：")
            print("   1. Chrome 扩展已安装")
            print("   2. Hub 页面已打开（http://localhost:38401）")
            return False

        print("✅ Page-Agent 扩展已连接")

        # 执行包含 MBTI 关键词的简单任务
        task = """
打开百度首页（https://www.baidu.com）
在搜索框中输入 "MBTI人格测试"
返回搜索框的 value 属性
"""

        print(f"\n执行任务: {task.strip()}")
        print("\n提示：查看日志以验证知识库注入")
        print("   tail -f E:/tools/ai-bot/logs/api_stderr.log | grep 'kb-'")

        response = requests.post(
            "http://localhost:38402/execute",
            json={"task": task},
            timeout=30
        )

        if response.status_code == 200:
            result = response.json()
            print(f"\n✅ 任务完成: {result.get('data', '')[:100]}")
            return True
        else:
            print(f"\n❌ 任务失败: HTTP {response.status_code}")
            return False

    except Exception as e:
        print(f"❌ 连接失败: {e}")
        return False


def main():
    print("🔍 知识库注入测试")
    print("=" * 80)
    print("\n提示：确保 niu_api 正在运行")
    print("   python -m niu_api")

    results = {
        "知识库搜索 API": test_kb_search(),
        "知识库问答 API": test_kb_answer(),
    }

    # 测试注入（可选）
    print("\n是否测试知识库注入？（需要 Page-Agent 扩展）")
    choice = input("输入 y 继续，其他键跳过: ").strip().lower()

    if choice == 'y':
        results["知识库注入"] = test_kb_injection()
    else:
        print("跳过注入测试")

    # 总结
    print("\n" + "=" * 80)
    print("📊 测试总结")
    print("=" * 80)

    for name, passed in results.items():
        status = "✅ 通过" if passed else "❌ 失败"
        print(f"{name}: {status}")

    all_passed = all(results.values())

    if all_passed:
        print("\n🎉 所有测试通过！知识库注入功能正常。")
    else:
        print("\n⚠️  部分测试失败，请检查服务状态。")

    return 0 if all_passed else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
