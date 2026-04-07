"""P0-2: 测试 MessageStore 排序逻辑"""
import pytest
import asyncio
import sys
import tempfile
import os
sys.path.insert(0, "E:/tools/ai-bot")

from agent.session import MessageStore


@pytest.mark.p0
class TestMessageStoreSorting:
    """测试 MessageStore 消息排序"""

    @pytest.fixture
    async def store(self):
        """创建测试 MessageStore"""
        db_path = tempfile.mktemp(suffix=".db")
        store = MessageStore(db_path)
        await store.init_db()  # 初始化数据库表

        # 创建 10 条消息（按顺序）
        for i in range(10):
            await store.add_message(
                role="user" if i % 2 == 0 else "assistant",
                content=f"Message {i}"
            )

        yield store

        # 清理
        if os.path.exists(db_path):
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_get_messages_limit_returns_latest(self, store):
        """测试 limit 参数返回最新 N 条消息"""
        # 获取最近 5 条消息
        messages = await store.get_messages(limit=5)

        # 验证返回了 5 条
        assert len(messages) == 5, f"Expected 5 messages, got {len(messages)}"

        # 验证是最新的 5 条（message 5-9）
        expected_contents = [f"Message {i}" for i in range(5, 10)]
        actual_contents = [msg.content for msg in messages]

        assert actual_contents == expected_contents, \
            f"Expected latest messages {expected_contents}, got {actual_contents}"

    @pytest.mark.asyncio
    async def test_get_messages_returns_chronological_order(self, store):
        """测试返回的消息按时间顺序（最旧在上）"""
        messages = await store.get_messages(limit=5)

        # 验证按时间顺序排列（created_at 递增）
        for i in range(len(messages) - 1):
            assert messages[i].created_at <= messages[i + 1].created_at, \
                "Messages should be in chronological order"

    @pytest.mark.asyncio
    async def test_get_messages_no_limit_returns_all(self, store):
        """测试无 limit 参数返回所有消息"""
        messages = await store.get_messages(limit=None)

        assert len(messages) == 10, f"Expected 10 messages, got {len(messages)}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "p0"])
