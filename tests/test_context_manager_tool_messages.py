"""Tests for context_manager.py restoring tool messages completely.

TDD for dual-pipeline architecture Phase 3: eliminate the "triple discard" problem.

The current context_manager.py:
1. load_history() filters out messages with empty content (discards assistant(tool_calls))
2. load_history() drops tool_calls and tool_call_id fields (discards tool metadata)
3. compress_messages() does not preserve assistant(tool_calls)+tool pairs
"""
import pytest
import os
import tempfile


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
async def test_compress_preserves_tool_pairs(ctx_mgr, store):
    """When compressing, assistant(tool_calls) + tool messages must be deleted as pairs."""
    # Store 5 rounds of conversation
    for i in range(5):
        await store.add_message(role="user", content=f"Question {i}")
        await store.add_message(
            role="assistant",
            content="",
            tool_calls=[{"id": f"call_{i:03d}", "type": "function", "function": {"name": "tool", "arguments": "{}"}}]
        )
        await store.add_message(
            role="tool",
            content=f"Result {i}",
            tool_call_id=f"call_{i:03d}"
        )
        await store.add_message(role="assistant", content=f"Answer {i}")

    # Total: 20 messages. Compress to 8 (2 rounds).
    compressed = ctx_mgr.compress_messages(await ctx_mgr.load_history())

    # Check that no orphaned tool messages exist (tool without matching assistant(tool_calls))
    tool_call_ids_in_assistant = set()
    for msg in compressed:
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tool_call_ids_in_assistant.add(tc["id"])

    for msg in compressed:
        if msg["role"] == "tool":
            assert msg["tool_call_id"] in tool_call_ids_in_assistant, \
                f"Orphaned tool message: {msg['tool_call_id']} has no matching assistant(tool_calls)"


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


@pytest.mark.asyncio
async def test_compress_does_not_break_tool_call_chain(ctx_mgr, store):
    """After compression, the remaining messages should form a valid tool call chain.

    Every tool message must have a preceding assistant(tool_calls) that references it.
    """
    # Store 3 rounds
    for i in range(3):
        await store.add_message(role="user", content=f"Q{i}")
        await store.add_message(
            role="assistant",
            content="",
            tool_calls=[{"id": f"call_{i:03d}", "type": "function", "function": {"name": "tool", "arguments": "{}"}}]
        )
        await store.add_message(
            role="tool",
            content=f"R{i}",
            tool_call_id=f"call_{i:03d}"
        )
        await store.add_message(role="assistant", content=f"A{i}")

    # Total: 12 messages. Compress to 8 (should drop first round's 4 messages).
    compressed = ctx_mgr.compress_messages(await ctx_mgr.load_history())

    # Verify tool call chain integrity
    for idx, msg in enumerate(compressed):
        if msg["role"] == "tool":
            # There must be a preceding assistant with matching tool_call
            found = False
            for prev in compressed[:idx]:
                if prev["role"] == "assistant" and prev.get("tool_calls"):
                    for tc in prev["tool_calls"]:
                        if tc["id"] == msg["tool_call_id"]:
                            found = True
                            break
                if found:
                    break
            assert found, f"Tool message at index {idx} with tool_call_id={msg['tool_call_id']} has no matching assistant(tool_calls)"
