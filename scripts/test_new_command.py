"""
测试 /new 命令的完整流程
"""
import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.session import get_message_store
from agent.runner import get_runner
from niu_api.chat import get_or_create_runner


async def test_new_command():
    """测试 /new 命令的完整流程"""
    print("=" * 60)
    print("测试 /new 命令")
    print("=" * 60)

    # 1. 添加测试消息
    print("\n[1] 添加测试消息...")
    store = await get_message_store()
    await store.add_message(role="user", content="测试消息 1")
    await store.add_message(role="assistant", content="测试回复 1")
    count = await store.count_messages()
    print(f"   当前消息数: {count}")

    # 2. 初始化 runner
    print("\n[2] 初始化 runner...")
    runner = get_or_create_runner()
    print(f"   Runner: {runner}")
    print(f"   Handler: {runner.handler if runner else None}")

    # 3. 清空消息（模拟 /new）
    print("\n[3] 清空消息...")
    deleted = await store.clear_messages()
    print(f"   删除了 {deleted} 条消息")

    # 4. 重置 runner 状态
    print("\n[4] 重置 runner 状态...")
    if runner:
        if runner.handler:
            runner.handler.reset_working_memory()
            print("   ✅ 工作记忆已重置")

        if runner.client and hasattr(runner.client, 'backend'):
            if hasattr(runner.client.backend, 'history'):
                runner.client.backend.history = []
                print("   ✅ LLM session history 已清空")

    # 5. 验证清空结果
    print("\n[5] 验证清空结果...")
    count = await store.count_messages()
    print(f"   当前消息数: {count}")

    if count == 0:
        print("\n✅ /new 命令测试通过！")
    else:
        print(f"\n❌ /new 命令失败：还有 {count} 条消息未清空")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_new_command())
