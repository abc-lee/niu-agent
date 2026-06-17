"""P1-1: 测试 ContextManager 统一历史管理"""
import pytest
import asyncio
import sys
import tempfile
import os
sys.path.insert(0, "E:/tools/ai-bot")

from agent.session import MessageStore
from agent.context_manager import ContextManager, get_context_manager, reset_context_manager


@pytest.mark.p1
class TestContextManager:
    """测试 ContextManager 功能"""

    @pytest.fixture
    async def store_with_messages(self):
        """创建包含消息的 MessageStore"""
        db_path = tempfile.mktemp(suffix=".db")
        store = MessageStore(db_path)
        await store.init_db()

        # 创建 60 条消息
        for i in range(60):
            await store.add_message(
                role="user" if i % 2 == 0 else "assistant",
                content=f"Message {i} with some content to test token counting"
            )

        yield store

        # 清理
        if os.path.exists(db_path):
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_load_history_with_limit(self, store_with_messages):
        """测试加载限制数量的历史"""
        store = store_with_messages
        manager = ContextManager(store, max_messages=30)

        history = await manager.load_history(limit=30)

        # 验证加载了 30 条
        assert len(history) == 30, f"Expected 30 messages, got {len(history)}"

        # 验证是最新的 30 条
        assert "Message 59" in history[-1]["content"]

    @pytest.mark.asyncio
    async def test_load_history_default_limit(self, store_with_messages):
        """测试使用默认限制加载历史"""
        store = store_with_messages
        manager = ContextManager(store, max_messages=50)

        history = await manager.load_history()

        # 验证加载了 50 条（默认）
        assert len(history) == 50, f"Expected 50 messages, got {len(history)}"

    def test_count_tokens_simple(self):
        """测试简单 token 计数"""
        manager = ContextManager(None)

        messages = [
            {"role": "user", "content": "Hello world"},
            {"role": "assistant", "content": "Hi there"}
        ]

        tokens = manager.count_tokens_simple(messages)

        # 验证 token 数量合理（粗略估算）
        assert tokens > 0, "Tokens should be positive"
        assert tokens < 100, "Simple messages should have less than 100 tokens"

    def test_should_compress_by_message_count(self):
        """测试 should_compress 已禁用：压缩只在 agent_loop 工具循环中同步触发"""
        manager = ContextManager(None, max_messages=50)

        # should_compress 已禁用，无论消息数量多少都返回 False
        messages = [{"role": "user", "content": f"Message {i}"} for i in range(40)]
        assert not manager.should_compress(messages), "should_compress is disabled, should return False"

        messages = [{"role": "user", "content": f"Message {i}"} for i in range(60)]
        assert not manager.should_compress(messages), "should_compress is disabled, should return False"

    def test_compress_messages(self):
        """测试消息压缩"""
        manager = ContextManager(None, max_messages=50)

        # 创建 50 条消息
        messages = [{"role": "user", "content": f"Message {i}"} for i in range(50)]

        compressed = manager.compress_messages(messages)

        # 验证压缩后保留 80%
        assert len(compressed) == int(50 * 0.8) + 1, "Should keep 80% plus compression note"  # +1 是压缩说明

        # 验证包含压缩说明
        assert "压缩" in compressed[0]["content"]

        # 验证保留的是最近的消息
        assert "Message 49" in compressed[-1]["content"]

    def test_estimate_context_usage(self):
        """测试上下文使用情况估算"""
        manager = ContextManager(None, max_messages=50, max_tokens=100000)

        messages = [{"role": "user", "content": f"Test message {i}"} for i in range(30)]

        usage = manager.estimate_context_usage(messages)

        # 验证返回值
        assert "message_count" in usage
        assert "estimated_tokens" in usage
        assert "usage_percentage" in usage
        assert "should_compress" in usage

        assert usage["message_count"] == 30
        assert usage["estimated_tokens"] > 0
        assert 0 <= usage["usage_percentage"] <= 100

    @pytest.mark.asyncio
    async def test_get_context_for_chat(self, store_with_messages):
        """测试获取聊天上下文（主入口）"""
        store = store_with_messages
        manager = ContextManager(store, max_messages=30)

        context = await manager.get_context_for_chat(exclude_last=True)

        # 验证返回历史消息
        assert len(context) > 0
        assert isinstance(context, list)
        assert all("role" in msg and "content" in msg for msg in context)


@pytest.mark.p1
class TestContextManagerGlobal:
    """测试全局 ContextManager 实例"""

    @pytest.mark.asyncio
    async def test_get_context_manager_singleton(self):
        """测试全局实例是单例"""
        reset_context_manager()

        db_path = tempfile.mktemp(suffix=".db")
        store = MessageStore(db_path)
        await store.init_db()

        manager1 = await get_context_manager(store)
        manager2 = await get_context_manager()

        # 验证是同一个实例
        assert manager1 is manager2

        # 清理
        reset_context_manager()
        if os.path.exists(db_path):
            os.unlink(db_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "p1"])
