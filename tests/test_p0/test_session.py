"""P0-2: 测试 MessageStore 排序逻辑"""
import pytest
import asyncio
import aiosqlite
import sys
import tempfile
import os
sys.path.insert(0, "E:/tools/ai-bot")

from agent.session import MessageStore


def _db_path_factory(tmp_path):
    """Create a temporary database path."""
    return str(tmp_path / "test_messages.db")


async def _init_store(db_path: str):
    """Create a MessageStore with real schema."""
    store = MessageStore(db_path)
    await store.init_db()
    return store


async def _insert_message(db_path: str, msg_id: str, role: str, content: str, created_at: str):
    """Insert a message directly into the database (to control created_at independently of rowid)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO messages (id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (msg_id, role, content, created_at),
        )
        await db.commit()


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


def test_message_has_rowid_field():
    """Message dataclass must have a rowid field with default 0."""
    from agent.session import Message

    msg = Message(
        id="test-id",
        role="user",
        content="hello",
        created_at="2026-01-01T00:00:00",
    )
    assert hasattr(msg, "rowid")
    assert msg.rowid == 0

    msg_with_rowid = Message(
        id="test-id",
        role="user",
        content="hello",
        created_at="2026-01-01T00:00:00",
        rowid=42,
    )
    assert msg_with_rowid.rowid == 42


def test_messages_ordered_by_rowid_not_created_at(tmp_path):
    """
    Messages must be returned in rowid order (write order), not created_at order.

    Scenario: Insert messages with out-of-order created_at values.
    - Insert A at 2026-05-31T08:00 (rowid=1)
    - Insert B at 2026-05-30T22:00 (rowid=2, created_at EARLIER than A)
    - Insert C at 2026-05-31T08:01 (rowid=3)

    If sorted by created_at ASC: B, A, C (wrong — B's timestamp is earliest)
    If sorted by rowid ASC: A, B, C (correct — matches write order)
    """
    async def _run():
        db_path = _db_path_factory(tmp_path)
        store = await _init_store(db_path)

        # Write A first (earlier timestamp today)
        await _insert_message(db_path, "msg-a", "user", "Message A", "2026-05-31T08:00:00")
        # Write B second (LATER rowid, but EARLIER created_at — yesterday)
        await _insert_message(db_path, "msg-b", "user", "Message B", "2026-05-30T22:00:00")
        # Write C third (latest of both)
        await _insert_message(db_path, "msg-c", "user", "Message C", "2026-05-31T08:01:00")

        messages = await store.get_messages(limit=10)

        # Must return in chronological order (rowid ASC): A, B, C
        # rowid=1 (A), rowid=2 (B), rowid=3 (C)
        # Even though B's created_at is earlier than A's, B was written after A,
        # so B comes after A in write order.
        assert len(messages) == 3
        assert messages[0].id == "msg-a"  # rowid 1 (written first)
        assert messages[1].id == "msg-b"  # rowid 2 (written second, despite earlier created_at)
        assert messages[2].id == "msg-c"  # rowid 3 (written third)

    asyncio.run(_run())


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "p0"])
