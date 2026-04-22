"""
Tests for niu_api/internal/lightrag_adapter.py

LightRAGAdapter: query interface (replaces vector-store search + kg-server query).
LightRAGIngester: dual-path injection (structured via ainsert_custom_kg,
                  unstructured via ainsert).

TDD RED phase — these tests define the expected interface.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# ============== LightRAGAdapter Tests ==============


class TestAdapterQueryModes:
    """Test LightRAGAdapter query with different modes."""

    def test_query_naive_mode(self):
        """J3: Query with naive mode returns string result."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_rag = MagicMock()
        mock_rag.aquery = AsyncMock(return_value="Naive answer")

        with patch.object(adapter, "_get_rag", return_value=mock_rag):
            result = adapter.query("What is Python?", mode="naive")
            assert isinstance(result, str)
            assert result == "Naive answer"

    def test_query_local_mode(self):
        """J3: Local mode focuses on entity-specific context."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_rag = MagicMock()
        mock_rag.aquery = AsyncMock(return_value="Local context answer")

        with patch.object(adapter, "_get_rag", return_value=mock_rag):
            result = adapter.query("Tell me about entity X", mode="local")
            assert result == "Local context answer"

    def test_query_global_mode(self):
        """J3: Global mode uses community-level summaries."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_rag = MagicMock()
        mock_rag.aquery = AsyncMock(return_value="Global summary answer")

        with patch.object(adapter, "_get_rag", return_value=mock_rag):
            result = adapter.query("Overview of the system", mode="global")
            assert result == "Global summary answer"

    def test_query_hybrid_mode(self):
        """J3: Hybrid combines local + global retrieval."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_rag = MagicMock()
        mock_rag.aquery = AsyncMock(return_value="Hybrid answer")

        with patch.object(adapter, "_get_rag", return_value=mock_rag):
            result = adapter.query("How does X relate to Y?", mode="hybrid")
            assert result == "Hybrid answer"

    def test_query_mix_mode_default(self):
        """J3: Default mode is mix (knowledge graph + vector)."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_rag = MagicMock()
        mock_rag.aquery = AsyncMock(return_value="Mix answer")

        with patch.object(adapter, "_get_rag", return_value=mock_rag):
            result = adapter.query("What is X?")
            # Default mode should be "mix"
            call_args = mock_rag.aquery.call_args
            assert call_args[1]["param"].mode == "mix"

    def test_query_only_context(self):
        """J3: only_need_context=True returns context without LLM generation."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_rag = MagicMock()
        mock_rag.aquery = AsyncMock(return_value="Retrieved context only")

        with patch.object(adapter, "_get_rag", return_value=mock_rag):
            result = adapter.query("What is X?", only_need_context=True)
            call_args = mock_rag.aquery.call_args
            assert call_args[1]["param"].only_need_context is True

    def test_query_with_top_k(self):
        """J3: Custom top_k for retrieval depth control."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_rag = MagicMock()
        mock_rag.aquery = AsyncMock(return_value="Answer")

        with patch.object(adapter, "_get_rag", return_value=mock_rag):
            result = adapter.query("What is X?", top_k=10)
            call_args = mock_rag.aquery.call_args
            assert call_args[1]["param"].top_k == 10


class TestAdapterErrorHandling:
    """J5: Graceful error handling when LightRAG unavailable."""

    def test_query_returns_none_when_lightrag_not_installed(self):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        with patch.object(adapter, "_get_rag", return_value=None):
            result = adapter.query("What is X?")
            assert result is None

    def test_query_handles_lightrag_exception(self):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_rag = MagicMock()
        mock_rag.aquery = AsyncMock(side_effect=RuntimeError("LLM timeout"))

        with patch.object(adapter, "_get_rag", return_value=mock_rag):
            result = adapter.query("What is X?")
            # Should not raise, should return None or error indicator
            assert result is None


class TestAdapterInvalidMode:
    """J5: Invalid query mode handling."""

    def test_query_rejects_invalid_mode(self):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        with pytest.raises(ValueError, match="mode"):
            adapter.query("What is X?", mode="invalid_mode")


# ============== LightRAGIngester Tests ==============


class TestIngesterStructuredData:
    """J1, J4: Inject structured knowledge (entities + relations)."""

    def test_inject_entity(self):
        """J1: Inject a single entity into the brain graph."""
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert_custom_kg = AsyncMock(return_value=None)

        with patch.object(ingester, "_get_rag", return_value=mock_rag):
            result = ingester.inject_entity(
                name="Python",
                entity_type="ProgrammingLanguage",
                description="A high-level programming language",
                source_id="doc-1",
            )
            assert result["status"] == "ok"
            # Verify ainsert_custom_kg was called with correct structure
            call_args = mock_rag.ainsert_custom_kg.call_args
            kg = call_args[0][0]  # First positional arg
            assert "entities" in kg
            assert len(kg["entities"]) == 1
            assert kg["entities"][0]["entity_name"] == "Python"
            assert kg["entities"][0]["entity_type"] == "ProgrammingLanguage"

    def test_inject_relation(self):
        """J1: Inject a relation between two entities."""
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert_custom_kg = AsyncMock(return_value=None)

        with patch.object(ingester, "_get_rag", return_value=mock_rag):
            result = ingester.inject_relation(
                src_id="Python",
                tgt_id="Django",
                relation="has_framework",
                description="Django is a Python web framework",
                source_id="doc-1",
            )
            assert result["status"] == "ok"
            call_args = mock_rag.ainsert_custom_kg.call_args
            kg = call_args[0][0]
            assert "relationships" in kg
            assert len(kg["relationships"]) == 1
            assert kg["relationships"][0]["src_id"] == "Python"
            assert kg["relationships"][0]["tgt_id"] == "Django"
            # LightRAG requires "keywords" (direct access, no .get() fallback)
            assert "keywords" in kg["relationships"][0]
            assert kg["relationships"][0]["keywords"] == "has_framework"
            # LightRAG reads "weight" with .get() default 1.0
            assert "weight" in kg["relationships"][0]
            assert kg["relationships"][0]["weight"] == 1.0

    def test_inject_entity_with_chunk(self):
        """J1: Inject entity with associated chunk for vector retrieval."""
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert_custom_kg = AsyncMock(return_value=None)

        with patch.object(ingester, "_get_rag", return_value=mock_rag):
            result = ingester.inject_entity(
                name="Python",
                entity_type="ProgrammingLanguage",
                description="A high-level programming language",
                source_id="doc-1",
                chunk_content="Python is a high-level, general-purpose programming language.",
            )
            assert result["status"] == "ok"
            call_args = mock_rag.ainsert_custom_kg.call_args
            kg = call_args[0][0]
            assert "chunks" in kg
            assert len(kg["chunks"]) == 1
            assert "Python is a high-level" in kg["chunks"][0]["content"]

    def test_inject_batch_entities(self):
        """J4: Batch inject multiple entities (kg-server migration)."""
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert_custom_kg = AsyncMock(return_value=None)

        entities = [
            {"name": "Python", "entity_type": "Language", "description": "Programming language"},
            {"name": "Django", "entity_type": "Framework", "description": "Web framework"},
            {"name": "PostgreSQL", "entity_type": "Database", "description": "Relational DB"},
        ]

        with patch.object(ingester, "_get_rag", return_value=mock_rag):
            result = ingester.inject_custom_kg(
                entities=entities,
                relationships=[],
                chunks=[],
                source_id="migration-batch-1",
            )
            assert result["status"] == "ok"
            call_args = mock_rag.ainsert_custom_kg.call_args
            kg = call_args[0][0]
            assert len(kg["entities"]) == 3

    def test_inject_custom_kg_full(self):
        """J4: Full custom_kg with entities, relations, and chunks."""
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert_custom_kg = AsyncMock(return_value=None)

        with patch.object(ingester, "_get_rag", return_value=mock_rag):
            result = ingester.inject_custom_kg(
                entities=[
                    {"entity_name": "Python", "entity_type": "Language", "description": "Programming language"},
                ],
                relationships=[
                    {"src_id": "Python", "tgt_id": "Django", "keywords": "has_framework", "description": "Django is a Python framework"},
                ],
                chunks=[
                    {"content": "Python is versatile.", "source_id": "doc-1"},
                ],
                source_id="doc-1",
            )
            assert result["status"] == "ok"
            call_args = mock_rag.ainsert_custom_kg.call_args
            kg = call_args[0][0]
            assert len(kg["entities"]) == 1
            assert kg["entities"][0]["entity_name"] == "Python"
            assert len(kg["relationships"]) == 1
            assert "keywords" in kg["relationships"][0]
            assert kg["relationships"][0]["keywords"] == "has_framework"
            assert len(kg["chunks"]) == 1


    def test_inject_relation_keywords_from_relation(self):
        """J1: 'relation' key maps to 'keywords' for backward compat."""
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert_custom_kg = AsyncMock(return_value=None)

        with patch.object(ingester, "_get_rag", return_value=mock_rag):
            result = ingester.inject_custom_kg(
                entities=[],
                relationships=[
                    {"src_id": "A", "tgt_id": "B", "relation": "connects_to"},
                ],
                chunks=[],
            )
            assert result["status"] == "ok"
            call_args = mock_rag.ainsert_custom_kg.call_args
            kg = call_args[0][0]
            # "relation" should map to "keywords" in the output
            assert kg["relationships"][0]["keywords"] == "connects_to"

    def test_inject_entity_name_key_compat(self):
        """J4: Entity dict accepts both 'name' and 'entity_name' keys."""
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert_custom_kg = AsyncMock(return_value=None)

        with patch.object(ingester, "_get_rag", return_value=mock_rag):
            # Using 'name' key (convenience alias)
            result = ingester.inject_custom_kg(
                entities=[{"name": "Python", "entity_type": "Language"}],
                relationships=[],
                chunks=[],
            )
            assert result["status"] == "ok"
            call_args = mock_rag.ainsert_custom_kg.call_args
            kg = call_args[0][0]
            assert kg["entities"][0]["entity_name"] == "Python"


class TestIngesterUnstructuredData:
    """J2: Inject unstructured documents for auto-extraction."""

    def test_inject_document_string(self):
        """J2: Inject a single document string."""
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(return_value="track-123")

        with patch.object(ingester, "_get_rag", return_value=mock_rag):
            result = ingester.inject_document(
                content="Python is a high-level programming language created by Guido van Rossum."
            )
            assert result["status"] == "ok"
            assert "track_id" in result
            call_args = mock_rag.ainsert.call_args
            assert "Python is a high-level" in call_args[0][0]

    def test_inject_document_with_id(self):
        """J2: Inject document with explicit ID for dedup."""
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(return_value="track-456")

        with patch.object(ingester, "_get_rag", return_value=mock_rag):
            result = ingester.inject_document(
                content="Some document text",
                doc_id="unique-doc-1",
            )
            assert result["status"] == "ok"
            call_args = mock_rag.ainsert.call_args
            # IDs should be passed through
            assert call_args[1].get("ids") == "unique-doc-1" or call_args[0][1] == "unique-doc-1" or "ids" in str(call_args)

    def test_inject_document_with_file_path(self):
        """J2: Inject document with file path for citation."""
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(return_value="track-789")

        with patch.object(ingester, "_get_rag", return_value=mock_rag):
            result = ingester.inject_document(
                content="Document content",
                file_path="/docs/readme.md",
            )
            assert result["status"] == "ok"

    def test_inject_batch_documents(self):
        """J2: Batch inject multiple documents."""
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(return_value="track-batch")

        docs = [
            "First document about Python.",
            "Second document about Django.",
        ]

        with patch.object(ingester, "_get_rag", return_value=mock_rag):
            result = ingester.inject_documents(docs)
            assert result["status"] == "ok"
            call_args = mock_rag.ainsert.call_args
            # ainsert accepts list[str]
            assert isinstance(call_args[0][0], list)


class TestIngesterErrorHandling:
    """J5: Graceful error handling for ingestion."""

    def test_inject_entity_returns_error_when_no_lightrag(self):
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        with patch.object(ingester, "_get_rag", return_value=None):
            result = ingester.inject_entity(
                name="Python", entity_type="Language", description="test"
            )
            assert result["status"] == "error"

    def test_inject_document_returns_error_when_no_lightrag(self):
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        with patch.object(ingester, "_get_rag", return_value=None):
            result = ingester.inject_document(content="test")
            assert result["status"] == "error"

    def test_inject_entity_handles_exception(self):
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert_custom_kg = AsyncMock(side_effect=RuntimeError("DB error"))

        with patch.object(ingester, "_get_rag", return_value=mock_rag):
            result = ingester.inject_entity(
                name="Python", entity_type="Language", description="test"
            )
            assert result["status"] == "error"

    def test_inject_document_handles_exception(self):
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert = AsyncMock(side_effect=RuntimeError("LLM error"))

        with patch.object(ingester, "_get_rag", return_value=mock_rag):
            result = ingester.inject_document(content="test")
            assert result["status"] == "error"


# ============== Integration: Adapter + Ingester ==============


class TestAdapterIngesterIntegration:
    """Test that adapter and ingester share the same LightRAG instance."""

    def test_adapter_delegates_to_lightrag_manager(self):
        """Adapter._get_rag should call lightrag_manager.get_lightrag()."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        mock_rag = MagicMock()
        with patch("niu_api.internal.lightrag_adapter.get_lightrag", return_value=mock_rag):
            adapter = LightRAGAdapter()
            rag = adapter._get_rag()
            assert rag == mock_rag

    def test_ingester_delegates_to_lightrag_manager(self):
        """Ingester._get_rag should call lightrag_manager.get_lightrag()."""
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        mock_rag = MagicMock()
        with patch("niu_api.internal.lightrag_adapter.get_lightrag", return_value=mock_rag):
            ingester = LightRAGIngester()
            rag = ingester._get_rag()
            assert rag == mock_rag


# ============== Query Result Format Tests ==============


class TestQueryResultFormat:
    """Test query result formatting."""

    def test_query_returns_string(self):
        """Query should return plain string (LightRAG default)."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_rag = MagicMock()
        mock_rag.aquery = AsyncMock(return_value="Answer text")

        with patch.object(adapter, "_get_rag", return_value=mock_rag):
            result = adapter.query("test query")
            assert isinstance(result, str)

    def test_query_with_custom_response_type(self):
        """J3: Custom response_type (e.g., Bullet Points)."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_rag = MagicMock()
        mock_rag.aquery = AsyncMock(return_value="- Point 1\n- Point 2")

        with patch.object(adapter, "_get_rag", return_value=mock_rag):
            result = adapter.query("List features", response_type="Bullet Points")
            call_args = mock_rag.aquery.call_args
            assert call_args[1]["param"].response_type == "Bullet Points"
