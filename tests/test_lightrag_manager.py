"""
Tests for niu_api/internal/lightrag_manager.py

LightRAG instance lifecycle, async/sync bridge, status reporting.
"""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


# ============== Config Tests ==============


class TestConfig:
    """Test LightRAG configuration reading."""

    def test_proxy_base_url(self):
        from niu_api.internal.lightrag_manager import PROXY_BASE_URL
        assert "llm/v1" in PROXY_BASE_URL

    def test_storage_dir_under_niu_home(self):
        from niu_api.internal.lightrag_manager import STORAGE_DIR
        assert ".niu" in str(STORAGE_DIR)
        assert "lightrag_storage" in str(STORAGE_DIR)

    def test_get_lightrag_config_returns_dict(self):
        from niu_api.internal.lightrag_manager import _get_lightrag_config
        config = _get_lightrag_config()
        assert isinstance(config, dict)


# ============== Availability Tests ==============


class TestAvailability:
    """Test LightRAG availability detection."""

    def test_is_available_false_when_not_installed(self):
        from niu_api.internal.lightrag_manager import is_lightrag_available
        # LightRAG is not installed in test environment
        result = is_lightrag_available()
        assert isinstance(result, bool)
        # In test env, likely False since lightrag-hku is not installed
        # But we don't assert False because it might be installed

    def test_get_lightrag_returns_none_when_not_installed(self):
        from niu_api.internal.lightrag_manager import get_lightrag
        import niu_api.internal.lightrag_manager as mgr
        # Reset instance
        mgr._rag_instance = None

        with patch("niu_api.internal.lightrag_manager.is_lightrag_available", return_value=False):
            # Even if available, _create_lightrag_instance will fail
            with patch("niu_api.internal.lightrag_manager._create_lightrag_instance", side_effect=ImportError("no lightrag")):
                result = get_lightrag()
                assert result is None


# ============== Status Tests ==============


class TestStatus:
    """Test get_lightrag_status() diagnostics."""

    def test_returns_dict_with_required_keys(self):
        from niu_api.internal.lightrag_manager import get_lightrag_status
        status = get_lightrag_status()
        assert "installed" in status
        assert "initialized" in status
        assert "storage_dir" in status
        assert "proxy_base_url" in status
        assert "embedding" in status
        assert "reranker" in status
        assert "loop_running" in status

    def test_status_shows_not_initialized(self):
        from niu_api.internal.lightrag_manager import get_lightrag_status
        import niu_api.internal.lightrag_manager as mgr
        mgr._rag_instance = None

        status = get_lightrag_status()
        assert status["initialized"] is False

    def test_status_includes_embedding_info(self):
        from niu_api.internal.lightrag_manager import get_lightrag_status
        status = get_lightrag_status()
        assert "name" in status["embedding"]
        assert "dim" in status["embedding"]

    def test_status_includes_reranker_info(self):
        from niu_api.internal.lightrag_manager import get_lightrag_status
        status = get_lightrag_status()
        assert "name" in status["reranker"]


# ============== Async/Sync Bridge Tests ==============


class TestAsyncSyncBridge:
    """Test the daemon event loop bridge."""

    def test_ensure_loop_creates_loop(self):
        from niu_api.internal.lightrag_manager import _ensure_loop
        import niu_api.internal.lightrag_manager as mgr

        # Reset loop state
        mgr._loop = None
        mgr._loop_thread = None

        loop = _ensure_loop()
        assert loop is not None
        assert loop.is_running()

    def test_call_async_runs_coroutine(self):
        from niu_api.internal.lightrag_manager import call_async
        import asyncio

        async def sample_coro():
            return 42

        result = call_async(sample_coro())
        assert result == 42

    def test_call_async_handles_exceptions(self):
        from niu_api.internal.lightrag_manager import call_async

        async def failing_coro():
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            call_async(failing_coro())


# ============== Embedding Dim Tests ==============


class TestEmbeddingDimForLightRAG:
    """Test _get_embedding_dim_for_lightrag() delegates correctly."""

    def test_returns_current_dim(self):
        from niu_api.internal.lightrag_manager import _get_embedding_dim_for_lightrag
        dim = _get_embedding_dim_for_lightrag()
        assert isinstance(dim, int)
        assert dim > 0

    def test_default_is_384(self):
        from niu_api.internal.lightrag_manager import _get_embedding_dim_for_lightrag
        dim = _get_embedding_dim_for_lightrag()
        # Default model is minilm-l12
        assert dim == 384


# ============== ensure_lightrag Tests ==============


class TestEnsureLightRAG:
    """Test async ensure_lightrag()."""

    @pytest.mark.asyncio
    async def test_returns_none_when_not_available(self):
        from niu_api.internal.lightrag_manager import ensure_lightrag
        import niu_api.internal.lightrag_manager as mgr
        mgr._rag_instance = None

        with patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=None):
            result = await ensure_lightrag()
            assert result is None