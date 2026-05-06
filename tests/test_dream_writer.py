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
    INVOLVES_RELATION,
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
    """Verify entity + brain:Niu relation created in single atomic call."""
    result = writer.write_semantic_entity(
        name="Python",
        entity_type="Skill",
        description="Programming language",
    )

    # Check status
    assert result["status"] == "ok"
    assert result["name"] == "Python"
    assert result["entity_type"] == "Skill"
    assert result["niu_relation_keyword"] == "skilled_in"

    # Verify single atomic inject_custom_kg call with entity + Niu relation + chunk
    mock_ingester.inject_custom_kg.assert_called_once()
    kg_call = mock_ingester.inject_custom_kg.call_args

    # Entity
    entity = kg_call.kwargs["entities"][0]
    assert entity["entity_name"] == "Python"
    assert entity["description"] == "Programming language"

    # brain:Niu anchor relation
    rel = kg_call.kwargs["relationships"][0]
    assert rel["src_id"] == NIU_ENTITY
    assert rel["tgt_id"] == "Python"
    assert rel["keywords"] == "skilled_in"

    # Chunk
    chunk = kg_call.kwargs["chunks"][0]
    assert chunk["content"] == "Programming language"


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
    """Verify event entity created with brain:Niu anchor in single atomic call."""
    result = writer.write_episodic_event(
        event_name="tool_x_failed",
        description="Tool X returned error code 500",
        experience_type="error",
        session_id="sess-001",
    )

    # Check status
    assert result["status"] == "ok"
    assert result["event_name"] == f"{EVENT_PREFIX}tool_x_failed"
    assert result["experience_type"] == "error"

    # Verify single atomic inject_custom_kg call
    mock_ingester.inject_custom_kg.assert_called_once()
    kg_call = mock_ingester.inject_custom_kg.call_args

    # Entity
    entity = kg_call.kwargs["entities"][0]
    assert entity["entity_name"] == f"{EVENT_PREFIX}tool_x_failed"
    assert entity["entity_type"] == EPISODIC_ENTITY_TYPE
    assert entity["description"] == "Tool X returned error code 500"

    # brain:Niu anchor relation (always present)
    rels = kg_call.kwargs["relationships"]
    niu_rel = [r for r in rels if r["src_id"] == NIU_ENTITY][0]
    assert niu_rel["tgt_id"] == f"{EVENT_PREFIX}tool_x_failed"
    assert niu_rel["keywords"] == "experienced"

    # No time chain or involves relations — only the Niu anchor
    assert len(rels) == 1


# ============== Test 4: write_episodic_event_with_chain ==============


def test_write_episodic_event_with_chain(
    writer: DreamWriter, mock_ingester: MagicMock
) -> None:
    """Verify followed_by/corrected_by chain in single atomic call."""
    # Test followed_by chain
    result_followed = writer.write_episodic_event(
        event_name="tried_tool_y",
        description="Tried tool Y",
        experience_type="success",
        prev_event_name="tried_tool_x",
        is_correction=False,
    )

    assert result_followed["status"] == "ok"

    # Single atomic call contains all relationships
    mock_ingester.inject_custom_kg.assert_called_once()
    kg_call = mock_ingester.inject_custom_kg.call_args
    rels = kg_call.kwargs["relationships"]

    # Find the chain relation
    chain_rel = None
    for r in rels:
        if r["keywords"] in (CHAIN_RELATION_FOLLOWED, CHAIN_RELATION_CORRECTED):
            chain_rel = r
            break

    assert chain_rel is not None
    assert chain_rel["src_id"] == f"{EVENT_PREFIX}tried_tool_x"
    assert chain_rel["tgt_id"] == f"{EVENT_PREFIX}tried_tool_y"
    assert chain_rel["keywords"] == CHAIN_RELATION_FOLLOWED

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

    assert result_corrected["status"] == "ok"

    mock_ingester.inject_custom_kg.assert_called_once()
    kg_call = mock_ingester.inject_custom_kg.call_args
    rels = kg_call.kwargs["relationships"]

    chain_rel = None
    for r in rels:
        if r["keywords"] in (CHAIN_RELATION_FOLLOWED, CHAIN_RELATION_CORRECTED):
            chain_rel = r
            break

    assert chain_rel is not None
    assert chain_rel["keywords"] == CHAIN_RELATION_CORRECTED


# ============== Test 5: write_episodic_event_with_involves ==============


def test_write_episodic_event_with_involves(
    writer: DreamWriter, mock_ingester: MagicMock
) -> None:
    """Verify involves relations included in single atomic call."""
    result = writer.write_episodic_event(
        event_name="data_analysis_session",
        description="Analyzed sales data",
        experience_type="success",
        related_entities=["Python", "pandas"],
    )

    assert result["status"] == "ok"

    # Single atomic call
    mock_ingester.inject_custom_kg.assert_called_once()
    kg_call = mock_ingester.inject_custom_kg.call_args
    rels = kg_call.kwargs["relationships"]

    # brain:Niu anchor + 2 involves relations = 3 total
    involves_rels = [r for r in rels if r["keywords"] == INVOLVES_RELATION]
    assert len(involves_rels) == 2
    assert involves_rels[0]["src_id"] == f"{EVENT_PREFIX}data_analysis_session"
    assert involves_rels[0]["tgt_id"] == "Python"
    assert involves_rels[1]["tgt_id"] == "pandas"


# ============== Test 6: _determine_niu_relation ==============


def test_determine_niu_relation(writer: DreamWriter) -> None:
    """Verify relation type mapping."""
    assert writer._determine_niu_relation("Person") == "remembers"
    assert writer._determine_niu_relation("Skill") == "skilled_in"
    assert writer._determine_niu_relation("Concept") == "knows_about"
    assert writer._determine_niu_relation("Tool") == "uses"
    # Default case
    assert writer._determine_niu_relation("UnknownType") == "remembers"
    assert writer._determine_niu_relation("Place") == "remembers"
