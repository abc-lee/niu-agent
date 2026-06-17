"""
Tests for BrainGraph — Memory brain graph on LightRAG.

TDD GREEN phase: Tests define the BrainGraph API contract.
"""

from unittest.mock import MagicMock


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
    """Test entity name generation — natural language, no colon prefix."""

    def test_person_entity(self):
        from niu_api.internal.brain_graph import make_entity_name

        assert make_entity_name("person", "LiLei") == "LiLei"

    def test_concept_entity(self):
        from niu_api.internal.brain_graph import make_entity_name

        assert make_entity_name("concept", "Knowledge Graph") == "Knowledge Graph"

    def test_skill_entity(self):
        from niu_api.internal.brain_graph import make_entity_name

        assert make_entity_name("skill", "web development") == "web development"

    def test_niu_entity(self):
        from niu_api.internal.brain_graph import make_entity_name

        assert make_entity_name("Niu", "") == "Niu"


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
    # Default: has_entity returns False so ensure_niu_entity will inject
    bg._adapter.has_entity.return_value = False
    return bg


class TestBrainGraphEnsureNiu:
    """Test Niu entity initialization."""

    def test_ensure_niu_entity_creates_entity(self):
        """ensure_niu_entity should inject Niu if not present."""
        bg = _make_mock_brain_graph()

        bg.ensure_niu_entity()

        bg._ingester.inject_custom_kg.assert_called_once()
        call_kwargs = bg._ingester.inject_custom_kg.call_args
        entities = call_kwargs[1]["entities"]
        assert len(entities) == 1
        assert entities[0]["entity_name"] == "Niu"
        assert entities[0]["entity_type"] == "Niu"


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
