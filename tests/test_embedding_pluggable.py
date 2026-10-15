"""
Tests for niu_api/internal/embedding.py

Config-driven pluggable embedding model selection, dimension reporting,
model switching, and backward compatibility.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# ============== SUPPORTED_MODELS Tests ==============


class TestSupportedModels:
    """Test the SUPPORTED_MODELS registry."""

    def test_contains_required_models(self):
        from niu_api.internal.embedding import SUPPORTED_MODELS
        assert "embeddinggemma-2" in SUPPORTED_MODELS
        assert "bge-base-zh-v1.5" not in SUPPORTED_MODELS
        assert "bge-m3" not in SUPPORTED_MODELS
        assert "minilm-l12" not in SUPPORTED_MODELS

    def test_each_model_has_required_keys(self):
        from niu_api.internal.embedding import SUPPORTED_MODELS
        required_keys = ["local_dir", "hf_id", "dim", "desc"]
        for name, info in SUPPORTED_MODELS.items():
            for key in required_keys:
                assert key in info, f"Model {name} missing key {key}"


# ============== Config Reading Tests ==============


class TestConfigReading:
    """Test _get_embedding_model_name() reading from preferences.json."""

    def test_default_when_no_prefs(self):
        from niu_api.internal.embedding import DEFAULT_MODEL, _get_embedding_model_name
        with patch("pathlib.Path.home", return_value=Path("/nonexistent")):
            result = _get_embedding_model_name()
            assert result == DEFAULT_MODEL
            assert result == "embeddinggemma-2"

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
                    assert result == "embeddinggemma-2"

    def test_returns_configured_model(self):
        from niu_api.internal.embedding import _get_embedding_model_name
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs = {"lightrag": {"embedding_model": "embeddinggemma-2"}}
            prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
            with patch("niu_api.internal.embedding.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)
                result = _get_embedding_model_name()
                assert result == "embeddinggemma-2"

    def test_falls_back_on_unknown_model(self):
        from niu_api.internal.embedding import DEFAULT_MODEL, _get_embedding_model_name
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / ".niu" / "preferences.json"
            prefs_path.parent.mkdir(parents=True)
            prefs = {"lightrag": {"embedding_model": "legacy-bge"}}
            prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
            with patch("niu_api.internal.embedding.Path") as mock_path:
                mock_path.home.return_value = Path(tmp)
                mock_path.side_effect = lambda x: Path(x)
                result = _get_embedding_model_name()
                assert result == DEFAULT_MODEL

    def test_falls_back_on_empty_model(self):
        from niu_api.internal.embedding import DEFAULT_MODEL, _get_embedding_model_name
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

    def test_default_dim_is_768(self):
        from niu_api.internal.embedding import get_embedding_dim
        # Default is bge-base-zh-v1.5
        dim = get_embedding_dim()
        assert dim == 768

    def test_dim_matches_configured_model(self):
        from niu_api.internal.embedding import get_embedding_dim
        with patch("niu_api.internal.embedding._get_embedding_model_name", return_value="embeddinggemma-2"):
            dim = get_embedding_dim()
            assert dim == 768


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
        assert info["name"] == "embeddinggemma-2"
        assert info["dim"] == 768
        # loaded may be True if another test loaded the model
        assert isinstance(info["loaded"], bool)

    def test_loaded_status_after_model_load(self):
        from niu_api.internal.embedding import get_current_model_info
        # Simulate model loaded
        with patch("niu_api.internal.embedding._model", MagicMock()):
            with patch("niu_api.internal.embedding._model_name", "minilm-l12"):
                info = get_current_model_info()
                assert info["loaded"] is True


# ============== Backward Compatibility Tests ==============


class TestBackwardCompat:
    """Test backward compatibility with existing vector store."""

    def test_default_model_is_embeddinggemma_2(self):
        from niu_api.internal.embedding import DEFAULT_MODEL
        assert DEFAULT_MODEL == "embeddinggemma-2"

    def test_default_dim_is_768(self):
        from niu_api.internal.embedding import get_embedding_dim
        assert get_embedding_dim() == 768


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
        with emb_module._model_lock:
            old_model = emb_module._model
            emb_module._model = None
        try:
            from niu_api.internal.embedding import is_ready
            assert is_ready() is False
        finally:
            with emb_module._model_lock:
                emb_module._model = old_model

    def test_is_ready_true_after_load(self):
        import niu_api.internal.embedding as emb_module
        with emb_module._model_lock:
            old_model = emb_module._model
            emb_module._model = MagicMock()
        try:
            from niu_api.internal.embedding import is_ready
            assert is_ready() is True
        finally:
            with emb_module._model_lock:
                emb_module._model = old_model

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


# ============== Stale Model Self-Heal Tests ==============


def _capture_loguru(level="WARNING"):
    """loguru sink 捕获（embedding 用 loguru 而非 stdlib logging，pytest caplog 捕获不到——
    同 test_md_f3.py / test_gemma_tokenizer.py 的 per-file 复制模式）。"""
    from loguru import logger

    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level=level)
    return messages, sink_id


def _write_prefs(tmp, prefs):
    """在 tmp/.niu/preferences.json 写入 prefs（conftest 已建 tmp/.niu）。"""
    prefs_path = Path(tmp) / ".niu" / "preferences.json"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
    return prefs_path


def _call_get_model_name_with_home(home_dir):
    """把 embedding 模块的 Path.home 指到 home_dir 后调 _get_embedding_model_name。"""
    from niu_api.internal.embedding import _get_embedding_model_name

    with patch("niu_api.internal.embedding.Path") as mock_path:
        mock_path.home.return_value = Path(home_dir)
        mock_path.side_effect = lambda x: Path(x)
        return _get_embedding_model_name()


class TestStaleModelSelfHeal:
    """陈旧 embedding_model 配置值：响亮告警 + 原子写回（进程内一次）+ 绝不抛。"""

    def test_stale_value_warns_and_heals_file(self, tmp_path, monkeypatch):
        """①返回生效值 ②有 WARNING ③文件键被改成生效值 ④其它字段未动。"""
        import niu_api.internal.embedding as emb
        from niu_api.internal.embedding import DEFAULT_MODEL

        monkeypatch.setattr(emb, "_stale_model_healed", False)
        prefs = {
            "version": "1.0",
            "lightrag": {"embedding_model": "bge-base-zh-v1.5", "reranker_model": "none"},
            "feishu": {"enabled": True, "app_id": "cli-abc"},
        }
        prefs_path = _write_prefs(str(tmp_path), prefs)
        msgs, sink_id = _capture_loguru()
        try:
            result = _call_get_model_name_with_home(str(tmp_path))
        finally:
            from loguru import logger
            logger.remove(sink_id)

        assert result == DEFAULT_MODEL, "不受支持值必须落到生效值"
        assert any("bge-base-zh-v1.5" in m and "embedding_model" in m and "不受支持" in m for m in msgs), (
            f"必须响亮告警并带配置值/键名，实际: {msgs}"
        )
        healed = json.loads(prefs_path.read_text(encoding="utf-8"))
        assert healed["lightrag"]["embedding_model"] == DEFAULT_MODEL, "自愈必须把键改成生效值"
        # 其它字段原样保留
        assert healed["version"] == "1.0"
        assert healed["lightrag"]["reranker_model"] == "none"
        assert healed["feishu"] == {"enabled": True, "app_id": "cli-abc"}

    def test_writeback_only_once_per_process(self, tmp_path, monkeypatch):
        """进程内已尝试过写回（含成功）⇒ 再次遇到陈旧值只告警、不再写盘。"""
        import niu_api.internal.embedding as emb
        from niu_api.internal.embedding import DEFAULT_MODEL

        monkeypatch.setattr(emb, "_stale_model_healed", True)  # 模拟本进程已尝试过
        prefs_path = _write_prefs(str(tmp_path), {"lightrag": {"embedding_model": "legacy-bge"}})
        msgs, sink_id = _capture_loguru()
        try:
            result = _call_get_model_name_with_home(str(tmp_path))
        finally:
            from loguru import logger
            logger.remove(sink_id)

        assert result == DEFAULT_MODEL
        assert any("legacy-bge" in m for m in msgs), "已尝试过写回仍要响亮告警"
        assert json.loads(prefs_path.read_text(encoding="utf-8"))["lightrag"]["embedding_model"] == "legacy-bge", (
            "进程内只写一次：已尝试过 ⇒ 不得再写盘"
        )

    def test_write_failure_warns_and_keeps_effective(self, tmp_path, monkeypatch):
        """写回失败（只读/权限/坏 JSON）⇒ 告警说明，不抛，行为不变（仍用生效值）。"""
        import niu_api.internal.embedding as emb
        from niu_api.internal.embedding import DEFAULT_MODEL

        monkeypatch.setattr(emb, "_stale_model_healed", False)

        def _boom(path, data):
            raise OSError("read-only file system")

        monkeypatch.setattr(emb, "_atomic_write_json", _boom)
        prefs_path = _write_prefs(str(tmp_path), {"lightrag": {"embedding_model": "bge-base-zh-v1.5"}})
        msgs, sink_id = _capture_loguru()
        try:
            result = _call_get_model_name_with_home(str(tmp_path))  # 不得抛
        finally:
            from loguru import logger
            logger.remove(sink_id)

        assert result == DEFAULT_MODEL
        assert any("写回失败" in m for m in msgs), f"写回失败必须告警说明，实际: {msgs}"
        assert json.loads(prefs_path.read_text(encoding="utf-8"))["lightrag"]["embedding_model"] == "bge-base-zh-v1.5"

    def test_missing_key_no_warning_no_writeback(self, tmp_path, monkeypatch):
        """键缺失 ⇒ 现状零变：不告警、不写回。"""
        import niu_api.internal.embedding as emb
        from niu_api.internal.embedding import DEFAULT_MODEL

        monkeypatch.setattr(emb, "_stale_model_healed", False)
        prefs_path = _write_prefs(str(tmp_path), {"version": "1.0"})
        original = prefs_path.read_text(encoding="utf-8")
        msgs, sink_id = _capture_loguru()
        try:
            result = _call_get_model_name_with_home(str(tmp_path))
        finally:
            from loguru import logger
            logger.remove(sink_id)

        assert result == DEFAULT_MODEL
        assert msgs == [], f"键缺失不得告警：{msgs}"
        assert prefs_path.read_text(encoding="utf-8") == original, "键缺失不得写回"

    def test_supported_value_no_warning_no_writeback(self, tmp_path, monkeypatch):
        """值受支持 ⇒ 原样返回，不告警、不写回（现状零变）。"""
        import niu_api.internal.embedding as emb
        from niu_api.internal.embedding import DEFAULT_MODEL

        monkeypatch.setattr(emb, "_stale_model_healed", False)
        prefs_path = _write_prefs(str(tmp_path), {"lightrag": {"embedding_model": "embeddinggemma-2"}})
        original = prefs_path.read_text(encoding="utf-8")
        msgs, sink_id = _capture_loguru()
        try:
            result = _call_get_model_name_with_home(str(tmp_path))
        finally:
            from loguru import logger
            logger.remove(sink_id)

        assert result == "embeddinggemma-2" == DEFAULT_MODEL
        assert msgs == []
        assert prefs_path.read_text(encoding="utf-8") == original

    def test_corrupt_json_returns_default_without_crash(self, tmp_path, monkeypatch):
        """坏 JSON 读失败 ⇒ 仍返回生效值、不抛、不写回。"""
        import niu_api.internal.embedding as emb
        from niu_api.internal.embedding import DEFAULT_MODEL

        monkeypatch.setattr(emb, "_stale_model_healed", False)
        prefs_path = _write_prefs(str(tmp_path), {})
        prefs_path.write_text("{ broken json }}}", encoding="utf-8")
        result = _call_get_model_name_with_home(str(tmp_path))  # 不得抛
        assert result == DEFAULT_MODEL
        assert prefs_path.read_text(encoding="utf-8") == "{ broken json }}}", "读失败不得写回"


# ============== Template Consistency Gate ==============


class TestTemplatePreferencesConsistency:
    """模板一致性门禁：memory/preferences.json（安装/重置模板）与代码支持表不许漂移。

    覆盖 lightrag.embedding_model（SUPPORTED_MODELS）与 lightrag.reranker_model
    （SUPPORTED_RERANKERS，跨模块）两个键。
    """

    def test_template_embedding_model_in_supported_models(self):
        from niu_api.internal.embedding import SUPPORTED_MODELS

        template = Path(__file__).resolve().parent.parent / "memory" / "preferences.json"
        assert template.exists(), "memory/preferences.json 模板缺失"
        data = json.loads(template.read_text(encoding="utf-8"))
        model = data.get("lightrag", {}).get("embedding_model", "")
        assert model in SUPPORTED_MODELS, (
            f"模板 lightrag.embedding_model={model!r} 不在代码支持表 {sorted(SUPPORTED_MODELS)} 内——"
            f"模板与代码再次漂移"
        )

    def test_template_reranker_model_in_supported_rerankers(self):
        from niu_api.internal.reranker import SUPPORTED_RERANKERS

        template = Path(__file__).resolve().parent.parent / "memory" / "preferences.json"
        assert template.exists(), "memory/preferences.json 模板缺失"
        data = json.loads(template.read_text(encoding="utf-8"))
        reranker_model = data.get("lightrag", {}).get("reranker_model", "")
        if not reranker_model:
            # 模板没有 lightrag.reranker_model 键 ⇒ 无值可校验，跳过断言（缺键不视为漂移）。
            return
        assert reranker_model in SUPPORTED_RERANKERS, (
            f"模板 lightrag.reranker_model={reranker_model!r} 不在代码支持表 {sorted(SUPPORTED_RERANKERS)} 内——"
            f"模板与代码再次漂移"
        )
