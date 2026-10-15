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


# ============== Stale Reranker Self-Heal Tests ==============


def _capture_loguru(level="WARNING"):
    """loguru sink 捕获（reranker 用 loguru 而非 stdlib logging，pytest caplog 捕获不到——
    与 test_embedding_pluggable.py 同范式的 per-file 复制）。"""
    from loguru import logger

    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level=level)
    return messages, sink_id


def _write_prefs(tmp, prefs):
    """在 tmp/.niu/preferences.json 写入 prefs。"""
    prefs_path = Path(tmp) / ".niu" / "preferences.json"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
    return prefs_path


def _call_get_model_name_with_home(home_dir):
    """把 reranker 模块的 Path.home 指到 home_dir 后调 _get_reranker_model_name。"""
    from niu_api.internal.reranker import _get_reranker_model_name

    with patch("niu_api.internal.reranker.Path") as mock_path:
        mock_path.home.return_value = Path(home_dir)
        mock_path.side_effect = lambda x: Path(x)
        return _get_reranker_model_name()


class TestStaleRerankerSelfHeal:
    """陈旧 reranker_model 配置值：响亮告警 + 原子写回（进程内一次）+ 绝不抛。"""

    def test_stale_value_warns_and_heals_file(self, tmp_path, monkeypatch):
        """①返回生效值 ②有 WARNING ③文件键被改成生效值 ④其它字段未动。"""
        import niu_api.internal.reranker as reranker_mod

        monkeypatch.setattr(reranker_mod, "_stale_reranker_healed", False)
        prefs = {
            "version": "1.0",
            "lightrag": {"embedding_model": "embeddinggemma-2", "reranker_model": "bge-reranker-v1.5"},
            "feishu": {"enabled": True, "app_id": "cli-abc"},
        }
        prefs_path = _write_prefs(str(tmp_path), prefs)
        msgs, sink_id = _capture_loguru()
        try:
            result = _call_get_model_name_with_home(str(tmp_path))
        finally:
            from loguru import logger
            logger.remove(sink_id)

        assert result == "none", "不受支持值必须落到生效值 'none'"
        assert any("bge-reranker-v1.5" in m and "reranker_model" in m and "不受支持" in m for m in msgs), (
            f"必须响亮告警并带配置值/键名，实际: {msgs}"
        )
        healed = json.loads(prefs_path.read_text(encoding="utf-8"))
        assert healed["lightrag"]["reranker_model"] == "none", "自愈必须把键改成生效值 'none'"
        # 其它字段原样保留
        assert healed["version"] == "1.0"
        assert healed["lightrag"]["embedding_model"] == "embeddinggemma-2"
        assert healed["feishu"] == {"enabled": True, "app_id": "cli-abc"}

    def test_writeback_only_once_per_process(self, tmp_path, monkeypatch):
        """进程内已尝试过写回（含成功）⇒ 再次遇到陈旧值只告警、不再写盘。"""
        import niu_api.internal.reranker as reranker_mod

        monkeypatch.setattr(reranker_mod, "_stale_reranker_healed", True)  # 模拟本进程已尝试过
        prefs_path = _write_prefs(str(tmp_path), {"lightrag": {"reranker_model": "legacy-reranker"}})
        msgs, sink_id = _capture_loguru()
        try:
            result = _call_get_model_name_with_home(str(tmp_path))
        finally:
            from loguru import logger
            logger.remove(sink_id)

        assert result == "none"
        assert any("legacy-reranker" in m for m in msgs), "已尝试过写回仍要响亮告警"
        assert json.loads(prefs_path.read_text(encoding="utf-8"))["lightrag"]["reranker_model"] == "legacy-reranker", (
            "进程内只写一次：已尝试过 ⇒ 不得再写盘"
        )

    def test_write_failure_warns_and_keeps_effective(self, tmp_path, monkeypatch):
        """写回失败（只读/权限）⇒ 告警说明，不抛，行为不变（仍用生效值 'none'）。"""
        import niu_api.internal.reranker as reranker_mod

        monkeypatch.setattr(reranker_mod, "_stale_reranker_healed", False)

        def _boom(path, data):
            raise OSError("read-only file system")

        monkeypatch.setattr(reranker_mod, "_atomic_write_json", _boom)
        prefs_path = _write_prefs(str(tmp_path), {"lightrag": {"reranker_model": "bge-reranker-v1.5"}})
        msgs, sink_id = _capture_loguru()
        try:
            result = _call_get_model_name_with_home(str(tmp_path))  # 不得抛
        finally:
            from loguru import logger
            logger.remove(sink_id)

        assert result == "none"
        assert any("写回失败" in m for m in msgs), f"写回失败必须告警说明，实际: {msgs}"
        assert json.loads(prefs_path.read_text(encoding="utf-8"))["lightrag"]["reranker_model"] == "bge-reranker-v1.5"

    def test_missing_key_no_warning_no_writeback(self, tmp_path, monkeypatch):
        """键缺失 ⇒ 现状零变：不告警、不写回。"""
        import niu_api.internal.reranker as reranker_mod

        monkeypatch.setattr(reranker_mod, "_stale_reranker_healed", False)
        prefs_path = _write_prefs(str(tmp_path), {"version": "1.0", "lightrag": {"embedding_model": "embeddinggemma-2"}})
        original = prefs_path.read_text(encoding="utf-8")
        msgs, sink_id = _capture_loguru()
        try:
            result = _call_get_model_name_with_home(str(tmp_path))
        finally:
            from loguru import logger
            logger.remove(sink_id)

        assert result == "none"
        assert msgs == [], f"键缺失不得告警：{msgs}"
        assert prefs_path.read_text(encoding="utf-8") == original, "键缺失不得写回"

    def test_supported_value_no_warning_no_writeback(self, tmp_path, monkeypatch):
        """值受支持 ⇒ 原样返回，不告警、不写回（现状零变）。"""
        import niu_api.internal.reranker as reranker_mod

        monkeypatch.setattr(reranker_mod, "_stale_reranker_healed", False)
        prefs_path = _write_prefs(str(tmp_path), {"lightrag": {"reranker_model": "bge-reranker-v2-m3"}})
        original = prefs_path.read_text(encoding="utf-8")
        msgs, sink_id = _capture_loguru()
        try:
            result = _call_get_model_name_with_home(str(tmp_path))
        finally:
            from loguru import logger
            logger.remove(sink_id)

        assert result == "bge-reranker-v2-m3"
        assert msgs == []
        assert prefs_path.read_text(encoding="utf-8") == original

    def test_corrupt_json_returns_none_without_crash(self, tmp_path, monkeypatch):
        """坏 JSON 读失败 ⇒ 仍返回生效值 'none'、不抛、不写回。"""
        import niu_api.internal.reranker as reranker_mod

        monkeypatch.setattr(reranker_mod, "_stale_reranker_healed", False)
        prefs_path = _write_prefs(str(tmp_path), {})
        prefs_path.write_text("{ broken json }}}", encoding="utf-8")
        result = _call_get_model_name_with_home(str(tmp_path))  # 不得抛
        assert result == "none"
        assert prefs_path.read_text(encoding="utf-8") == "{ broken json }}}", "读失败不得写回"
