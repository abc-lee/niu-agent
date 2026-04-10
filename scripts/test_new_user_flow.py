"""
模拟用户的完整操作流程
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.session import get_message_store
from niu_api.chat import get_or_create_runner, init_runner
from agent.tool_registry import get_registry


async def simulate_user_flow():
    """模拟用户操作流程"""
    print("=" * 60)
    print("模拟用户操作流程")
    print("=" * 60)

    # 初始化
    registry = get_registry()
    init_runner(registry)
    store = await get_message_store()

    # 1. 用户发送消息
    print("\n[1] 用户发送消息...")
    runner = get_or_create_runner()

    def send_message(msg):
        chunks = []
        for chunk in runner.chat("default", msg, stream=False):
            chunks.append(chunk)
        return "".join(chunks)

    reply1 = await asyncio.to_thread(send_message, "你好")
    print(f"   AI 回复: {reply1[:50]}...")

    reply2 = await asyncio.to_thread(send_message, "你有什么隐藏技能没有")
    print(f"   AI 回复: {reply2[:50]}...")

    count_before = await store.count_messages()
    print(f"\n[2] 清空前消息数: {count_before}")

    # 2. 用户输入 /new
    print("\n[3] 用户输入 /new...")
    count = await store.clear_messages()
    print(f"   删除了 {count} 条消息")

    # 重置 runner 状态
    if runner.handler:
        runner.handler.reset_working_memory()
        print("   ✅ 重置 handler 工作记忆")

    if runner.client and hasattr(runner.client, 'backend'):
        if hasattr(runner.client.backend, 'history'):
            runner.client.backend.history = []
            print("   ✅ 清空 LLM session history")

    # 3. 检查数据库
    count_after = await store.count_messages()
    print(f"\n[4] 清空后消息数: {count_after}")

    # 4. 模拟关闭窗口再打开（重新加载历史）
    print("\n[5] 模拟重新打开窗口...")
    messages = await store.get_messages(limit=20)
    print(f"   加载到 {len(messages)} 条消息")

    if len(messages) == 0:
        print("\n✅ 测试通过！清空成功")
    else:
        print(f"\n❌ 测试失败：还有 {len(messages)} 条消息")
        for msg in messages[:5]:
            print(f"   - {msg.role}: {msg.content[:50]}")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(simulate_user_flow())
