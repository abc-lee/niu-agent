"""
完整的 /new 命令端到端测试
模拟用户操作流程
"""
import asyncio
import httpx
import sys
from pathlib import Path

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.session import get_message_store


async def test_new_command_e2e():
    """端到端测试 /new 命令"""
    print("=" * 60)
    print("端到端测试 /new 命令")
    print("=" * 60)

    base_url = "http://127.0.0.1:9876"
    client = httpx.AsyncClient(timeout=30.0)

    # 1. 发送消息
    print("\n[1] 发送消息...")
    try:
        response = await client.post(f"{base_url}/api/chat/session", json={"message": "测试消息 1"})
        result = response.json()
        print(f"   响应: {result.get('reply', '')[:50]}")
    except Exception as e:
        print(f"   ⚠️ 发送失败: {e}")
        print("   可能是服务未启动，跳过此步骤")

    # 2. 检查消息数
    print("\n[2] 检查消息数...")
    store = await get_message_store()
    count_before = await store.count_messages()
    print(f"   消息数: {count_before}")

    # 3. 清空消息
    print("\n[3] 清空消息...")
    try:
        response = await client.post(f"{base_url}/api/chat/clear", json={"sessionId": "default"})
        result = response.json()
        print(f"   结果: {result}")
    except Exception as e:
        print(f"   ❌ 清空失败: {e}")
        return

    # 4. 检查数据库
    print("\n[4] 检查数据库...")
    count_after = await store.count_messages()
    print(f"   消息数: {count_after}")

    # 5. 发送新消息
    print("\n[5] 发送新消息...")
    try:
        response = await client.post(f"{base_url}/api/chat/session", json={"message": "清空后的新消息"})
        result = response.json()
        print(f"   响应: {result.get('reply', '')[:50]}")
    except Exception as e:
        print(f"   ⚠️ 发送失败: {e}")

    # 6. 再次检查消息数
    print("\n[6] 再次检查消息数...")
    count_final = await store.count_messages()
    print(f"   消息数: {count_final}")

    # 7. 加载历史消息（模拟重新打开窗口）
    print("\n[7] 加载历史消息...")
    try:
        response = await client.get(f"{base_url}/api/context/messages?limit=20&full=true")
        result = response.json()
        messages = result.get('messages', [])
        print(f"   加载到 {len(messages)} 条消息")
        for msg in messages[:5]:
            print(f"   - {msg['role']}: {msg['content'][:50]}")
    except Exception as e:
        print(f"   ❌ 加载失败: {e}")

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_new_command_e2e())
