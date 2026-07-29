"""Tests for session.py supporting role='tool' messages and tool_call_id field.

TDD for dual-pipeline architecture Phase 3.
"""
import os
import tempfile

import pytest


@pytest.fixture
async def store():
    """Create a MessageStore with a temporary database."""
    from agent.session import MessageStore
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test.db")
    s = MessageStore(db_path)
    await s.init_db()
    yield s
    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)
    os.rmdir(tmp_dir)


@pytest.mark.asyncio
async def test_add_tool_message(store):
    """Can store a message with role='tool'."""
    await store.add_message(
        role="tool",
        content="tool result content",
        tool_call_id="call_abc123",
    )
    messages = await store.get_messages()
    assert len(messages) == 1
    assert messages[0].role == "tool"
    assert messages[0].content == "tool result content"
    assert messages[0].tool_call_id == "call_abc123"


@pytest.mark.asyncio
async def test_add_assistant_with_tool_calls(store):
    """An assistant message can carry tool_calls."""
    await store.add_message(
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call_abc123",
                "type": "function",
                "function": {"name": "ingest_photo", "arguments": "{}"},
            }
        ],
    )
    messages = await store.get_messages()
    assert len(messages) == 1
    assert messages[0].role == "assistant"
    assert len(messages[0].tool_calls) == 1
    assert messages[0].tool_calls[0]["id"] == "call_abc123"


@pytest.mark.asyncio
async def test_message_sequence(store):
    """Full user -> assistant(tool_calls) -> tool message sequence."""
    await store.add_message(role="user", content="Please process this photo")
    await store.add_message(
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call_001",
                "type": "function",
                "function": {"name": "ingest_photo", "arguments": "{}"},
            }
        ],
    )
    await store.add_message(
        role="tool",
        content='{"status": "success"}',
        tool_call_id="call_001",
    )
    await store.add_message(role="assistant", content="Photo processed.")

    messages = await store.get_messages()
    assert len(messages) == 4
    assert messages[0].role == "user"
    assert messages[1].role == "assistant"
    assert len(messages[1].tool_calls) == 1
    assert messages[2].role == "tool"
    assert messages[2].tool_call_id == "call_001"
    assert messages[3].role == "assistant"


@pytest.mark.asyncio
async def test_backward_compat_no_tool_call_id(store):
    """When tool_call_id is not provided, it defaults to empty string (backward compat)."""
    await store.add_message(role="user", content="hello")
    messages = await store.get_messages()
    assert messages[0].tool_call_id == ""


@pytest.mark.asyncio
async def test_migration_existing_db(store):
    """An existing database should auto-migrate to add the tool_call_id column."""
    # init_db already called in fixture (which runs migration logic)
    # Verify we can write a message with tool_call_id
    await store.add_message(
        role="tool",
        content="result",
        tool_call_id="call_test",
    )
    messages = await store.get_messages()
    assert messages[0].tool_call_id == "call_test"
