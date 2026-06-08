"""
Tests for DreamWriter — Dream Evolver's brain graph write layer

Validates dual-pipeline memory writes via lightrag_insert:
- Pipeline A: Semantic memory (entities + associative relations)
- Pipeline B: Episodic memory (events + time chains)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent.injector.dream_writer import (
    CHAIN_RELATION_CORRECTED,
    CHAIN_RELATION_FOLLOWED,
    EPISODIC_ENTITY_TYPE,
    EVENT_PREFIX,
    INVOLVES_RELATION,
    DreamWriter,
)


# ============== Fixtures ==============


@pytest.fixture
def mock_ingester() -> MagicMock:
    """Create a mock LightRAGIngester."""
    ingester = MagicMock()
    ingester.lightrag_insert.return_value = {"status": "ok", "track_id": "track-001"}
    return ingester


@pytest.fixture
def writer(mock_ingester: MagicMock) -> DreamWriter:
    """Create a DreamWriter with mock ingester."""
    return DreamWriter(mock_ingester)


# ============== Test 1: write_semantic_entity ==============


def test_write_semantic_entity(writer: DreamWriter, mock_ingester: MagicMock) -> None:
    """Verify semantic entity text (no niu anchor) passed to lightrag_insert."""
    result = writer.write_semantic_entity(
        name="Python",
        entity_type="Skill",
        description="Programming language",
    )

    # Check return value from lightrag_insert
    assert result["status"] == "ok"
    assert result["track_id"] == "track-001"

    # Verify lightrag_insert called with correct structured text
    mock_ingester.lightrag_insert.assert_called_once()
    call_kwargs = mock_ingester.lightrag_insert.call_args
    text = call_kwargs.kwargs["content"]

    # Text should contain entity name, type, description
    assert "Python" in text
    assert "Skill" in text
    assert "Programming language" in text
    assert "brain:Niu" not in text
    assert "skilled_in" not in text


def test_write_semantic_entity_default_relation(
    writer: DreamWriter, mock_ingester: MagicMock
) -> None:
    """Verify semantic entity text without niu anchor for unknown types."""
    writer.write_semantic_entity(
        name="Alice",
        entity_type="UnknownType",
        description="A person",
    )

    call_kwargs = mock_ingester.lightrag_insert.call_args
    text = call_kwargs.kwargs["content"]
    assert "语义记忆" in text
    assert "Alice" in text
    assert "UnknownType" in text
    assert "brain:Niu" not in text


# ============== Test 2: write_semantic_relation ==============


def test_write_semantic_relation(writer: DreamWriter, mock_ingester: MagicMock) -> None:
    """Verify structured text with relation passed to lightrag_insert."""
    result = writer.write_semantic_relation(
        src_name="Python",
        tgt_name="数据分析",
        relation="USED_FOR",
        description="Python for data analysis",
    )

    assert result["status"] == "ok"

    mock_ingester.lightrag_insert.assert_called_once()
    call_kwargs = mock_ingester.lightrag_insert.call_args
    text = call_kwargs.kwargs["content"]

    assert "Python" in text
    assert "数据分析" in text
    assert "USED_FOR" in text
    assert "Python for data analysis" in text


def test_write_semantic_relation_no_description(
    writer: DreamWriter, mock_ingester: MagicMock
) -> None:
    """Verify relation text without optional description."""
    writer.write_semantic_relation(
        src_name="Python",
        tgt_name="Django",
        relation="USED_FOR",
    )

    call_kwargs = mock_ingester.lightrag_insert.call_args
    text = call_kwargs.kwargs["content"]

    assert "语义关系: Python —[USED_FOR]→ Django。" == text


# ============== Test 3: write_episodic_event ==============


def test_write_episodic_event(writer: DreamWriter, mock_ingester: MagicMock) -> None:
    """Verify structured text with event passed to lightrag_insert."""
    result = writer.write_episodic_event(
        event_name="tool_x_failed",
        description="Tool X returned error code 500",
        experience_type="error",
        session_id="sess-001",
    )

    assert result["status"] == "ok"

    mock_ingester.lightrag_insert.assert_called_once()
    call_kwargs = mock_ingester.lightrag_insert.call_args
    text = call_kwargs.kwargs["content"]

    # Text should contain event name, type, description
    assert "tool_x_failed" in text
    assert "error" in text
    assert "Tool X returned error code 500" in text
    assert "brain:Niu experienced" not in text
    # No chain or involves relations
    assert "followed_by" not in text
    assert "corrected_by" not in text
    assert "involves" not in text
    # session_id should be included in text (M1 fix)
    assert "来自会话 sess-001" in text


# ============== Test 4: write_episodic_event_with_chain ==============


def test_write_episodic_event_with_chain(
    writer: DreamWriter, mock_ingester: MagicMock
) -> None:
    """Verify followed_by/corrected_by chain in structured text."""
    # Test followed_by chain
    result_followed = writer.write_episodic_event(
        event_name="tried_tool_y",
        description="Tried tool Y",
        experience_type="success",
        prev_event_name="tried_tool_x",
        is_correction=False,
    )

    assert result_followed["status"] == "ok"

    call_kwargs = mock_ingester.lightrag_insert.call_args
    text = call_kwargs.kwargs["content"]

    assert "tried_tool_x" in text
    assert "tried_tool_y" in text
    assert CHAIN_RELATION_FOLLOWED in text

    # Reset for correction test
    mock_ingester.reset_mock()
    mock_ingester.lightrag_insert.return_value = {"status": "ok", "track_id": "track-002"}

    # Test corrected_by chain
    result_corrected = writer.write_episodic_event(
        event_name="used_tool_z",
        description="Used tool Z successfully",
        experience_type="success",
        prev_event_name="tried_tool_y",
        is_correction=True,
    )

    assert result_corrected["status"] == "ok"

    call_kwargs = mock_ingester.lightrag_insert.call_args
    text = call_kwargs.kwargs["content"]

    assert "tried_tool_y" in text
    assert "used_tool_z" in text
    assert CHAIN_RELATION_CORRECTED in text


# ============== Test 5: write_episodic_event_with_involves ==============


def test_write_episodic_event_with_involves(
    writer: DreamWriter, mock_ingester: MagicMock
) -> None:
    """Verify involves relations included in structured text."""
    result = writer.write_episodic_event(
        event_name="data_analysis_session",
        description="Analyzed sales data",
        experience_type="success",
        related_entities=["Python", "pandas"],
    )

    assert result["status"] == "ok"

    call_kwargs = mock_ingester.lightrag_insert.call_args
    text = call_kwargs.kwargs["content"]

    assert "data_analysis_session" in text
    assert "involves" in text
    assert "Python" in text
    assert "pandas" in text


# ============== Test 7: error handling ==============


def test_write_semantic_entity_error(mock_ingester: MagicMock) -> None:
    """Verify error handling when lightrag_insert raises exception."""
    mock_ingester.lightrag_insert.side_effect = RuntimeError("insert failed")
    writer = DreamWriter(mock_ingester)

    result = writer.write_semantic_entity(
        name="Python",
        entity_type="Skill",
        description="Programming language",
    )

    assert result["status"] == "error"
    assert "insert failed" in result["message"]


def test_write_episodic_event_error(mock_ingester: MagicMock) -> None:
    """Verify error handling when lightrag_insert raises exception."""
    mock_ingester.lightrag_insert.side_effect = RuntimeError("insert failed")
    writer = DreamWriter(mock_ingester)

    result = writer.write_episodic_event(
        event_name="test_event",
        description="Test description",
        experience_type="error",
    )

    assert result["status"] == "error"
    assert "insert failed" in result["message"]


def test_write_semantic_relation_error(mock_ingester: MagicMock) -> None:
    """Verify error handling when lightrag_insert raises exception."""
    mock_ingester.lightrag_insert.side_effect = RuntimeError("insert failed")
    writer = DreamWriter(mock_ingester)

    result = writer.write_semantic_relation(
        src_name="A",
        tgt_name="B",
        relation="USED_FOR",
    )

    assert result["status"] == "error"
    assert "insert failed" in result["message"]


# ============== Test 8: lightrag_insert returns error dict (H1) ==============


def test_write_semantic_entity_insert_returns_error(mock_ingester: MagicMock) -> None:
    """Verify warning logged and early return when lightrag_insert returns non-ok dict."""
    mock_ingester.lightrag_insert.return_value = {"status": "error", "message": "duplicate"}
    writer = DreamWriter(mock_ingester)

    result = writer.write_semantic_entity(
        name="Python",
        entity_type="Skill",
        description="Programming language",
    )

    assert result["status"] == "error"
    assert result["message"] == "duplicate"


def test_write_semantic_relation_insert_returns_error(mock_ingester: MagicMock) -> None:
    """Verify warning logged and early return when lightrag_insert returns non-ok dict."""
    mock_ingester.lightrag_insert.return_value = {"status": "error", "message": "duplicate"}
    writer = DreamWriter(mock_ingester)

    result = writer.write_semantic_relation(
        src_name="A",
        tgt_name="B",
        relation="USED_FOR",
    )

    assert result["status"] == "error"
    assert result["message"] == "duplicate"


def test_write_episodic_event_insert_returns_error(mock_ingester: MagicMock) -> None:
    """Verify warning logged and early return when lightrag_insert returns non-ok dict."""
    mock_ingester.lightrag_insert.return_value = {"status": "error", "message": "duplicate"}
    writer = DreamWriter(mock_ingester)

    result = writer.write_episodic_event(
        event_name="test_event",
        description="Test",
        experience_type="error",
    )

    assert result["status"] == "error"
    assert result["message"] == "duplicate"


# ============== Test 9: experience_type validation (M2) ==============


def test_write_episodic_event_invalid_experience_type(mock_ingester: MagicMock) -> None:
    """Verify invalid experience_type is rejected before calling lightrag_insert."""
    writer = DreamWriter(mock_ingester)

    result = writer.write_episodic_event(
        event_name="bad_event",
        description="Bad type",
        experience_type="warning",
    )

    assert result["status"] == "error"
    assert "warning" in result["message"]
    assert "error" in result["message"] or "success" in result["message"]
    # lightrag_insert should NOT have been called
    mock_ingester.lightrag_insert.assert_not_called()


# ============== Test 10: session_id omitted ==============


def test_write_episodic_event_no_session_id(writer: DreamWriter, mock_ingester: MagicMock) -> None:
    """Verify no session clause when session_id is None."""
    result = writer.write_episodic_event(
        event_name="test_event",
        description="No session",
        experience_type="success",
    )

    assert result["status"] == "ok"

    call_kwargs = mock_ingester.lightrag_insert.call_args
    text = call_kwargs.kwargs["content"]
    assert "来自会话" not in text
