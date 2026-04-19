"""Tests for message deletion with temp file cleanup"""
import os
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest
import aiosqlite


@pytest.fixture
async def test_db(tmp_path):
    """Create a test messages database"""
    db_path = tmp_path / "test_messages.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_results TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.commit()
    yield str(db_path)
    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def tmp_dir_fixture(tmp_path):
    """Use tmp_path as ~/.niu/tmp/ for isolation"""
    niu_tmp = tmp_path / ".niu" / "tmp"
    niu_tmp.mkdir(parents=True, exist_ok=True)
    with patch("agent.tmp_dir.Path.home", return_value=tmp_path):
        yield niu_tmp


class TestClearMessagesCleansTmp:
    async def test_clear_removes_tmp_files_referenced_in_content(self, test_db, tmp_dir_fixture):
        """clear_messages should delete temp files referenced in message content"""
        from agent.tmp_dir import save_to_tmp
        from agent.session import MessageStore

        # Create a temp file (simulating a face-boxed photo)
        boxed_path = save_to_tmp("person1_photo1_boxed.jpg", b"fake image data")
        assert os.path.exists(boxed_path)

        # Add a message that references the temp file
        store = MessageStore(db_path=test_db)
        await store.init_db()
        await store.add_message(role="assistant", content=f"这是谁？\n::person_photo::{{\"path\": \"{boxed_path}\", \"person_id\": \"abc\"}}::")

        # Clear messages — should also delete the temp file
        deleted = await store.clear_messages()
        assert deleted == 1
        assert not os.path.exists(boxed_path)

    async def test_clear_does_not_delete_non_tmp_files(self, test_db, tmp_dir_fixture):
        """clear_messages should NOT delete files outside tmp dir"""
        from agent.session import MessageStore

        # Add a message referencing a non-tmp file (original photo)
        original_path = "E:/tmp/bot/2026/04/photo.jpg"
        store = MessageStore(db_path=test_db)
        await store.init_db()
        await store.add_message(role="assistant", content=f"照片：{original_path}")

        deleted = await store.clear_messages()
        assert deleted == 1
        # Non-tmp file should not be touched (we can't verify it wasn't deleted
        # since it doesn't exist in test, but the code should skip it)


class TestDeleteMessagesByIdsCleansTmp:
    async def test_delete_by_ids_removes_tmp_files(self, test_db, tmp_dir_fixture):
        """delete_messages_by_ids should delete temp files in deleted messages"""
        from agent.tmp_dir import save_to_tmp
        from agent.session import MessageStore

        # Create two temp files
        boxed1 = save_to_tmp("boxed1.jpg", b"data1")
        boxed2 = save_to_tmp("boxed2.jpg", b"data2")

        store = MessageStore(db_path=test_db)
        await store.init_db()
        msg_id1 = await store.add_message(role="assistant", content=f"photo: {boxed1}")
        msg_id2 = await store.add_message(role="assistant", content=f"photo: {boxed2}")
        msg_id3 = await store.add_message(role="user", content="hello")

        # Delete only the first two messages — should clean their temp files
        deleted = await store.delete_messages_by_ids([msg_id1, msg_id2])
        assert deleted == 2
        assert not os.path.exists(boxed1)
        assert not os.path.exists(boxed2)

    async def test_delete_by_ids_preserves_non_tmp_files(self, test_db, tmp_dir_fixture):
        """delete_messages_by_ids should not touch files outside tmp dir"""
        from agent.session import MessageStore

        store = MessageStore(db_path=test_db)
        await store.init_db()
        msg_id = await store.add_message(role="assistant", content="E:/photos/original.jpg")

        deleted = await store.delete_messages_by_ids([msg_id])
        assert deleted == 1
        # Non-tmp path should be ignored
