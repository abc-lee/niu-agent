"""Tests for compat.py dual-pipeline persistence.

TDD for dual-pipeline architecture Phase 4 — compat.py endpoint.

Key concepts:
- SSE pipeline: only push reply content to frontend (already done in runner.py)
- DB pipeline: extract complete messages (including tool_calls + tool results)
  from agent_runner_loop's return value and persist to database
"""


import pytest


def _make_return_value(messages):
    """Build a return_value dict with messages, simulating agent_runner_loop output."""
    return {"result": "CURRENT_TASK_DONE", "data": None, "messages": messages}


def _make_tool_messages_sequence():
    """Build a typical message sequence: user -> assistant(tool_calls) -> tool -> assistant(reply)."""
    return [
        {"role": "user", "content": "Please process this photo"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_001",
                    "type": "function",
                    "function": {"name": "ingest_photo", "arguments": '{"path": "/tmp/test.jpg"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_001",
            "content": '{"status": "success", "faces": 2}',
        },
        {"role": "assistant", "content": "I found 2 faces in the photo."},
    ]


@pytest.mark.asyncio
async def test_compat_persists_tool_messages():
    """compat.py /api/chat/session should persist tool messages to database.

    When runner.chat() completes, the return_value contains the full messages
    list including tool_calls and tool results. compat.py must iterate these
    and persist each one to the message store.
    """
    import os
    import tempfile

    from agent.session import MessageStore

    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test.db")
    store = MessageStore(db_path)
    await store.init_db()

    # Simulate what runner.last_return_value would contain
    messages = _make_tool_messages_sequence()
    return_value = _make_return_value(messages)

    # We need to test that compat.py's chat_session endpoint
    # persists all messages from return_value (not just user + assistant)
    # Import the persistence helper
    from niu_api.compat import _persist_messages_from_return_value

    # Persist all messages (skip user which was already persisted by the endpoint)
    await _persist_messages_from_return_value(
        store, return_value
    )

    # Verify all messages were persisted (user skipped because
    # _persist_messages_from_return_value now always skips user messages)
    db_messages = await store.get_messages()
    assert len(db_messages) == 3

    # Check assistant with tool_calls
    assistant_msg = db_messages[0]
    assert assistant_msg.role == "assistant"
    assert len(assistant_msg.tool_calls) == 1
    assert assistant_msg.tool_calls[0]["id"] == "call_001"

    # Check tool message
    tool_msg = db_messages[1]
    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == "call_001"
    assert "success" in tool_msg.content

    # Check final assistant reply
    final_msg = db_messages[2]
    assert final_msg.role == "assistant"
    assert "2 faces" in final_msg.content

    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)
    os.rmdir(tmp_dir)


@pytest.mark.asyncio
async def test_compat_persist_skips_user_messages():
    """User messages are always skipped (already persisted by the endpoint)."""
    import os
    import tempfile

    from agent.session import MessageStore

    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test.db")
    store = MessageStore(db_path)
    await store.init_db()

    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    return_value = _make_return_value(messages)

    from niu_api.compat import _persist_messages_from_return_value

    await _persist_messages_from_return_value(
        store, return_value
    )

    db_messages = await store.get_messages()
    # Only assistant message should be persisted (user always skipped)
    assert len(db_messages) == 1
    assert db_messages[0].role == "assistant"

    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)
    os.rmdir(tmp_dir)


@pytest.mark.asyncio
async def test_compat_persist_no_messages_in_return_value():
    """When return_value has no messages key, persistence should be a no-op."""
    import os
    import tempfile

    from agent.session import MessageStore

    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test.db")
    store = MessageStore(db_path)
    await store.init_db()

    # No messages key
    return_value = {"result": "CURRENT_TASK_DONE", "data": None}

    from niu_api.compat import _persist_messages_from_return_value

    await _persist_messages_from_return_value(
        store, return_value
    )

    db_messages = await store.get_messages()
    assert len(db_messages) == 0

    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)
    os.rmdir(tmp_dir)


@pytest.mark.asyncio
async def test_compat_persist_context_overflow():
    """When return_value is CONTEXT_OVERFLOW, messages should still be persisted."""
    import os
    import tempfile

    from agent.session import MessageStore

    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test.db")
    store = MessageStore(db_path)
    await store.init_db()

    messages = _make_tool_messages_sequence()
    return_value = {
        "result": "CONTEXT_OVERFLOW",
        "data": {"overflow": True, "tokens_used": 170000},
        "messages": messages,
    }

    from niu_api.compat import _persist_messages_from_return_value

    await _persist_messages_from_return_value(
        store, return_value
    )

    db_messages = await store.get_messages()
    # user skipped (skip_user=True), only 3 persisted
    assert len(db_messages) == 3

    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)
    os.rmdir(tmp_dir)


@pytest.mark.asyncio
async def test_compat_persist_multiple_tool_calls():
    """Multiple tool_calls in a single assistant message should all be persisted."""
    import os
    import tempfile

    from agent.session import MessageStore

    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test.db")
    store = MessageStore(db_path)
    await store.init_db()

    messages = [
        {"role": "user", "content": "Analyze this"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_001",
                    "type": "function",
                    "function": {"name": "tool_a", "arguments": "{}"},
                },
                {
                    "id": "call_002",
                    "type": "function",
                    "function": {"name": "tool_b", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_001", "content": "result_a"},
        {"role": "tool", "tool_call_id": "call_002", "content": "result_b"},
        {"role": "assistant", "content": "Done"},
    ]
    return_value = _make_return_value(messages)

    from niu_api.compat import _persist_messages_from_return_value

    await _persist_messages_from_return_value(
        store, return_value
    )

    db_messages = await store.get_messages()
    # user skipped (skip_user=True), only 4 messages persisted
    assert len(db_messages) == 4

    # Assistant with 2 tool_calls
    assert len(db_messages[0].tool_calls) == 2
    # Both tool messages
    assert db_messages[1].role == "tool"
    assert db_messages[1].tool_call_id == "call_001"
    assert db_messages[2].role == "tool"
    assert db_messages[2].tool_call_id == "call_002"

    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)
    os.rmdir(tmp_dir)


@pytest.mark.asyncio
async def test_compat_persist_skips_system_messages():
    """System messages from agent_runner_loop should not be persisted to DB."""
    import os
    import tempfile

    from agent.session import MessageStore

    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test.db")
    store = MessageStore(db_path)
    await store.init_db()

    messages = [
        {"role": "system", "content": "You are a helpful assistant"},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]
    return_value = _make_return_value(messages)

    from niu_api.compat import _persist_messages_from_return_value

    await _persist_messages_from_return_value(
        store, return_value
    )

    db_messages = await store.get_messages()
    # Only assistant (system and user skipped)
    assert len(db_messages) == 1
    assert db_messages[0].role == "assistant"

    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)
    os.rmdir(tmp_dir)
