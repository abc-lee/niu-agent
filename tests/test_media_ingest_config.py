"""T1：`media_ingest.enabled` 开关的唯一读取点（方案 Task 1 Step 1）。

四种输入全部覆盖：空段（含配置无该段/空文件/文件缺失）、`enabled: true`、
坏 JSON、类型错误 —— 一律注入**临时** CONFIG_PATH，绝不读写真实 ~/.niu。
坏输入必须回默认 `False`（不抛），且每次调用现读（改配置免重启热生效）。
"""
import json

from niu_api.internal import media_ingest_config as mic


def _write_config(tmp_path, payload: str) -> str:
    p = tmp_path / "user-config.json"
    p.write_text(payload, encoding="utf-8")
    return str(p)


class TestMediaIngestEnabled:
    def test_no_section_defaults_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, "{}"))
        assert mic.media_ingest_enabled() is False

    def test_empty_file_defaults_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, ""))
        assert mic.media_ingest_enabled() is False

    def test_missing_file_defaults_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(tmp_path / "no-such.json"))
        assert mic.media_ingest_enabled() is False

    def test_enabled_true(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "niu_api.config.CONFIG_PATH",
            _write_config(tmp_path, json.dumps({"media_ingest": {"enabled": True}})),
        )
        assert mic.media_ingest_enabled() is True

    def test_enabled_false_explicit(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "niu_api.config.CONFIG_PATH",
            _write_config(tmp_path, json.dumps({"media_ingest": {"enabled": False}})),
        )
        assert mic.media_ingest_enabled() is False

    def test_broken_json_returns_false_not_raise(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, "{不是JSON"))
        assert mic.media_ingest_enabled() is False

    def test_section_wrong_type_returns_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "niu_api.config.CONFIG_PATH",
            _write_config(tmp_path, json.dumps({"media_ingest": "on"})),
        )
        assert mic.media_ingest_enabled() is False

    def test_value_wrong_type_returns_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "niu_api.config.CONFIG_PATH",
            _write_config(tmp_path, json.dumps({"media_ingest": {"enabled": "yes"}})),
        )
        assert mic.media_ingest_enabled() is False

    def test_top_level_wrong_type_returns_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, "[1,2]"))
        assert mic.media_ingest_enabled() is False

    def test_reread_each_call_is_hot(self, monkeypatch, tmp_path):
        """每次调用现读 → 改配置免重启热生效（spec §5.4 生效时机的前提）。"""
        p = tmp_path / "user-config.json"
        p.write_text(json.dumps({"media_ingest": {"enabled": False}}), encoding="utf-8")
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(p))
        assert mic.media_ingest_enabled() is False
        p.write_text(json.dumps({"media_ingest": {"enabled": True}}), encoding="utf-8")
        assert mic.media_ingest_enabled() is True
