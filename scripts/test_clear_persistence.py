"""
测试清空消息后的数据库状态
"""
import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.session import get_message_store


async def test_clear_persistence():
    """测试清空消息后重新打开窗口的场景"""
    print("=" * 60)
    print("测试清空消息持久性")
    print("=" * 60)

    store = await get_message_store()

    # 1. 添加测试消息
    print("\n[1] 添加测试消息...")
    await store.add_message(role="user", content="测试消息 1")
    await store.add_message(role="assistant", content="测试回复 1")
    count_before = await store.count_messages()
    print(f"   消息数: {count_before}")

    # 2. 清空消息
    print("\n[2] 清空消息...")
    deleted = await store.clear_messages()
    print(f"   删除了 {deleted} 条消息")

    # 3. 检查数据库
    print("\n[3] 检查数据库...")
    count_after = await store.count_messages()
    print(f"   消息数: {count_after}")

    # 4. 重新加载消息（模拟重新打开窗口）
    print("\n[4] 重新加载消息（模拟重新打开窗口）...")
    messages = await store.get_messages(limit=20)
    print(f"   加载到 {len(messages)} 条消息")

    if len(messages) == 0:
        print("\n✅ 清空持久性测试通过！")
    else:
        print(f"\n❌ 清空失败：还有 {len(messages)} 条消息")
        for msg in messages[:5]:
            print(f"   - {msg.role}: {msg.content[:50]}")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_clear_persistence())
