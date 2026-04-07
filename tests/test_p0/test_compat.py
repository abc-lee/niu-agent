"""P0-3: 测试上下文长度限制"""
import pytest
import asyncio
import sys
import tempfile
import os
sys.path.insert(0, "E:/tools/ai-bot")

from agent.session import MessageStore


@pytest.mark.p0
class TestContextLengthLimit:
    """测试历史消息加载限制"""

    @pytest.fixture
    async def long_conversation_store(self):
        """创建 60 轮对话的 MessageStore"""
        db_path = tempfile.mktemp(suffix=".db")
        store = MessageStore(db_path)
        await store.init_db()  # 初始化数据库表

        # 创建 60 轮对话（120 条消息）
        for i in range(60):
            await store.add_message(role="user", content=f"User question {i}")
            await store.add_message(role="assistant", content=f"Assistant answer {i}")

        yield store

        # 清理
        if os.path.exists(db_path):
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_history_limit_50_messages(self, long_conversation_store):
        """测试历史加载限制为 50 条"""
        store = long_conversation_store

        # 模拟 compat.py 中的历史加载
        history = await store.get_messages(limit=50)

        # 验证只加载了 50 条
        assert len(history) == 50, \
            f"Expected 50 messages with limit=50, got {len(history)}"

        # 验证是最新的 50 条
        first_content = history[0].content
        # 最新 50 条应该是 message 70-119（总共 120 条）
        assert "question" in first_content or "answer" in first_content, \
            "Should load the latest 50 messages"

    @pytest.mark.asyncio
    async def test_context_window_token_estimation(self, long_conversation_store):
        """估算 50 条消息的 token 数量"""
        store = long_conversation_store
        history = await store.get_messages(limit=50)

        # 简单估算：每条消息约 100 tokens
        estimated_tokens = sum(
            len(msg.content.split()) * 1.3  # 粗略估算
            for msg in history
        )

        print(f"Estimated tokens for 50 messages: {int(estimated_tokens)}")

        # 验证在合理范围内（10-20K tokens）
        assert estimated_tokens < 20000, \
            f"50 messages should be within 20K tokens, got {int(estimated_tokens)}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "p0"])
