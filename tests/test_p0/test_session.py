"""P0-2: 测试 MessageStore 排序逻辑"""
import asyncio
import os
import sys
import tempfile

import aiosqlite
import pytest

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


def test_pagination_cursor_uses_rowid(tmp_path):
    """
    Cursor-based pagination must use rowid, not created_at.

    Scenario:
    - Insert 5 messages with mixed created_at values
    - Request limit=2 (gets oldest 2 by rowid, returned in chronological order)
    - Use last message's id as before_id
    - Request next page (should get the next 2 by rowid, not by created_at)
    """
    async def _run():
        db_path = _db_path_factory(tmp_path)
        store = await _init_store(db_path)

        # Insert 5 messages with out-of-order timestamps
        # rowid order: 1,2,3,4,5 — this is the true order
        await _insert_message(db_path, "m1", "user", "First", "2026-05-31T08:00:00")   # rowid=1
        await _insert_message(db_path, "m2", "user", "Second", "2026-05-30T22:00:00")  # rowid=2
        await _insert_message(db_path, "m3", "user", "Third", "2026-05-31T08:01:00")   # rowid=3
        await _insert_message(db_path, "m4", "user", "Fourth", "2026-05-30T23:00:00")  # rowid=4
        await _insert_message(db_path, "m5", "user", "Fifth", "2026-05-31T08:02:00")   # rowid=5

        # First page: limit=2, should get m4, m5 (oldest 2 by rowid ASC after reverse)
        # SQL returns rowid DESC: m5, m4 -> reversed: m4, m5
        page1 = await store.get_messages(limit=2)
        assert len(page1) == 2
        assert page1[0].id == "m4"
        assert page1[1].id == "m5"

        # Second page: before_id=m4's id, rowid<4 returns m3,m2,m1 DESC -> reversed ASC: m1,m2,m3
        # limit=2 -> SQL: m3,m2 DESC -> reversed: m2,m3
        page2 = await store.get_messages(limit=2, before_id="m4")
        assert len(page2) == 2
        assert page2[0].id == "m2"
        assert page2[1].id == "m3"

        # Third page: before_id=m2's id, rowid<2 returns m1 DESC -> reversed: m1
        page3 = await store.get_messages(limit=2, before_id="m2")
        assert len(page3) == 1
        assert page3[0].id == "m1"

    asyncio.run(_run())


def test_rowid_ordering_with_deleted_messages(tmp_path):
    """
    Deleting messages creates rowid gaps but must not break ordering.

    Scenario:
    - Insert 5 messages (rowid 1-5)
    - Delete messages 2 and 4
    - Remaining: rowid 1, 3, 5
    - get_messages() must return them in rowid ASC order: 1, 3, 5
    """
    async def _run():
        db_path = _db_path_factory(tmp_path)
        store = await _init_store(db_path)

        for i in range(1, 6):
            await _insert_message(
                db_path, f"m{i}", "user", f"Message {i}",
                f"2026-05-31T08:00:{i:02d}",
            )

        # Delete m2 and m4
        await store.delete_messages_by_ids(["m2", "m4"])

        messages = await store.get_messages(limit=10)
        assert len(messages) == 3
        # get_messages returns chronological order (rowid ASC): m1, m3, m5
        assert messages[0].id == "m1"
        assert messages[1].id == "m3"
        assert messages[2].id == "m5"

    asyncio.run(_run())


def test_same_second_messages_maintain_write_order(tmp_path):
    """
    Messages with identical created_at must still be ordered by write order (rowid).

    This is the original bug: same-second messages had indeterminate ordering.
    """
    async def _run():
        db_path = _db_path_factory(tmp_path)
        store = await _init_store(db_path)

        # Insert 3 messages with IDENTICAL created_at
        ts = "2026-05-31T08:00:00"
        await _insert_message(db_path, "first", "user", "Written first", ts)
        await _insert_message(db_path, "second", "assistant", "Written second", ts)
        await _insert_message(db_path, "third", "user", "Written third", ts)

        messages = await store.get_messages(limit=10)
        assert len(messages) == 3
        # Must be in write order (rowid ASC): first, second, third
        assert messages[0].id == "first"
        assert messages[1].id == "second"
        assert messages[2].id == "third"

    asyncio.run(_run())


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "p0"])
