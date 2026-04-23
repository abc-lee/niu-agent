"""
Tests for BrainGraph — Memory brain graph on LightRAG.

TDD RED phase: These tests define the BrainGraph API contract.
They should FAIL until the implementation is written.
"""

import re
from datetime import datetime, timezone
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

        assert normalize_name("c++/python") == "CPython"

    def test_truncate_64(self):
        from niu_api.internal.brain_graph import normalize_name

        long_name = "a" * 100
        result = normalize_name(long_name)
        assert len(result) <= 64

    def test_preserve_underscore_and_hyphen(self):
        from niu_api.internal.brain_graph import normalize_name

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


class TestBrainGraphStoreMemory:
    """Test memory storage in the brain graph."""

    def test_store_l0_memory_creates_related_to(self):
        """L0 memory should create brain:Niu --related_to--> entity with weight 0.3."""
        from niu_api.internal.brain_graph import BrainGraph

        bg = BrainGraph()
        bg._ingester = MagicMock()
        bg._ingester.inject_entity.return_value = {"status": "ok"}
        bg._ingester.inject_relation.return_value = {"status": "ok"}

        result = bg.store_memory(
            content="用户提到了asyncio问题",
            level="L0",
        )

        assert result["status"] == "ok"
        # Should have injected a relation from brain:Niu
        bg._ingester.inject_relation.assert_called()
        call_kwargs = bg._ingester.inject_relation.call_args
        assert call_kwargs[1]["src_id"] == "brain:Niu"
        assert call_kwargs[1]["relation"] == "related_to"

    def test_store_l1_memory_prefers(self):
        """L1 memory with type=preferences should create 'prefers' relation."""
        from niu_api.internal.brain_graph import BrainGraph

        bg = BrainGraph()
        bg._ingester = MagicMock()
        bg._ingester.inject_entity.return_value = {"status": "ok"}
        bg._ingester.inject_relation.return_value = {"status": "ok"}

        result = bg.store_memory(
            content="偏好暗色主题编码",
            level="L1",
            memory_type="preferences",
        )

        assert result["status"] == "ok"
        call_kwargs = bg._ingester.inject_relation.call_args
        assert call_kwargs[1]["relation"] == "prefers"

    def test_store_l1_memory_skills(self):
        """L1 memory with type=skills should create 'skilled_in' relation."""
        from niu_api.internal.brain_graph import BrainGraph

        bg = BrainGraph()
        bg._ingester = MagicMock()
        bg._ingester.inject_entity.return_value = {"status": "ok"}
        bg._ingester.inject_relation.return_value = {"status": "ok"}

        result = bg.store_memory(
            content="擅长Web开发",
            level="L1",
            memory_type="skills",
        )

        assert result["status"] == "ok"
        call_kwargs = bg._ingester.inject_relation.call_args
        assert call_kwargs[1]["relation"] == "skilled_in"

    def test_store_l1_memory_experiences(self):
        """L1 memory with type=experiences should create 'remembers' relation."""
        from niu_api.internal.brain_graph import BrainGraph

        bg = BrainGraph()
        bg._ingester = MagicMock()
        bg._ingester.inject_entity.return_value = {"status": "ok"}
        bg._ingester.inject_relation.return_value = {"status": "ok"}

        result = bg.store_memory(
            content="从2019年开始用Python做AI/ML",
            level="L1",
            memory_type="experiences",
        )

        assert result["status"] == "ok"
        call_kwargs = bg._ingester.inject_relation.call_args
        assert call_kwargs[1]["relation"] == "remembers"

    def test_store_l2_memory_high_weight(self):
        """L2 memory should have weight 0.9."""
        from niu_api.internal.brain_graph import BrainGraph

        bg = BrainGraph()
        bg._ingester = MagicMock()
        bg._ingester.inject_entity.return_value = {"status": "ok"}
        bg._ingester.inject_relation.return_value = {"status": "ok"}

        result = bg.store_memory(
            content="Python的GIL机制导致多线程无法真正并行",
            level="L2",
        )

        assert result["status"] == "ok"
        call_kwargs = bg._ingester.inject_relation.call_args
        assert call_kwargs[1]["weight"] == 0.9


class TestBrainGraphRecallMemories:
    """Test memory recall from the brain graph."""

    def test_recall_returns_list(self):
        """recall_memories should return a list of memory dicts."""
        from niu_api.internal.brain_graph import BrainGraph

        bg = BrainGraph()
        bg._adapter = MagicMock()
        bg._adapter.query.return_value = "Niu prefers Dark_Mode. Niu skilled_in Web_Development."

        result = bg.recall_memories(query="编码偏好", top_k=5)

        assert isinstance(result, list)

    def test_recall_uses_mix_mode(self):
        """recall_memories should use LightRAG mix mode for comprehensive retrieval."""
        from niu_api.internal.brain_graph import BrainGraph

        bg = BrainGraph()
        bg._adapter = MagicMock()
        bg._adapter.query.return_value = ""

        bg.recall_memories(query="Python", top_k=10)

        bg._adapter.query.assert_called_once()
        call_kwargs = bg._adapter.query.call_args
        assert call_kwargs[1]["mode"] == "mix"

    def test_recall_min_weight_filter(self):
        """Memories below min_weight should be filtered out."""
        from niu_api.internal.brain_graph import BrainGraph

        bg = BrainGraph()
        bg._adapter = MagicMock()
        bg._adapter.query.return_value = ""

        # Even with empty result, should not raise
        result = bg.recall_memories(query="test", min_weight=0.5)
        assert isinstance(result, list)


class TestBrainGraphEnsureNiu:
    """Test brain:Niu entity initialization."""

    def test_ensure_niu_entity_creates_entity(self):
        """ensure_niu_entity should inject brain:Niu if not present."""
        from niu_api.internal.brain_graph import BrainGraph

        bg = BrainGraph()
        bg._ingester = MagicMock()
        bg._ingester.inject_entity.return_value = {"status": "ok"}

        bg.ensure_niu_entity()

        bg._ingester.inject_entity.assert_called_once()
        call_kwargs = bg._ingester.inject_entity.call_args
        assert call_kwargs[1]["name"] == "brain:Niu"
        assert call_kwargs[1]["entity_type"] == "Niu"


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


class TestLevelDefaults:
    """Test level-to-weight and decay_rate defaults."""

    def test_l0_defaults(self):
        from niu_api.internal.brain_graph import LEVEL_DEFAULTS

        assert LEVEL_DEFAULTS["L0"]["weight"] == 0.3
        assert LEVEL_DEFAULTS["L0"]["decay_rate"] == 0.05
        assert LEVEL_DEFAULTS["L0"]["relation_type"] == "related_to"

    def test_l1_defaults(self):
        from niu_api.internal.brain_graph import LEVEL_DEFAULTS

        assert LEVEL_DEFAULTS["L1"]["weight"] == 0.7
        assert LEVEL_DEFAULTS["L1"]["decay_rate"] == 0.01

    def test_l2_defaults(self):
        from niu_api.internal.brain_graph import LEVEL_DEFAULTS

        assert LEVEL_DEFAULTS["L2"]["weight"] == 0.9
        assert LEVEL_DEFAULTS["L2"]["decay_rate"] == 0.002


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
