"""Tests for context_manager.py restoring tool messages completely.

TDD for dual-pipeline architecture Phase 3: eliminate the "triple discard" problem.

ContextManager preserves the full dict contract (role/content/tool_calls/tool_call_id);
the compression semantics were retired by the context assembler redesign (2026-08-25).
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
    if os.path.exists(db_path):
        os.unlink(db_path)
    os.rmdir(tmp_dir)


@pytest.fixture
async def ctx_mgr(store):
    """Create a ContextManager instance."""
    from agent.context_manager import ContextManager
    return ContextManager(store)


@pytest.mark.asyncio
async def test_load_history_includes_tool_messages(ctx_mgr, store):
    """load_history should return tool messages and assistant messages with tool_calls."""
    # Store a full sequence: user -> assistant(tool_calls) -> tool -> assistant
    await store.add_message(role="user", content="Please process the photo")
    await store.add_message(
        role="assistant",
        content="",
        tool_calls=[{"id": "call_001", "type": "function", "function": {"name": "ingest_photo", "arguments": "{}"}}]
    )
    await store.add_message(
        role="tool",
        content='{"status": "success"}',
        tool_call_id="call_001"
    )
    await store.add_message(role="assistant", content="Photo processed.")

    history = await ctx_mgr.load_history()

    # Should return 4 messages
    assert len(history) == 4
    assert history[0]["role"] == "user"
    assert history[1]["role"] == "assistant"
    assert "tool_calls" in history[1]
    assert history[1]["tool_calls"][0]["id"] == "call_001"
    assert history[2]["role"] == "tool"
    assert history[2]["tool_call_id"] == "call_001"
    assert history[3]["role"] == "assistant"


@pytest.mark.asyncio
async def test_load_history_empty_assistant_with_tool_calls(ctx_mgr, store):
    """An assistant message with empty content but tool_calls should NOT be filtered."""
    await store.add_message(
        role="assistant",
        content="",
        tool_calls=[{"id": "call_002", "type": "function", "function": {"name": "read", "arguments": "{}"}}]
    )

    history = await ctx_mgr.load_history()

    assert len(history) == 1
    assert history[0]["role"] == "assistant"
    assert "tool_calls" in history[0]


@pytest.mark.asyncio
async def test_load_history_no_completely_empty_messages(ctx_mgr, store):
    """Completely empty messages (no content, no tool_calls, no tool_call_id) should be filtered."""
    await store.add_message(role="user", content="hello")
    await store.add_message(role="assistant", content="")  # completely empty

    history = await ctx_mgr.load_history()

    # The empty assistant message should be filtered
    assert len(history) == 1
    assert history[0]["role"] == "user"


@pytest.mark.asyncio
async def test_load_history_tool_message_with_content(ctx_mgr, store):
    """A tool message with content should be included in history."""
    await store.add_message(
        role="tool",
        content='{"result": "data"}',
        tool_call_id="call_003"
    )

    history = await ctx_mgr.load_history()

    assert len(history) == 1
    assert history[0]["role"] == "tool"
    assert history[0]["content"] == '{"result": "data"}'
    assert history[0]["tool_call_id"] == "call_003"


@pytest.mark.asyncio
async def test_load_history_respects_limit(ctx_mgr, store):
    """load_history should respect the limit parameter."""
    for i in range(10):
        await store.add_message(role="user", content=f"msg {i}")

    history = await ctx_mgr.load_history(limit=5)
    assert len(history) == 5
    # Should be the most recent 5
    assert history[-1]["content"] == "msg 9"

