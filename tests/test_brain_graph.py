"""
Tests for BrainGraph — Memory brain graph on LightRAG.

TDD GREEN phase: Tests define the BrainGraph API contract.
"""

import re
from unittest.mock import MagicMock, patch

import pytest


# ============== normalize_name ==============


class TestNormalizeName:
    """Test name normalization for brain: namespace."""

    def test_spaces_to_underscores(self):
        from niu_api.internal.brain_graph import normalize_name

        assert normalize_name("web development") == "Web_Development"

    def test_pascal_case(self):
        from niu_api.internal.brain_graph import normalize_name

        assert normalize_name("rust programming") == "Rust_Programming"

    def test_remove_special_chars(self):
        from niu_api.internal.brain_graph import normalize_name

        result = normalize_name("c++/python")
        assert "++" not in result
        assert "/" not in result

    def test_truncate_64(self):
        from niu_api.internal.brain_graph import normalize_name

        long_name = "a" * 100
        result = normalize_name(long_name)
        assert len(result) <= 64

    def test_preserve_underscore_and_hyphen(self):
        from niu_api.internal.brain_graph import normalize_name

        # Hyphens and underscores both become segment separators, joined with _
        assert normalize_name("ai-bot project") == "Ai_Bot_Project"

    def test_empty_string(self):
        from niu_api.internal.brain_graph import normalize_name

        assert normalize_name("") == ""

    def test_single_word(self):
        from niu_api.internal.brain_graph import normalize_name

        assert normalize_name("python") == "Python"


# ============== make_entity_name ==============


class TestMakeEntityName:
    """Test entity name generation with brain: prefix."""

    def test_person_entity(self):
        from niu_api.internal.brain_graph import make_entity_name

        assert make_entity_name("person", "LiLei") == "brain:person:LiLei"

    def test_concept_entity(self):
        from niu_api.internal.brain_graph import make_entity_name

        assert make_entity_name("concept", "Knowledge Graph") == "brain:concept:Knowledge_Graph"

    def test_skill_entity(self):
        from niu_api.internal.brain_graph import make_entity_name

        assert make_entity_name("skill", "web development") == "brain:skill:Web_Development"

    def test_niu_entity(self):
        from niu_api.internal.brain_graph import make_entity_name

        assert make_entity_name("Niu", "") == "brain:Niu"


# ============== BrainGraph ==============


def _make_mock_brain_graph():
    """Create a BrainGraph with mocked ingester and adapter."""
    from niu_api.internal.brain_graph import BrainGraph

    bg = BrainGraph.__new__(BrainGraph)
    bg._ingester = MagicMock()
    bg._adapter = MagicMock()
    bg._ingester.inject_entity.return_value = {"status": "ok"}
    bg._ingester.inject_custom_kg.return_value = {"status": "ok"}
    # Default: query_data returns no structured data (falls back to text query)
    bg._adapter.query_data.return_value = None
    return bg


class TestBrainGraphStoreMemory:
    """Test memory storage in the brain graph."""

    def test_store_memory_no_type_default(self):
        """Memory without type should store entity with default weight."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="用户提到了asyncio问题",
        )

        assert result["status"] == "ok"
        assert result["relation_type"] == "remembers"
        assert result["weight"] == 0.7
        # Single atomic call — no niu→entity relationship
        assert bg._ingester.inject_custom_kg.call_count == 1
        call_kwargs = bg._ingester.inject_custom_kg.call_args
        assert call_kwargs[1]["relationships"] == []

    def test_store_l1_memory_prefers(self):
        """Memory with type=preferences should store entity with 'prefers' relation_type."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="偏好暗色主题编码",
            memory_type="preferences",
        )

        assert result["status"] == "ok"
        assert result["relation_type"] == "prefers"
        assert bg._ingester.inject_custom_kg.call_count == 1

    def test_store_memory_skills(self):
        """Memory with type=skills should store entity with 'skilled_in' relation_type."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="擅长Web开发",
            memory_type="skills",
        )

        assert result["status"] == "ok"
        assert result["relation_type"] == "skilled_in"
        assert bg._ingester.inject_custom_kg.call_count == 1

    def test_store_memory_experiences(self):
        """Memory with type=experiences should store entity with 'remembers' relation_type."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="从2019年开始用Python做AI/ML",
            memory_type="experiences",
        )

        assert result["status"] == "ok"
        assert result["relation_type"] == "remembers"
        assert bg._ingester.inject_custom_kg.call_count == 1

    def test_store_memory_default_weight(self):
        """Memory without type should use default weight 0.7."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="Python的GIL机制导致多线程无法真正并行",
        )

        assert result["status"] == "ok"
        assert result["weight"] == 0.7
        assert bg._ingester.inject_custom_kg.call_count == 1

    def test_store_memory_unknown_type_defaults(self):
        """Unknown memory_type should fall back to DEFAULT_RELATION_TYPE."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="hobby content",
            memory_type="hobbies",
        )

        assert result["status"] == "ok"
        assert result["relation_type"] == "remembers"
        assert result["entity_type"] == "Concept"


class TestBrainGraphRecallMemories:
    """Test memory recall from the brain graph."""

    def test_recall_returns_list(self):
        """recall_memories should return a list of memory dicts."""
        bg = _make_mock_brain_graph()
        # query_data returns structured data with relationships
        bg._adapter.query_data.return_value = {
            "data": {
                "relationships": [
                    {"src_id": "brain:Niu", "tgt_id": "brain:concept:Dark_Mode", "keywords": "prefers", "description": "偏好暗色主题", "weight": 0.7},
                ]
            }
        }

        result = bg.recall_memories(query="编码偏好", top_k=5)

        assert isinstance(result, list)

    def test_recall_uses_structured_data_first(self):
        """recall_memories should use query_data for structured retrieval with real weights."""
        bg = _make_mock_brain_graph()
        bg._adapter.query_data.return_value = {
            "data": {
                "relationships": [
                    {"src_id": "brain:Niu", "tgt_id": "brain:concept:Python", "keywords": "skilled_in", "description": "擅长Python", "weight": 0.9},
                ]
            }
        }

        result = bg.recall_memories(query="Python", top_k=10)

        bg._adapter.query_data.assert_called_once()
        assert len(result) == 1
        assert result[0]["weight"] == 0.9
        assert result[0]["target"] == "brain:concept:Python"

    def test_recall_falls_back_to_text_query(self):
        """recall_memories should fall back to text query if query_data returns no relationships."""
        bg = _make_mock_brain_graph()
        bg._adapter.query_data.return_value = None
        bg._adapter.query.return_value = "brain:concept:Python is a language."

        result = bg.recall_memories(query="Python")

        # Should have fallen back to text-based extraction
        bg._adapter.query.assert_called_once()
        assert isinstance(result, list)

    def test_recall_min_weight_filter(self):
        """Memories below min_weight should be filtered out."""
        bg = _make_mock_brain_graph()
        bg._adapter.query_data.return_value = {
            "data": {
                "relationships": [
                    {"src_id": "brain:Niu", "tgt_id": "brain:concept:Low", "keywords": "related_to", "description": "低权重记忆", "weight": 0.2},
                    {"src_id": "brain:Niu", "tgt_id": "brain:concept:High", "keywords": "remembers", "description": "高权重记忆", "weight": 0.9},
                ]
            }
        }

        result = bg.recall_memories(query="test", min_weight=0.5)

        assert len(result) == 1
        assert result[0]["target"] == "brain:concept:High"

    def test_recall_extracts_brain_entities_from_text_fallback(self):
        """recall_memories text fallback should extract entities from text."""
        bg = _make_mock_brain_graph()
        bg._adapter.query_data.return_value = None
        bg._adapter.query.return_value = "brain:concept:Python is a language. brain:skill:Web_Development is useful."

        result = bg.recall_memories(query="编程技能")

        assert len(result) >= 1
        # New logic extracts target from text content, not hardcoded "Niu"
        assert result[0]["relation_type"] == "remembers"
        assert "Python" in result[0]["description"]


class TestBrainGraphEnsureNiu:
    """Test brain:Niu entity initialization."""

    def test_ensure_niu_entity_creates_entity(self):
        """ensure_niu_entity should inject brain:Niu if not present."""
        bg = _make_mock_brain_graph()

        bg.ensure_niu_entity()

        bg._ingester.inject_custom_kg.assert_called_once()
        call_kwargs = bg._ingester.inject_custom_kg.call_args
        entities = call_kwargs[1]["entities"]
        assert len(entities) == 1
        assert entities[0]["entity_name"] == "brain:Niu"
        assert entities[0]["entity_type"] == "Niu"


class TestMemoryTypeMapping:
    """Test memory_type to relation_type mapping."""

    def test_environment_maps_to_located_at(self):
        from niu_api.internal.brain_graph import MEMORY_TYPE_TO_RELATION

        assert MEMORY_TYPE_TO_RELATION["environment"] == "located_at"

    def test_preferences_maps_to_prefers(self):
        from niu_api.internal.brain_graph import MEMORY_TYPE_TO_RELATION

        assert MEMORY_TYPE_TO_RELATION["preferences"] == "prefers"

    def test_skills_maps_to_skilled_in(self):
        from niu_api.internal.brain_graph import MEMORY_TYPE_TO_RELATION

        assert MEMORY_TYPE_TO_RELATION["skills"] == "skilled_in"

    def test_experiences_maps_to_remembers(self):
        from niu_api.internal.brain_graph import MEMORY_TYPE_TO_RELATION

        assert MEMORY_TYPE_TO_RELATION["experiences"] == "remembers"

    def test_facts_maps_to_remembers(self):
        from niu_api.internal.brain_graph import MEMORY_TYPE_TO_RELATION

        assert MEMORY_TYPE_TO_RELATION["facts"] == "remembers"


class TestFormatMemoriesForPrompt:
    """Test memory formatting for system prompt injection."""

    def test_format_empty_memories(self):
        from niu_api.internal.brain_graph import format_memories_for_prompt

        result = format_memories_for_prompt([])
        assert result == ""

    def test_format_single_memory(self):
        from niu_api.internal.brain_graph import format_memories_for_prompt

        memories = [
            {
                "target": "brain:concept:Python",
                "relation_type": "remembers",
                "description": "从2019年开始用Python",
                "weight": 0.85,
            }
        ]
        result = format_memories_for_prompt(memories)
        assert "Python" in result
        assert "从2019年开始用Python" in result

    def test_format_multiple_memories_sorted_by_weight(self):
        from niu_api.internal.brain_graph import format_memories_for_prompt

        memories = [
            {
                "target": "brain:concept:Rust",
                "relation_type": "learned_from",
                "description": "最近在学Rust",
                "weight": 0.5,
            },
            {
                "target": "brain:concept:Python",
                "relation_type": "skilled_in",
                "description": "擅长Python",
                "weight": 0.9,
            },
        ]
        result = format_memories_for_prompt(memories)
        # Higher weight (Python) should appear first
        assert result.index("Python") < result.index("Rust")


class TestGetBrainGraphSingleton:
    """Test get_brain_graph() singleton."""

    def test_returns_same_instance(self):
        from niu_api.internal.brain_graph import get_brain_graph

        bg1 = get_brain_graph()
        bg2 = get_brain_graph()
        assert bg1 is bg2

    def test_returns_brain_graph_instance(self):
        from niu_api.internal.brain_graph import BrainGraph, get_brain_graph

        bg = get_brain_graph()
        assert isinstance(bg, BrainGraph)


class TestMetadataEmbedding:
    """Test metadata embedding in store_memory."""

    def test_metadata_embedded_in_description(self):
        """metadata should be embedded as JSON in the entity description."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="用户提到了asyncio问题",
            metadata={"source": "chat", "turn": 5},
        )

        assert result["status"] == "ok"
        assert bg._ingester.inject_custom_kg.call_count == 1
        call_kwargs = bg._ingester.inject_custom_kg.call_args
        entities = call_kwargs[1]["entities"]
        desc = entities[0]["description"]
        assert "[meta:" in desc
        assert "source" in desc

    def test_no_metadata_no_bracket(self):
        """Without metadata, entity description should not contain [meta:]."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="用户提到了asyncio问题",
        )

        assert result["status"] == "ok"
        assert bg._ingester.inject_custom_kg.call_count == 1
        call_kwargs = bg._ingester.inject_custom_kg.call_args
        entities = call_kwargs[1]["entities"]
        desc = entities[0]["description"]
        assert "[meta:" not in desc

    def test_metadata_stripped_from_prompt(self):
        """format_memories_for_prompt should strip [meta:...] from description."""
        from niu_api.internal.brain_graph import format_memories_for_prompt

        memories = [
            {
                "target": "brain:concept:Python",
                "relation_type": "remembers",
                "description": "擅长Python [meta:{\"source\":\"chat\"}]",
                "weight": 0.9,
            }
        ]
        result = format_memories_for_prompt(memories)
        assert "擅长Python" in result
        assert "[meta:" not in result
        assert "source" not in result

    def test_metadata_too_long_skipped(self):
        """Metadata exceeding 200 chars should be skipped entirely."""
        bg = _make_mock_brain_graph()

        big_meta = {"key": "x" * 300}
        result = bg.store_memory(
            content="测试内容",
            metadata=big_meta,
        )

        assert result["status"] == "ok"
        assert bg._ingester.inject_custom_kg.call_count == 1
        call_kwargs = bg._ingester.inject_custom_kg.call_args
        entities = call_kwargs[1]["entities"]
        desc = entities[0]["description"]
        assert "[meta:" not in desc
