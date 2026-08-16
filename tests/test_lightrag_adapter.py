"""
Tests for niu_api/internal/lightrag_adapter.py

LightRAGAdapter: query interface (replaces vector-store search + kg-server query).
LightRAGIngester: dual-path injection (structured via ainsert_custom_kg,
                  unstructured via ainsert).

TDD RED phase — these tests define the expected interface.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester

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
            adapter.query("What is X?")
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
            adapter.query("What is X?", only_need_context=True)
            call_args = mock_rag.aquery.call_args
            assert call_args[1]["param"].only_need_context is True

    def test_query_with_top_k(self):
        """J3: Custom top_k for retrieval depth control."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_rag = MagicMock()
        mock_rag.aquery = AsyncMock(return_value="Answer")

        with patch.object(adapter, "_get_rag", return_value=mock_rag):
            adapter.query("What is X?", top_k=10)
            call_args = mock_rag.aquery.call_args
            assert call_args[1]["param"].top_k == 10


class TestAdapterErrorHandling:
    """J5: Graceful error handling when LightRAG unavailable."""

    def test_query_returns_error_text_when_lightrag_not_installed(self):
        """E3 契约反转：错误不再伪装为无结果——query rag None 返回错误文本 str（通用文案）"""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        with patch.object(adapter, "_get_rag", return_value=None):
            result = adapter.query("What is X?")
            assert isinstance(result, str)
            assert "知识图谱不可用" in result

    def test_query_handles_lightrag_exception(self):
        """E3 契约反转：错误不再伪装为无结果——真异常返回错误文本 str（含"图谱查询失败"）"""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_rag = MagicMock()
        mock_rag.aquery = AsyncMock(side_effect=RuntimeError("LLM timeout"))

        with patch.object(adapter, "_get_rag", return_value=mock_rag):
            result = adapter.query("What is X?")
            # Should not raise, should return error text str
            assert isinstance(result, str)
            assert result.startswith("[图谱查询失败")
            assert "LLM timeout" in result


class TestSanitizeGraphError:
    """E3 专用脱敏 _sanitize_graph_error——绝对路径 + key/Bearer 凭证剥离。"""

    def test_strips_absolute_paths(self):
        """绝对路径（/Users/...、~/...、C:\\...）→ ***（不暴露本机目录结构给 LLM）"""
        from niu_api.internal.lightrag_adapter import _sanitize_graph_error

        msg = "Failed to open /Users/lilei/tools/ai-bot/data/vdb_entities.json: no such file; check ~/.niu/storage"
        result = _sanitize_graph_error(msg)
        assert "/Users/lilei" not in result
        assert "~/.niu" not in result
        assert result.count("***") >= 2

    def test_tilde_requires_path_separator(self):
        """~ 后必须跟 / 才剥离（P3-1）——~100/版本~1.0 不误伤，~/.niu 真实路径剥离"""
        from niu_api.internal.lightrag_adapter import _sanitize_graph_error

        msg = "score ~100 and version ~1.0; storage at ~/.niu/data; workdir ~/projects"
        result = _sanitize_graph_error(msg)
        assert "~100" in result
        assert "~1.0" in result
        assert "~/.niu" not in result
        assert "~/projects" not in result

    def test_strips_keys_and_bearer(self):
        """key=xxx / api_key=xxx / Bearer xxx 凭证 → 脱敏（复用 E2 规则）"""
        from niu_api.internal.lightrag_adapter import _sanitize_graph_error

        msg = "auth failed api_key=sk-abc123 Bearer token-xyz"
        result = _sanitize_graph_error(msg)
        assert "sk-abc123" not in result
        assert "token-xyz" not in result
        assert "api_key=***" in result
        assert "Bearer ***" in result


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

        ingester = LightRAGIngester()
        with patch.object(ingester, "_get_rag", return_value=None):
            result = ingester.inject_entity(
                name="Python", entity_type="Language", description="test"
            )
            assert result["status"] == "error"

    def test_inject_document_returns_error_when_no_lightrag(self):

        ingester = LightRAGIngester()
        with patch.object(ingester, "_get_rag", return_value=None):
            result = ingester.inject_document(content="test")
            assert result["status"] == "error"

    def test_inject_entity_handles_exception(self):

        ingester = LightRAGIngester()
        mock_rag = MagicMock()
        mock_rag.ainsert_custom_kg = AsyncMock(side_effect=RuntimeError("DB error"))

        with patch.object(ingester, "_get_rag", return_value=mock_rag):
            result = ingester.inject_entity(
                name="Python", entity_type="Language", description="test"
            )
            assert result["status"] == "error"

    def test_inject_document_handles_exception(self):

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
            adapter.query("List features", response_type="Bullet Points")
            call_args = mock_rag.aquery.call_args
            assert call_args[1]["param"].response_type == "Bullet Points"


# ============== Tests for _filter_by_entity_type ==============


class TestFilterByEntityType:
    """Tests for LightRAGAdapter.filter_by_entity_type."""

    def _make_adapter(self):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter
        return LightRAGAdapter()

    def test_returns_empty_for_none_input(self):
        adapter = self._make_adapter()
        result = adapter.filter_by_entity_type(None, "skill")
        assert result == []

    def test_returns_empty_for_empty_entities(self):
        adapter = self._make_adapter()
        query_result = {"data": {"entities": [], "relationships": []}}
        result = adapter.filter_by_entity_type(query_result, "skill")
        assert result == []

    def test_filters_matching_entity_type(self):
        adapter = self._make_adapter()
        query_result = {
            "data": {
                "entities": [
                    {"id": "e1", "name": "Python", "entity_type": "skill"},
                    {"id": "e2", "name": "Docker", "entity_type": "tool"},
                    {"id": "e3", "name": "FastAPI", "entity_type": "skill"},
                ],
            }
        }
        result = adapter.filter_by_entity_type(query_result, "skill")
        assert len(result) == 2
        assert all(e["entity_type"] == "skill" for e in result)

    def test_filters_case_insensitive(self):
        adapter = self._make_adapter()
        query_result = {
            "data": {
                "entities": [
                    {"id": "e1", "name": "Python", "entity_type": "Skill"},
                    {"id": "e2", "name": "Docker", "entity_type": "TOOL"},
                ],
            }
        }
        result = adapter.filter_by_entity_type(query_result, "skill")
        assert len(result) == 1
        assert result[0]["name"] == "Python"

    def test_handles_missing_entity_type_field(self):
        adapter = self._make_adapter()
        query_result = {
            "data": {
                "entities": [
                    {"id": "e1", "name": "Unknown"},
                    {"id": "e2", "name": "Python", "entity_type": "skill"},
                ],
            }
        }
        result = adapter.filter_by_entity_type(query_result, "skill")
        assert len(result) == 1
        assert result[0]["name"] == "Python"

    def test_handles_flat_dict_fallback(self):
        """When query_result has no 'data' key, treat it as the data dict directly."""
        adapter = self._make_adapter()
        query_result = {
            "entities": [
                {"id": "e1", "name": "Python", "entity_type": "skill"},
            ]
        }
        result = adapter.filter_by_entity_type(query_result, "skill")
        assert len(result) == 1

    def test_returns_empty_when_no_match(self):
        adapter = self._make_adapter()
        query_result = {
            "data": {
                "entities": [
                    {"id": "e1", "name": "Docker", "entity_type": "tool"},
                ],
            }
        }
        result = adapter.filter_by_entity_type(query_result, "skill")
        assert result == []


# ============== Tests for _query_data ==============


class TestQueryData:
    """Tests for LightRAGAdapter.query_data."""

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_calls_aquery_data_with_params(self, mock_get_rag, mock_call_async):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        rag = MagicMock()
        mock_get_rag.return_value = rag
        mock_call_async.return_value = {"data": {"entities": []}}

        adapter = LightRAGAdapter()
        result = adapter.query_data("test query", mode="local", top_k=5)

        mock_call_async.assert_called_once()
        assert result == {"data": {"entities": []}}

    @patch.object(LightRAGAdapter, "_get_rag")
    def test_returns_error_dict_when_rag_none(self, mock_get_rag):
        """E3 契约反转：错误不再伪装为无结果——query_data rag None 返回 error dict（通用文案）"""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        mock_get_rag.return_value = None
        adapter = LightRAGAdapter()
        result = adapter.query_data("test")
        assert isinstance(result, dict)
        assert result["status"] == "error"
        assert "知识图谱不可用" in result["message"]

    def test_raises_on_invalid_mode(self):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        with pytest.raises(ValueError, match="Invalid mode"):
            adapter.query_data("test", mode="invalid_mode")

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_returns_error_dict_on_exception(self, mock_get_rag, mock_call_async):
        """E3 契约反转：错误不再伪装为无结果——query_data 真异常返回 error dict"""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        rag = MagicMock()
        mock_get_rag.return_value = rag
        mock_call_async.side_effect = RuntimeError("query_data error")

        adapter = LightRAGAdapter()
        result = adapter.query_data("test", mode="local")
        assert isinstance(result, dict)
        assert result["status"] == "error"
        assert "query_data error" in result["message"]


# ============== Tests for search_skills ==============


class TestSearchSkills:
    """Tests for LightRAGAdapter.search_skills."""

    @patch.object(LightRAGAdapter, "query_data")
    @patch.object(LightRAGAdapter, "filter_by_entity_type")
    def test_calls_query_data_with_local_mode(self, mock_filter, mock_query_data):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        mock_query_data.return_value = {"data": {"entities": []}}
        mock_filter.return_value = []

        adapter = LightRAGAdapter()
        adapter.search_skills("python", top_k=5)

        mock_query_data.assert_called_once_with("python", mode="local", top_k=5, keywords=None)

    @patch.object(LightRAGAdapter, "filter_by_entity_type")
    @patch.object(LightRAGAdapter, "query_data")
    def test_filters_by_skill_type(self, mock_query_data, mock_filter):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        mock_query_data.return_value = {"data": {"entities": [{"entity_type": "skill"}]}}
        mock_filter.return_value = [{"entity_type": "skill"}]

        adapter = LightRAGAdapter()
        result = adapter.search_skills("python")

        mock_filter.assert_called_once_with(mock_query_data.return_value, "skill")
        assert result == [{"entity_type": "skill"}]

    @patch.object(LightRAGAdapter, "query_data")
    @patch.object(LightRAGAdapter, "filter_by_entity_type")
    def test_default_top_k_is_10(self, mock_filter, mock_query_data):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        mock_query_data.return_value = {}
        mock_filter.return_value = []

        adapter = LightRAGAdapter()
        adapter.search_skills("test")

        mock_query_data.assert_called_once_with("test", mode="local", top_k=10, keywords=None)


# ============== Tests for search_tools ==============


class TestSearchTools:
    """Tests for LightRAGAdapter.search_tools."""

    @patch.object(LightRAGAdapter, "query_data")
    @patch.object(LightRAGAdapter, "filter_by_entity_type")
    def test_filters_by_tool_type(self, mock_filter, mock_query_data):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        mock_query_data.return_value = {"data": {"entities": []}}
        mock_filter.return_value = [{"entity_type": "tool"}]

        adapter = LightRAGAdapter()
        result = adapter.search_tools("docker", top_k=5)

        mock_query_data.assert_called_once_with("docker", mode="local", top_k=5, keywords=None)
        mock_filter.assert_called_once_with(mock_query_data.return_value, "tool")
        assert result == [{"entity_type": "tool"}]


# ============== Tests for search_knowledge ==============


class TestSearchKnowledge:
    """Tests for LightRAGAdapter.search_knowledge.

    search_knowledge returns both "knowledge" and "concept" entity types.
    """

    @patch.object(LightRAGAdapter, "filter_by_entity_type")
    @patch.object(LightRAGAdapter, "query_data")
    def test_returns_knowledge_and_concept_types(self, mock_query_data, mock_filter):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        mock_query_data.return_value = {"data": {"entities": []}}
        knowledge_entities = [{"entity_type": "knowledge"}]
        concept_entities = [{"entity_type": "concept"}]
        mock_filter.side_effect = [knowledge_entities, concept_entities]

        adapter = LightRAGAdapter()
        result = adapter.search_knowledge("machine learning")

        assert mock_filter.call_count == 2
        mock_filter.assert_any_call(mock_query_data.return_value, "knowledge")
        mock_filter.assert_any_call(mock_query_data.return_value, "concept")
        assert len(result) == 2
        assert result[0]["entity_type"] == "knowledge"
        assert result[1]["entity_type"] == "concept"

    @patch.object(LightRAGAdapter, "filter_by_entity_type")
    @patch.object(LightRAGAdapter, "query_data")
    def test_uses_local_mode(self, mock_query_data, mock_filter):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        mock_query_data.return_value = {}
        mock_filter.return_value = []

        adapter = LightRAGAdapter()
        adapter.search_knowledge("test")

        mock_query_data.assert_called_once_with("test", mode="local", top_k=10, keywords=None)


class TestSearchByFilePath:
    """Tests for LightRAGAdapter.search_by_file_path."""

    @patch.object(LightRAGAdapter, "query_data")
    def test_raises_runtime_error_on_error_dict(self, mock_query_data):
        """E3 契约：error dict 必须 raise 传导（防重新静默丢失）——search_by_file_path 抛 RuntimeError 含原文"""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        mock_query_data.return_value = {"status": "error", "message": "boom"}

        with pytest.raises(RuntimeError, match="boom"):
            adapter.search_by_file_path("test", file_path_contains="skill_sync")


# ============== Tests for explore_node ==============


class TestExploreNode:
    """Tests for LightRAGAdapter.explore_node."""

    @patch.object(LightRAGAdapter, "_get_rag")
    def test_returns_empty_when_rag_none(self, mock_get_rag):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        mock_get_rag.return_value = None
        adapter = LightRAGAdapter()
        result = adapter.explore_node("Python")
        assert result["nodes"] == []
        assert result["edges"] == []
        assert result["stats"]["nodes"] == 0

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_returns_empty_when_kg_is_none(self, mock_get_rag, mock_call_async):
        """真空分支（P1）：kg 为 None 是真实无结果——返回无 status 空壳，不带 error dict"""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        mock_get_rag.return_value = MagicMock()
        mock_call_async.return_value = None

        adapter = LightRAGAdapter()
        result = adapter.explore_node("Python")
        assert result["nodes"] == []
        assert result["edges"] == []
        assert result["stats"]["nodes"] == 0
        assert "status" not in result
        assert "message" not in result

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_returns_empty_when_kg_has_no_nodes_or_edges(self, mock_get_rag, mock_call_async):
        """真空分支（P1）：空图（nodes/edges 均空）同样返回无 status 空壳"""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        kg = MagicMock()
        kg.nodes = []
        kg.edges = []
        mock_get_rag.return_value = MagicMock()
        mock_call_async.return_value = kg

        adapter = LightRAGAdapter()
        result = adapter.explore_node("Python")
        assert result["nodes"] == []
        assert result["edges"] == []
        assert result["stats"]["nodes"] == 0
        assert "status" not in result
        assert "message" not in result

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_returns_structured_subgraph(self, mock_get_rag, mock_call_async):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        rag = MagicMock()
        mock_get_rag.return_value = rag

        # Build mock KnowledgeGraph with nodes and edges
        node1 = MagicMock()
        node1.id = "Python"
        node1.properties = {"entity_type": "skill", "description": "A programming language"}

        node2 = MagicMock()
        node2.id = "FastAPI"
        node2.properties = {"entity_type": "tool", "description": "A web framework"}

        edge1 = MagicMock()
        edge1.source = "Python"
        edge1.target = "FastAPI"
        edge1.properties = {"keywords": "used_by", "description": "Python is used by FastAPI", "weight": 1.0}

        kg = MagicMock()
        kg.nodes = [node1, node2]
        kg.edges = [edge1]

        mock_call_async.return_value = kg

        adapter = LightRAGAdapter()
        result = adapter.explore_node("Python", depth=2)

        assert result["center"]["name"] == "Python"
        assert result["center"]["type"] == "skill"
        assert len(result["nodes"]) == 2
        assert len(result["edges"]) == 1
        assert result["edges"][0]["source"] == "Python"
        assert result["edges"][0]["target"] == "FastAPI"
        assert result["edges"][0]["relation"] == "used_by"
        assert result["stats"]["nodes"] == 2
        assert result["stats"]["edges"] == 1
        assert result["stats"]["max_depth"] == 2

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_clamps_depth_to_valid_range(self, mock_get_rag, mock_call_async):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        rag = MagicMock()
        mock_get_rag.return_value = rag

        kg = MagicMock()
        kg.nodes = []
        kg.edges = []
        mock_call_async.return_value = kg

        adapter = LightRAGAdapter()
        result = adapter.explore_node("test", depth=10)
        assert result["stats"]["max_depth"] == 5

        result = adapter.explore_node("test", depth=0)
        assert result["stats"]["max_depth"] == 1

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_handles_exception_gracefully(self, mock_get_rag, mock_call_async):
        """E3 契约反转：错误不再伪装为无结果——真异常返回空壳 + error dict"""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        rag = MagicMock()
        mock_get_rag.return_value = rag
        mock_call_async.side_effect = RuntimeError("graph error")

        adapter = LightRAGAdapter()
        result = adapter.explore_node("Python")
        assert result["status"] == "error"
        assert "graph error" in result["message"]
        assert result["nodes"] == []
        assert result["edges"] == []
        assert result["stats"]["nodes"] == 0


# ============== Tests for get_graph_snapshot ==============


class TestGetGraphSnapshot:
    """Tests for LightRAGAdapter.get_graph_snapshot."""

    @patch.object(LightRAGAdapter, "_get_rag")
    def test_returns_empty_when_rag_none(self, mock_get_rag):
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        mock_get_rag.return_value = None
        adapter = LightRAGAdapter()
        result = adapter.get_graph_snapshot()
        assert result["nodes"] == []
        assert result["edges"] == []

    @patch.object(LightRAGAdapter, "_get_rag")
    def test_returns_empty_when_kg_none(self, mock_get_rag):
        """get_graph_snapshot returns empty when chunk_entity_relation_graph is None."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        rag = MagicMock()
        mock_get_rag.return_value = rag
        # chunk_entity_relation_graph is None — no graph available
        rag.chunk_entity_relation_graph = None

        adapter = LightRAGAdapter()
        result = adapter.get_graph_snapshot()
        assert result["nodes"] == []
        assert result["edges"] == []

    @patch("niu_api.internal.lightrag_manager.graph_read_lock")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_returns_full_graph(self, mock_get_rag, mock_read_lock):
        """get_graph_snapshot reads NetworkX graph directly (not via call_async)."""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        rag = MagicMock()
        mock_get_rag.return_value = rag

        # Build a mock NetworkX-style graph object
        mock_graph = MagicMock()
        rag.chunk_entity_relation_graph = MagicMock()
        rag.chunk_entity_relation_graph._graph = mock_graph

        # Mock graph_read_lock as a context manager
        mock_read_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_read_lock.return_value.__exit__ = MagicMock(return_value=False)

        # mock_graph.copy() returns the snapshot
        snapshot = MagicMock()
        mock_graph.copy.return_value = snapshot

        # snapshot.nodes() returns node names
        snapshot.nodes.return_value = ["python", "docker"]
        # snapshot.degree() returns degree for each node
        snapshot.degree.side_effect = lambda n: {"python": 2, "docker": 1}[n]

        # snapshot.has_node() returns True for known nodes
        snapshot.has_node.side_effect = lambda n: n in ("python", "docker")

        # Use a real dict for node attributes so snapshot.nodes[node_name] works
        node_attrs = {
            "python": {"entity_type": "skill", "description": "A language"},
            "docker": {"entity_type": "tool", "description": "Container platform"},
        }
        # Make snapshot.nodes subscriptable like a dict
        snapshot.nodes.__getitem__ = MagicMock(side_effect=lambda key: node_attrs[key])

        # snapshot.edges(data=True) returns edge tuples
        snapshot.edges.return_value = [
            ("python", "docker", {"keywords": "deployed_with", "description": "", "weight": 1.0}),
        ]

        adapter = LightRAGAdapter()
        result = adapter.get_graph_snapshot(limit=100)

        assert len(result["nodes"]) == 2
        assert len(result["edges"]) == 1
        assert result["nodes"][0]["id"] == "python"
        assert result["nodes"][0]["type"] == "skill"
        assert result["edges"][0]["source"] == "python"
        assert result["edges"][0]["target"] == "docker"

    @patch("niu_api.internal.lightrag_manager.graph_read_lock")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_handles_exception_gracefully(self, mock_get_rag, mock_read_lock):
        """E3 契约反转：错误不再伪装为无结果——真异常返回空壳 + error dict"""
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        rag = MagicMock()
        mock_get_rag.return_value = rag

        # Mock graph_read_lock to raise inside the context manager
        mock_read_lock.return_value.__enter__ = MagicMock(
            side_effect=RuntimeError("snapshot error")
        )
        mock_read_lock.return_value.__exit__ = MagicMock(return_value=False)

        # Need chunk_entity_relation_graph to exist so code enters the try block
        rag.chunk_entity_relation_graph = MagicMock()
        rag.chunk_entity_relation_graph._graph = MagicMock()

        adapter = LightRAGAdapter()
        result = adapter.get_graph_snapshot()
        assert result["status"] == "error"
        assert "snapshot error" in result["message"]
        assert result["nodes"] == []
        assert result["edges"] == []
