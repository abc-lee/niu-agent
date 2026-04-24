"""
Tests for DreamWriter — Dream Evolver's brain graph write layer

Validates dual-pipeline memory writes:
- Pipeline A: Semantic memory (entities + associative relations)
- Pipeline B: Episodic memory (events + time chains)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent.injector.dream_writer import (
    CHAIN_RELATION_CORRECTED,
    CHAIN_RELATION_FOLLOWED,
    DREAM_FILE_PATH,
    DREAM_SOURCE_ID,
    EPISODIC_ENTITY_TYPE,
    EVENT_PREFIX,
    NIU_ENTITY,
    DreamWriter,
)


# ============== Fixtures ==============


@pytest.fixture
def mock_ingester() -> MagicMock:
    """Create a mock LightRAGIngester."""
    ingester = MagicMock()
    ingester.inject_entity.return_value = {"status": "ok", "name": "test"}
    ingester.inject_custom_kg.return_value = {"status": "ok"}
    return ingester


@pytest.fixture
def writer(mock_ingester: MagicMock) -> DreamWriter:
    """Create a DreamWriter with mock ingester."""
    return DreamWriter(mock_ingester)


# ============== Test 1: write_semantic_entity ==============


def test_write_semantic_entity(writer: DreamWriter, mock_ingester: MagicMock) -> None:
    """Verify entity + brain:Niu relation created."""
    result = writer.write_semantic_entity(
        name="Python",
        entity_type="Skill",
        description="Programming language",
        level="L0",
    )

    # Check status
    assert result["status"] == "ok"
    assert result["name"] == "Python"
    assert result["entity_type"] == "Skill"
    assert result["niu_relation_keyword"] == "skilled_in"

    # Verify inject_entity called with correct params
    mock_ingester.inject_entity.assert_called_once_with(
        name="Python",
        entity_type="Skill",
        description="Programming language",
        source_id=DREAM_SOURCE_ID,
        chunk_content="Programming language",
        file_path=DREAM_FILE_PATH,
    )

    # Verify inject_custom_kg called for brain:Niu relation
    mock_ingester.inject_custom_kg.assert_called_once()
    kg_call = mock_ingester.inject_custom_kg.call_args
    rel = kg_call.kwargs["relationships"][0]
    assert rel["src_id"] == NIU_ENTITY
    assert rel["tgt_id"] == "Python"
    assert rel["keywords"] == "skilled_in"


# ============== Test 2: write_semantic_relation ==============


def test_write_semantic_relation(writer: DreamWriter, mock_ingester: MagicMock) -> None:
    """Verify relation created."""
    result = writer.write_semantic_relation(
        src="Python",
        tgt="数据分析",
        relation_type="USED_FOR",
        description="Python for data analysis",
    )

    # Verify inject_custom_kg called with correct relationship
    mock_ingester.inject_custom_kg.assert_called_once()
    kg_call = mock_ingester.inject_custom_kg.call_args
    rel = kg_call.kwargs["relationships"][0]
    assert rel["src_id"] == "Python"
    assert rel["tgt_id"] == "数据分析"
    assert rel["keywords"] == "USED_FOR"
    assert rel["description"] == "Python for data analysis"


# ============== Test 3: write_episodic_event ==============


def test_write_episodic_event(writer: DreamWriter, mock_ingester: MagicMock) -> None:
    """Verify event entity created with metadata."""
    result = writer.write_episodic_event(
        event_name="tool_x_failed",
        description="Tool X returned error code 500",
        experience_type="error",
        level="L1",
        session_id="sess-001",
    )

    # Check status
    assert result["status"] == "ok"
    assert result["event_name"] == f"{EVENT_PREFIX}tool_x_failed"
    assert result["experience_type"] == "error"

    # Verify inject_entity called with brain:event: prefix and metadata in description
    mock_ingester.inject_entity.assert_called_once()
    entity_call = mock_ingester.inject_entity.call_args
    assert entity_call.kwargs["name"] == f"{EVENT_PREFIX}tool_x_failed"
    assert entity_call.kwargs["entity_type"] == EPISODIC_ENTITY_TYPE
    # Description should contain brain_meta fields
    desc = entity_call.kwargs["description"]
    assert "brain_meta_experience_type:error" in desc
    assert "brain_meta_level:L1" in desc
    assert "brain_meta_session_id:sess-001" in desc
    assert "Tool X returned error code 500" in desc

    # No time chain or involves relations expected
    assert result["chain"] is None
    assert result["involves"] == []


# ============== Test 4: write_episodic_event_with_chain ==============


def test_write_episodic_event_with_chain(
    writer: DreamWriter, mock_ingester: MagicMock
) -> None:
    """Verify followed_by/corrected_by chain."""
    # Test followed_by chain
    result_followed = writer.write_episodic_event(
        event_name="tried_tool_y",
        description="Tried tool Y",
        experience_type="success",
        prev_event_name="tried_tool_x",
        is_correction=False,
    )

    # Check chain relation
    assert result_followed["chain"] is not None
    # inject_custom_kg should have been called for the chain
    kg_calls = mock_ingester.inject_custom_kg.call_args_list
    # Find the chain call (not the entity call — entity call has empty relationships)
    chain_call = None
    for c in kg_calls:
        rels = c.kwargs.get("relationships", [])
        if rels and rels[0]["keywords"] in (
            CHAIN_RELATION_FOLLOWED,
            CHAIN_RELATION_CORRECTED,
        ):
            chain_call = rels[0]
            break

    assert chain_call is not None
    assert chain_call["src_id"] == f"{EVENT_PREFIX}tried_tool_x"
    assert chain_call["tgt_id"] == f"{EVENT_PREFIX}tried_tool_y"
    assert chain_call["keywords"] == CHAIN_RELATION_FOLLOWED

    # Reset for correction test
    mock_ingester.reset_mock()
    mock_ingester.inject_entity.return_value = {"status": "ok", "name": "test"}
    mock_ingester.inject_custom_kg.return_value = {"status": "ok"}

    # Test corrected_by chain
    result_corrected = writer.write_episodic_event(
        event_name="used_tool_z",
        description="Used tool Z successfully",
        experience_type="success",
        prev_event_name="tried_tool_y",
        is_correction=True,
    )

    kg_calls = mock_ingester.inject_custom_kg.call_args_list
    chain_call = None
    for c in kg_calls:
        rels = c.kwargs.get("relationships", [])
        if rels and rels[0]["keywords"] in (
            CHAIN_RELATION_FOLLOWED,
            CHAIN_RELATION_CORRECTED,
        ):
            chain_call = rels[0]
            break

    assert chain_call is not None
    assert chain_call["keywords"] == CHAIN_RELATION_CORRECTED


# ============== Test 5: _determine_niu_relation ==============


def test_determine_niu_relation(writer: DreamWriter) -> None:
    """Verify relation type mapping."""
    assert writer._determine_niu_relation("Person") == "remembers"
    assert writer._determine_niu_relation("Skill") == "skilled_in"
    assert writer._determine_niu_relation("Concept") == "knows_about"
    assert writer._determine_niu_relation("Tool") == "uses"
    # Default case
    assert writer._determine_niu_relation("UnknownType") == "remembers"
    assert writer._determine_niu_relation("Place") == "remembers"
