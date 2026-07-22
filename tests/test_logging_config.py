"""测试 config/user-config.json 的 logging 子节点解析。

缺省情况下（user-config.json 不含 logging 字段或 logging.enabled=false），
所有日志输出应关闭（loguru sink、raw_http 两层日志、llm_interaction 可读日志、
im_adapter_stderr、http-log 服务）。只有显式 logging.enabled=true 才按 level 输出。
"""
import json


def test_config_no_logging_field_defaults_to_disabled(tmp_path, monkeypatch):
    """user-config.json 不含 logging 字段时，logging 默认 enabled=False"""
    from niu_api import config as cfg_mod

    cfg_file = tmp_path / "user-config.json"
    cfg_file.write_text(json.dumps({"llm": {"apikey": "x"}}), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(cfg_file))
    cfg_mod._config = None  # 重置单例

    cfg = cfg_mod.get_config()
    assert cfg.logging.enabled is False
    assert cfg.logging.level == "INFO"  # 默认 INFO


def test_config_logging_enabled_true(tmp_path, monkeypatch):
    """user-config.json 含 logging.enabled=true 时，logging.enabled 为 True"""
    from niu_api import config as cfg_mod

    cfg_file = tmp_path / "user-config.json"
    cfg_file.write_text(json.dumps({
        "llm": {"apikey": "x"},
        "logging": {"enabled": True, "level": "DEBUG"},
    }), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(cfg_file))
    cfg_mod._config = None

    cfg = cfg_mod.get_config()
    assert cfg.logging.enabled is True
    assert cfg.logging.level == "DEBUG"


def test_config_logging_enabled_missing_field_defaults_false(tmp_path, monkeypatch):
    """logging 字段存在但 enabled 字段缺失时，enabled=False"""
    from niu_api import config as cfg_mod

    cfg_file = tmp_path / "user-config.json"
    cfg_file.write_text(json.dumps({
        "llm": {"apikey": "x"},
        "logging": {"level": "INFO"},
    }), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(cfg_file))
    cfg_mod._config = None

    cfg = cfg_mod.get_config()
    assert cfg.logging.enabled is False
    assert cfg.logging.level == "INFO"


def test_get_logging_config_returns_logging_object(tmp_path, monkeypatch):
    """get_logging_config() 返回 logging 子对象"""
    from niu_api import config as cfg_mod

    cfg_file = tmp_path / "user-config.json"
    cfg_file.write_text(json.dumps({
        "llm": {"apikey": "x"},
        "logging": {"enabled": True, "level": "WARNING"},
    }), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(cfg_file))
    cfg_mod._config = None

    log_cfg = cfg_mod.get_logging_config()
    assert log_cfg.enabled is True
    assert log_cfg.level == "WARNING"


def test_get_logging_config_fallback_on_exception(monkeypatch):
    """Config 加载异常时，get_logging_config() 兜底返回 enabled=False"""
    from niu_api import config as cfg_mod

    def _raise(*_, **__):
        raise RuntimeError("config load failed")

    monkeypatch.setattr(cfg_mod, "get_config", _raise)

    log_cfg = cfg_mod.get_logging_config()
    assert log_cfg.enabled is False
    assert log_cfg.level == "INFO"


def test_config_file_not_found_defaults_disabled(tmp_path, monkeypatch):
    """config 文件不存在时，Config.load 内部 catch FileNotFoundError 返回默认 Config（logging=False）"""
    from niu_api import config as cfg_mod

    nonexistent = tmp_path / "nonexistent-config.json"
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(nonexistent))
    cfg_mod._config = None

    cfg = cfg_mod.get_config()
    assert cfg.logging.enabled is False
    assert cfg.logging.level == "INFO"
