"""P1-1: 测试 ContextManager 统一历史管理"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, "E:/tools/ai-bot")

from agent.context_manager import ContextManager, get_context_manager, reset_context_manager
from agent.session import MessageStore


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
        """测试使用默认限制加载历史（默认0=不限制，返回全部消息）"""
        store = store_with_messages
        manager = ContextManager(store)  # max_messages 默认 0 = 不限制

        history = await manager.load_history()

        # 验证返回全部消息（fixture 创建了 60 条）
        assert len(history) == 60, f"Expected 60 messages (all), got {len(history)}"

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
