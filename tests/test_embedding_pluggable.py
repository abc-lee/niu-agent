"""
Tests for niu_api/internal/embedding.py

Config-driven pluggable embedding model selection, dimension reporting,
model switching, and backward compatibility.
"""

import json
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock


# ============== SUPPORTED_MODELS Tests ==============


class TestSupportedModels:
    """Test the SUPPORTED_MODELS registry."""

    def test_contains_required_models(self):
        from niu_api.internal.embedding import SUPPORTED_MODELS
        assert "bge-m3" in SUPPORTED_MODELS
        assert "minilm-l12" in SUPPORTED_MODELS

    def test_bge_m3_has_correct_dim(self):
        from niu_api.internal.embedding import SUPPORTED_MODELS
        assert SUPPORTED_MODELS["bge-m3"]["dim"] == 1024

    def test_minilm_l12_has_correct_dim(self):
        from niu_api.internal.embedding import SUPPORTED_MODELS
        assert SUPPORTED_MODELS["minilm-l12"]["dim"] == 384

    def test_each_model_has_required_keys(self):
        from niu_api.internal.embedding import SUPPORTED_MODELS
        required_keys = ["local_dir", "hf_id", "dim", "desc"]
        for name, info in SUPPORTED_MODELS.items():
            for key in required_keys:
                assert key in info, f"Model {name} missing key {key}"

    def test_bge_m3_local_dir_matches(self):
        from niu_api.internal.embedding import SUPPORTED_MODELS
        assert SUPPORTED_MODELS["bge-m3"]["local_dir"] == "bge-m3"

    def test_minilm_l12_local_dir_matches_existing(self):
        from niu_api.internal.embedding import SUPPORTED_MODELS
        assert SUPPORTED_MODELS["minilm-l12"]["local_dir"] == "paraphrase-multilingual-MiniLM-L12-v2"


# ============== Config Reading Tests ==============


class TestConfigReading:
    """Test _get_embedding_model_name() reading from preferences.json."""

    def test_default_when_no_prefs(self):
        from niu_api.internal.embedding import _get_embedding_model_name, DEFAULT_MODEL
        with patch("pathlib.Path.home", return_value=Path("/nonexistent")):
            result = _get_embedding_model_name()
            assert result == DEFAULT_MODEL
            assert result == "minilm-l12"

    def test_default_when_no_lightrag_section(self):
        from niu_api.internal.embedding import _get_embedding_model_name
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")
            with patch("pathlib.Path.home", return_value=Path(tmp)):
                # The function uses Path.home() / ".niu" / "preferences.json"
                # We need to patch the exact path construction
                with patch("niu_api.internal.embedding.Path") as mock_path:
                    mock_path.home.return_value = Path(tmp)
                    # Make Path(...) constructor work normally for other calls
                    mock_path.side_effect = lambda x: Path(x)
                    result = _get_embedding_model_name()
                    assert result == "minilm-l12"

    def test_returns_configured_model(self):
        from niu_api.internal.embedding import _get_embedding_model_name
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs = {"lightrag": {"embedding_model": "bge-m3"}}
            prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
            with patch("niu_api.internal.embedding.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)
                result = _get_embedding_model_name()
                assert result == "bge-m3"

    def test_falls_back_on_unknown_model(self):
        from niu_api.internal.embedding import _get_embedding_model_name, DEFAULT_MODEL
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs = {"lightrag": {"embedding_model": "unknown-model"}}
            prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
            with patch("niu_api.internal.embedding.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)
                result = _get_embedding_model_name()
                assert result == DEFAULT_MODEL

    def test_falls_back_on_empty_model(self):
        from niu_api.internal.embedding import _get_embedding_model_name, DEFAULT_MODEL
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs = {"lightrag": {"embedding_model": ""}}
            prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
            with patch("niu_api.internal.embedding.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)
                result = _get_embedding_model_name()
                assert result == DEFAULT_MODEL


# ============== get_embedding_dim Tests ==============


class TestGetEmbeddingDim:
    """Test get_embedding_dim() returns correct dimension."""

    def test_default_dim_is_384(self):
        from niu_api.internal.embedding import get_embedding_dim
        # Default is minilm-l12
        dim = get_embedding_dim()
        assert dim == 384

    def test_dim_matches_configured_model(self):
        from niu_api.internal.embedding import get_embedding_dim
        with patch("niu_api.internal.embedding._get_embedding_model_name", return_value="bge-m3"):
            dim = get_embedding_dim()
            assert dim == 1024


# ============== get_current_model_info Tests ==============


class TestGetCurrentModelInfo:
    """Test get_current_model_info() returns correct status."""

    def test_returns_dict_with_required_keys(self):
        from niu_api.internal.embedding import get_current_model_info
        info = get_current_model_info()
        assert "name" in info
        assert "dim" in info
        assert "desc" in info
        assert "loaded" in info

    def test_default_model_info(self):
        from niu_api.internal.embedding import get_current_model_info
        info = get_current_model_info()
        assert info["name"] == "minilm-l12"
        assert info["dim"] == 384
        assert info["loaded"] is False

    def test_loaded_status_after_model_load(self):
        from niu_api.internal.embedding import get_current_model_info, _model
        # Simulate model loaded
        with patch("niu_api.internal.embedding._model", MagicMock()):
            with patch("niu_api.internal.embedding._model_name", "minilm-l12"):
                info = get_current_model_info()
                assert info["loaded"] is True


# ============== switch_model Tests ==============


class TestSwitchModel:
    """Test switch_model() runtime model switching."""

    def test_rejects_unknown_model(self):
        from niu_api.internal.embedding import switch_model
        result = switch_model("nonexistent-model")
        assert result["status"] == "error"
        assert "Unknown model" in result["message"]

    def test_switches_to_valid_model(self):
        from niu_api.internal.embedding import switch_model
        with tempfile.TemporaryDirectory() as tmp:
            # Create a preferences.json in the temp dir
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

            with patch("niu_api.internal.embedding.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)

                result = switch_model("bge-m3")
                assert result["status"] == "switched"
                assert result["new_model"] == "bge-m3"
                assert result["new_dim"] == 1024
                assert result["needs_reindex"] is True  # 384 → 1024

    def test_same_dim_no_reindex_needed(self):
        from niu_api.internal.embedding import switch_model
        # Switching from minilm-l12 to minilm-l12 (same dim)
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs = {"lightrag": {"embedding_model": "minilm-l12"}}
            prefs_path.write_text(json.dumps(prefs), encoding="utf-8")

            with patch("niu_api.internal.embedding.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)

                result = switch_model("minilm-l12")
                assert result["needs_reindex"] is False

    def test_updates_preferences_json(self):
        from niu_api.internal.embedding import switch_model
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

            with patch("niu_api.internal.embedding.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)

                switch_model("bge-m3")

                # Verify preferences.json was updated
                updated_prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
                assert updated_prefs["lightrag"]["embedding_model"] == "bge-m3"

    def test_forces_model_unload(self):
        from niu_api.internal.embedding import switch_model, _model, _model_name
        import niu_api.internal.embedding as emb_module

        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

            with patch("niu_api.internal.embedding.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)

                # After switch, model should be unloaded
                result = switch_model("bge-m3")
                assert emb_module._model is None
                assert emb_module._model_name is None


# ============== Backward Compatibility Tests ==============


class TestBackwardCompat:
    """Test backward compatibility with existing vector store."""

    def test_default_model_is_minilm_l12(self):
        from niu_api.internal.embedding import DEFAULT_MODEL
        assert DEFAULT_MODEL == "minilm-l12"

    def test_default_dim_is_384(self):
        from niu_api.internal.embedding import get_embedding_dim
        assert get_embedding_dim() == 384

    def test_existing_model_dir_name_preserved(self):
        from niu_api.internal.embedding import SUPPORTED_MODELS
        # The local_dir must match the existing directory on disk
        assert SUPPORTED_MODELS["minilm-l12"]["local_dir"] == "paraphrase-multilingual-MiniLM-L12-v2"


# ============== Core Functions Tests ==============


class TestCoreFunctions:
    """Test encode, batch_encode, similarity with mock model."""

    def test_encode_returns_list_of_floats(self):
        from niu_api.internal.embedding import encode
        mock_model = MagicMock()
        mock_embedding = MagicMock()
        mock_embedding.tolist.return_value = [0.1, 0.2, 0.3]
        mock_model.encode.return_value = mock_embedding

        with patch("niu_api.internal.embedding.get_model", return_value=mock_model):
            result = encode("test text")
            assert isinstance(result, list)
            assert all(isinstance(x, float) for x in result)

    def test_batch_encode_returns_list_of_lists(self):
        from niu_api.internal.embedding import batch_encode
        mock_model = MagicMock()
        mock_embeddings = MagicMock()
        mock_embeddings.tolist.return_value = [[0.1, 0.2], [0.3, 0.4]]
        mock_model.encode.return_value = mock_embeddings

        with patch("niu_api.internal.embedding.get_model", return_value=mock_model):
            result = batch_encode(["text1", "text2"])
            assert isinstance(result, list)
            assert len(result) == 2
            assert all(isinstance(v, list) for v in result)

    def test_similarity_returns_float(self):
        from niu_api.internal.embedding import similarity
        # similarity uses numpy internally, mock the model
        mock_model = MagicMock()
        import numpy as np
        mock_model.encode.return_value = np.array([[1.0, 0.0], [0.0, 1.0]])

        with patch("niu_api.internal.embedding.get_model", return_value=mock_model):
            result = similarity("text1", "text2")
            assert isinstance(result, float)
            assert 0.0 <= result <= 1.0

    def test_similarity_vectors_orthogonal(self):
        from niu_api.internal.embedding import similarity_vectors
        result = similarity_vectors([1.0, 0.0], [0.0, 1.0])
        assert result == 0.0

    def test_similarity_vectors_identical(self):
        from niu_api.internal.embedding import similarity_vectors
        result = similarity_vectors([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        assert abs(result - 1.0) < 0.001

    def test_similarity_vectors_zero_vector(self):
        from niu_api.internal.embedding import similarity_vectors
        result = similarity_vectors([0.0, 0.0], [1.0, 2.0])
        assert result == 0.0


# ============== Lifecycle Tests ==============


class TestLifecycle:
    """Test preload and is_ready."""

    def test_is_ready_false_before_load(self):
        import niu_api.internal.embedding as emb_module
        # Reset module state
        emb_module._model = None
        from niu_api.internal.embedding import is_ready
        assert is_ready() is False

    def test_is_ready_true_after_load(self):
        import niu_api.internal.embedding as emb_module
        emb_module._model = MagicMock()
        from niu_api.internal.embedding import is_ready
        assert is_ready() is True
        # Clean up
        emb_module._model = None

    def test_get_models_dir_default(self):
        from niu_api.internal.embedding import get_models_dir
        with patch.dict("os.environ", {}, clear=True):
            # Remove NIU_MODELS_PATH if set
            result = get_models_dir()
            assert "models" in str(result)

    def test_get_models_dir_from_env(self):
        from niu_api.internal.embedding import get_models_dir
        with patch.dict("os.environ", {"NIU_MODELS_PATH": "/custom/models"}, clear=False):
            result = get_models_dir()
            # Windows uses backslashes, compare using Path equality
            assert result == Path("/custom/models")