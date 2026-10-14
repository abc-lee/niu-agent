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


class TestMediaIngestMaxPixels:
    def test_missing_key_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, "{}"))
        assert mic.media_ingest_max_pixels() == mic.DEFAULT_MAX_PIXELS

    def test_explicit_value(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "niu_api.config.CONFIG_PATH",
            _write_config(tmp_path, json.dumps({"media_ingest": {"max_pixels": 12345}})),
        )
        assert mic.media_ingest_max_pixels() == 12345

    def test_wrong_types_default(self, monkeypatch, tmp_path):
        for bad in ["8000000", 8000000.0, True, 0, -5, None, []]:
            monkeypatch.setattr(
                "niu_api.config.CONFIG_PATH",
                _write_config(tmp_path, json.dumps({"media_ingest": {"max_pixels": bad}})),
            )
            assert mic.media_ingest_max_pixels() == mic.DEFAULT_MAX_PIXELS

    def test_broken_json_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, "{不是JSON"))
        assert mic.media_ingest_max_pixels() == mic.DEFAULT_MAX_PIXELS

    def test_missing_file_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(tmp_path / "no-such.json"))
        assert mic.media_ingest_max_pixels() == mic.DEFAULT_MAX_PIXELS

    def test_reread_each_call_is_hot(self, monkeypatch, tmp_path):
        p = tmp_path / "user-config.json"
        p.write_text(json.dumps({"media_ingest": {"max_pixels": 1000}}), encoding="utf-8")
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(p))
        assert mic.media_ingest_max_pixels() == 1000
        p.write_text(json.dumps({"media_ingest": {"max_pixels": 2000}}), encoding="utf-8")
        assert mic.media_ingest_max_pixels() == 2000  # 改配置免重启生效（I-5）

    def test_shape_top_level_array_defaults(self, monkeypatch, tmp_path):
        """形态层：顶层 JSON 为数组 ⇒ `isinstance` 卫句兜住，回保守默认、绝不抛（与 `TestMediaIngestEnabled`
        的 `test_top_level_wrong_type_returns_false` 同型）。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, "[1,2]"))
        assert mic.media_ingest_max_pixels() == mic.DEFAULT_MAX_PIXELS

    def test_shape_top_level_string_defaults(self, monkeypatch, tmp_path):
        """形态层：顶层 JSON 为字符串（含超大字符串数字）⇒ 回保守默认、绝不抛（数值层无上限，但非整数形态一律默认）。"""
        for bad in ("\"8000000\"", "\"10000000000000000000\""):
            monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, bad))
            assert mic.media_ingest_max_pixels() == mic.DEFAULT_MAX_PIXELS

    def test_shape_section_non_dict_defaults(self, monkeypatch, tmp_path):
        """形态层：`media_ingest` 键存在但值为非 dict（字符串）⇒ `isinstance` 卫句兜住，回保守默认、绝不抛。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, json.dumps({"media_ingest": "x"})))
        assert mic.media_ingest_max_pixels() == mic.DEFAULT_MAX_PIXELS


class TestMediaIngestDownscaleLongSide:
    def test_missing_key_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, "{}"))
        assert mic.media_ingest_downscale_long_side() == mic.DEFAULT_DOWNSCALE_LONG_SIDE

    def test_explicit_value(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "niu_api.config.CONFIG_PATH",
            _write_config(tmp_path, json.dumps({"media_ingest": {"downscale_long_side": 2048}})),
        )
        assert mic.media_ingest_downscale_long_side() == 2048

    def test_wrong_types_default(self, monkeypatch, tmp_path):
        for bad in ["1024", 1024.0, False, 0, -1, None]:
            monkeypatch.setattr(
                "niu_api.config.CONFIG_PATH",
                _write_config(tmp_path, json.dumps({"media_ingest": {"downscale_long_side": bad}})),
            )
            assert mic.media_ingest_downscale_long_side() == mic.DEFAULT_DOWNSCALE_LONG_SIDE

    def test_broken_json_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, "{坏"))
        assert mic.media_ingest_downscale_long_side() == mic.DEFAULT_DOWNSCALE_LONG_SIDE

    def test_shape_top_level_array_defaults(self, monkeypatch, tmp_path):
        """形态层：顶层 JSON 为数组 ⇒ `isinstance` 卫句兜住，回保守默认、绝不抛。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, "[1,2]"))
        assert mic.media_ingest_downscale_long_side() == mic.DEFAULT_DOWNSCALE_LONG_SIDE

    def test_shape_top_level_string_defaults(self, monkeypatch, tmp_path):
        """形态层：顶层 JSON 为字符串（含超大字符串数字）⇒ 回保守默认、绝不抛。"""
        for bad in ("\"1024\"", "\"10000000000000000000\""):
            monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, bad))
            assert mic.media_ingest_downscale_long_side() == mic.DEFAULT_DOWNSCALE_LONG_SIDE

    def test_shape_section_non_dict_defaults(self, monkeypatch, tmp_path):
        """形态层：`media_ingest` 键存在但值为非 dict（字符串）⇒ `isinstance` 卫句兜住，回保守默认、绝不抛。"""
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, json.dumps({"media_ingest": "x"})))
        assert mic.media_ingest_downscale_long_side() == mic.DEFAULT_DOWNSCALE_LONG_SIDE
