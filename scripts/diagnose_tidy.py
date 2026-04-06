"""
诊断睡眠整理功能

测试步骤：
1. 后端接口是否正常
2. 前端是否能触发
3. MCP 工具是否可用
"""

import requests
import json

API_URL = "http://127.0.0.1:9876"

def test_tidy_endpoint():
    """测试 /api/context/tidy 接口"""
    print("\n=== 测试 1: 后端接口 ===")

    try:
        response = requests.post(
            f"{API_URL}/api/context/tidy",
            json={"session_id": "default", "mode": "sleep"},
            timeout=60
        )

        print(f"状态码: {response.status_code}")
        print(f"响应: {json.dumps(response.json(), ensure_ascii=False, indent=2)}")

        if response.status_code == 200:
            print("✅ 后端接口正常")
        else:
            print("❌ 后端接口异常")

    except Exception as e:
        print(f"❌ 请求失败: {e}")


def test_messages_endpoint():
    """测试 /api/context/messages 接口"""
    print("\n=== 测试 2: 消息列表接口 ===")

    try:
        response = requests.get(
            f"{API_URL}/api/context/messages",
            params={"session_id": "default", "full": "true", "limit": 100}
        )

        print(f"状态码: {response.status_code}")
        data = response.json()
        print(f"消息总数: {data['total_in_db']}")
        print(f"返回消息数: {len(data['messages'])}")

        if data['messages']:
            # 计算总大小
            total_kb = sum(len(m['content']) for m in data['messages']) / 1024
            print(f"总大小: {total_kb:.1f} KB")

            # 显示第一条和最后一条消息
            print(f"\n第一条消息: {data['messages'][0]['role']}: {data['messages'][0]['content'][:50]}...")
            print(f"最后一条消息: {data['messages'][-1]['role']}: {data['messages'][-1]['content'][:50]}...")

        print("✅ 消息列表接口正常")

    except Exception as e:
        print(f"❌ 请求失败: {e}")


def test_delete_endpoint():
    """测试 /api/context/messages/delete 接口（不实际删除）"""
    print("\n=== 测试 3: 删除接口（空请求） ===")

    try:
        response = requests.post(
            f"{API_URL}/api/context/messages/delete",
            json={"session_id": "default", "message_indices": []}
        )

        print(f"状态码: {response.status_code}")
        print(f"响应: {json.dumps(response.json(), ensure_ascii=False)}")
        print("✅ 删除接口正常")

    except Exception as e:
        print(f"❌ 请求失败: {e}")


def test_health():
    """测试健康检查"""
    print("\n=== 测试 0: 健康检查 ===")

    try:
        response = requests.get(f"{API_URL}/health")
        print(f"状态码: {response.status_code}")
        print(f"响应: {json.dumps(response.json(), ensure_ascii=False, indent=2)}")
        print("✅ 服务正常运行")

    except Exception as e:
        print(f"❌ 服务未运行: {e}")
        return False

    return True


if __name__ == "__main__":
    print("=" * 60)
    print("睡眠整理功能诊断")
    print("=" * 60)

    # 先检查服务是否运行
    if not test_health():
        print("\n❌ 服务未运行，请先启动：go run main.go")
        exit(1)

    # 测试各个接口
    test_messages_endpoint()
    test_delete_endpoint()
    test_tidy_endpoint()

    print("\n" + "=" * 60)
    print("诊断完成")
    print("=" * 60)
    print("\n如果所有测试都通过，请检查：")
    print("1. 浏览器控制台是否有 [Tidy] 日志")
    print("2. 后端日志是否有 [Tidy] Context tidy triggered")
    print("3. 是否等待了 5 分钟进入睡眠状态")
