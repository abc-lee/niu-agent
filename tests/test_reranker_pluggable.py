"""
Tests for niu_api/internal/reranker.py

Config-driven pluggable reranker model selection, lazy loading,
LightRAG callable creation, and runtime switching.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# ============== SUPPORTED_RERANKERS Tests ==============


class TestSupportedRerankers:
    """Test the SUPPORTED_RERANKERS registry."""

    def test_contains_required_models(self):
        from niu_api.internal.reranker import SUPPORTED_RERANKERS
        assert "bge-reranker-v2-m3" in SUPPORTED_RERANKERS
        assert "bge-reranker-v2-gemma" in SUPPORTED_RERANKERS
        assert "none" in SUPPORTED_RERANKERS

    def test_each_model_has_required_keys(self):
        from niu_api.internal.reranker import SUPPORTED_RERANKERS
        required_keys = ["local_dir", "hf_id", "desc"]
        for name, info in SUPPORTED_RERANKERS.items():
            for key in required_keys:
                assert key in info, f"Reranker {name} missing key {key}"

    def test_none_model_has_empty_paths(self):
        from niu_api.internal.reranker import SUPPORTED_RERANKERS
        assert SUPPORTED_RERANKERS["none"]["local_dir"] == ""
        assert SUPPORTED_RERANKERS["none"]["hf_id"] == ""


# ============== Config Reading Tests ==============


class TestConfigReading:
    """Test _get_reranker_model_name() reading from preferences.json."""

    def test_default_is_none(self):
        from niu_api.internal.reranker import _get_reranker_model_name
        with patch("niu_api.internal.reranker.Path") as mock_path:
            mock_path.home.return_value = Path("/nonexistent")
            mock_path.side_effect = lambda x: Path(x)
            result = _get_reranker_model_name()
            assert result == "none"

    def test_returns_configured_model(self):
        from niu_api.internal.reranker import _get_reranker_model_name
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs = {"lightrag": {"reranker_model": "bge-reranker-v2-m3"}}
            prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
            with patch("niu_api.internal.reranker.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)
                result = _get_reranker_model_name()
                assert result == "bge-reranker-v2-m3"

    def test_falls_back_on_unknown_model(self):
        from niu_api.internal.reranker import _get_reranker_model_name
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs = {"lightrag": {"reranker_model": "unknown"}}
            prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
            with patch("niu_api.internal.reranker.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)
                result = _get_reranker_model_name()
                assert result == "none"


# ============== get_reranker Tests ==============


class TestGetReranker:
    """Test get_reranker() lazy loading behavior."""

    def test_returns_none_when_disabled(self):
        import niu_api.internal.reranker as reranker_module
        from niu_api.internal.reranker import get_reranker
        reranker_module._reranker_model = None
        reranker_module._reranker_name = None

        with patch("niu_api.internal.reranker._get_reranker_model_name", return_value="none"):
            result = get_reranker()
            assert result is None

    def test_returns_none_when_config_is_none(self):
        import niu_api.internal.reranker as reranker_module
        from niu_api.internal.reranker import get_reranker
        reranker_module._reranker_model = None
        reranker_module._reranker_name = None

        with patch("niu_api.internal.reranker._get_reranker_model_name", return_value="none"):
            result = get_reranker()
            assert result is None

    def test_returns_cached_model_when_same(self):
        import niu_api.internal.reranker as reranker_module
        from niu_api.internal.reranker import get_reranker
        mock_model = MagicMock()
        reranker_module._reranker_model = mock_model
        reranker_module._reranker_name = "bge-reranker-v2-m3"

        with patch("niu_api.internal.reranker._get_reranker_model_name", return_value="bge-reranker-v2-m3"):
            result = get_reranker()
            assert result == mock_model

        # Clean up
        reranker_module._reranker_model = None
        reranker_module._reranker_name = None


# ============== rerank Tests ==============


class TestRerank:
    """Test rerank() function behavior."""

    def test_returns_dummy_scores_when_disabled(self):
        from niu_api.internal.reranker import rerank
        with patch("niu_api.internal.reranker.get_reranker", return_value=None):
            result = rerank("query", ["doc1", "doc2", "doc3"], top_k=2)
            assert len(result) == 2
            assert result[0]["score"] > result[1]["score"]

    def test_rerank_with_mock_model(self):
        from niu_api.internal.reranker import rerank
        mock_model = MagicMock()
        import numpy as np
        mock_model.predict.return_value = np.array([0.9, 0.5, 0.7])

        with patch("niu_api.internal.reranker.get_reranker", return_value=mock_model):
            result = rerank("query", ["doc1", "doc2", "doc3"], top_k=2)
            assert len(result) == 2
            # Should be sorted by score descending
            assert result[0]["score"] > result[1]["score"]
            assert result[0]["text"] == "doc1"  # score 0.9

    def test_rerank_returns_text_and_index(self):
        from niu_api.internal.reranker import rerank
        mock_model = MagicMock()
        import numpy as np
        mock_model.predict.return_value = np.array([0.3, 0.8])

        with patch("niu_api.internal.reranker.get_reranker", return_value=mock_model):
            result = rerank("query", ["doc1", "doc2"], top_k=2)
            for item in result:
                assert "index" in item
                assert "text" in item
                assert "score" in item


# ============== make_lightrag_reranker_callable Tests ==============


class TestLightragCallable:
    """Test make_lightrag_reranker_callable() for LightRAG integration."""

    def test_returns_none_when_disabled(self):
        from niu_api.internal.reranker import make_lightrag_reranker_callable
        with patch("niu_api.internal.reranker._get_reranker_model_name", return_value="none"):
            result = make_lightrag_reranker_callable()
            assert result is None

    def test_returns_callable_when_enabled(self):
        from niu_api.internal.reranker import make_lightrag_reranker_callable
        with patch("niu_api.internal.reranker._get_reranker_model_name", return_value="bge-reranker-v2-m3"):
            result = make_lightrag_reranker_callable()
            assert result is not None
            assert callable(result)

    def test_callable_returns_tuples(self):
        from niu_api.internal.reranker import make_lightrag_reranker_callable
        mock_model = MagicMock()
        import numpy as np
        mock_model.predict.return_value = np.array([0.9, 0.5, 0.7])

        with patch("niu_api.internal.reranker._get_reranker_model_name", return_value="bge-reranker-v2-m3"):
            func = make_lightrag_reranker_callable()

        with patch("niu_api.internal.reranker.get_reranker", return_value=mock_model):
            result = func("query", ["doc1", "doc2", "doc3"])
            assert isinstance(result, list)
            for item in result:
                assert isinstance(item, tuple)
                assert len(item) == 2  # (index, score)

    def test_callable_returns_dummy_when_no_model(self):
        from niu_api.internal.reranker import make_lightrag_reranker_callable
        with patch("niu_api.internal.reranker._get_reranker_model_name", return_value="bge-reranker-v2-m3"):
            func = make_lightrag_reranker_callable()

        with patch("niu_api.internal.reranker.get_reranker", return_value=None):
            result = func("query", ["doc1", "doc2"])
            assert len(result) == 2
            for item in result:
                assert isinstance(item, tuple)


# ============== get_current_reranker_info Tests ==============


class TestGetCurrentRerankerInfo:
    """Test get_current_reranker_info() diagnostics."""

    def test_returns_dict_with_required_keys(self):
        from niu_api.internal.reranker import get_current_reranker_info
        info = get_current_reranker_info()
        assert "name" in info
        assert "desc" in info
        assert "loaded" in info

    def test_default_is_none_not_loaded(self):
        from niu_api.internal.reranker import get_current_reranker_info
        info = get_current_reranker_info()
        assert info["name"] == "none"
        assert info["loaded"] is False


# ============== switch_reranker Tests ==============


class TestSwitchReranker:
    """Test switch_reranker() runtime switching."""

    def test_rejects_unknown_model(self):
        from niu_api.internal.reranker import switch_reranker
        result = switch_reranker("nonexistent")
        assert result["status"] == "error"
        assert "Unknown model" in result["message"]

    def test_switches_to_valid_model(self):
        from niu_api.internal.reranker import switch_reranker
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

            with patch("niu_api.internal.reranker.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)

                result = switch_reranker("bge-reranker-v2-m3")
                assert result["status"] == "switched"
                assert result["new_model"] == "bge-reranker-v2-m3"

    def test_switches_to_none(self):
        from niu_api.internal.reranker import switch_reranker
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs = {"lightrag": {"reranker_model": "bge-reranker-v2-m3"}}
            prefs_path.write_text(json.dumps(prefs), encoding="utf-8")

            with patch("niu_api.internal.reranker.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)

                result = switch_reranker("none")
                assert result["status"] == "switched"
                assert result["new_model"] == "none"

    def test_updates_preferences_json(self):
        from niu_api.internal.reranker import switch_reranker
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

            with patch("niu_api.internal.reranker.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)

                switch_reranker("bge-reranker-v2-m3")

                updated_prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
                assert updated_prefs["lightrag"]["reranker_model"] == "bge-reranker-v2-m3"

    def test_forces_model_unload(self):
        import niu_api.internal.reranker as reranker_module
        from niu_api.internal.reranker import switch_reranker

        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

            with patch("niu_api.internal.reranker.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)

                switch_reranker("bge-reranker-v2-m3")
                assert reranker_module._reranker_model is None
                assert reranker_module._reranker_name is None
