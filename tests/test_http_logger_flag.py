"""测试 raw_http transport 层日志在 logging.enabled=false 时不写文件。

agent/generic/http_logger.py 的 install_http_logger() 现状是模块导入时无条件 patch
HTTP client。整改后：install_http_logger() 入口检查 logging.enabled，false 时不 patch。
install_http_logger() 加 _patched 幂等守卫（在 flag gate 之后、_do_patch_http 之前），
重复调用只 patch 一次（防 original_post 指向已 patch 版本形成递归）。
"""
import json
from unittest import mock


def _setup_config(tmp_path, monkeypatch, logging_enabled: bool):
    """在 tmp_path 下生成临时 user-config.json 并 monkeypatch CONFIG_PATH"""
    from niu_api import config as cfg_mod

    cfg_file = tmp_path / "user-config.json"
    cfg_file.write_text(json.dumps({
        "llm": {"apikey": "x"},
        "logging": {"enabled": logging_enabled, "level": "INFO"},
    }), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(cfg_file))
    if hasattr(cfg_mod, "_config"):
        cfg_mod._config = None
    cfg_mod.get_config()  # 预热单例


def test_install_http_logger_skips_when_logging_disabled(tmp_path, monkeypatch):
    """logging.enabled=false 时，install_http_logger 不 patch HTTP client"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=False)

    import agent.generic.http_logger as hl_mod
    hl_mod._patched = False  # 重置幂等标志

    patched_called = mock.MagicMock()
    # raising=False：Step 3 写测试时 _do_patch_http 还不存在（Step 5 才实现），
    # 默认 raising=True 会 AttributeError 让测试错误信息不清晰
    monkeypatch.setattr(hl_mod, "_do_patch_http", patched_called, raising=False)

    hl_mod.install_http_logger()
    assert patched_called.called is False, "logging.enabled=false 时不应该 patch HTTP"


def test_install_http_logger_patches_when_logging_enabled(tmp_path, monkeypatch):
    """logging.enabled=true 时，install_http_logger 正常 patch HTTP client"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=True)

    import agent.generic.http_logger as hl_mod
    hl_mod._patched = False

    patched_called = mock.MagicMock()
    monkeypatch.setattr(hl_mod, "_do_patch_http", patched_called, raising=False)

    hl_mod.install_http_logger()
    assert patched_called.called is True, "logging.enabled=true 时应该 patch HTTP"


def test_install_http_logger_idempotent(tmp_path, monkeypatch):
    """重复调 install_http_logger 不应多次 patch（防递归）。

    幂等守卫放在 install_http_logger 入口（flag gate 之后），所以 mock _do_patch_http
    后仍能验证守卫——_patched=True 时 install_http_logger 直接 return 不调 _do_patch_http。
    """
    _setup_config(tmp_path, monkeypatch, logging_enabled=True)

    import agent.generic.http_logger as hl_mod
    hl_mod._patched = False

    call_count = mock.MagicMock()
    monkeypatch.setattr(hl_mod, "_do_patch_http", call_count, raising=False)

    hl_mod.install_http_logger()
    hl_mod.install_http_logger()  # 第二次
    hl_mod.install_http_logger()  # 第三次
    assert call_count.call_count == 1, f"幂等守卫失败，_do_patch_http 被调了 {call_count.call_count} 次"


def test_write_log_entry_skipped_when_logging_disabled(tmp_path, monkeypatch):
    """logging.enabled=false 时，_write_log_entry 不写文件"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=False)

    import agent.generic.http_logger as hl_mod

    # _get_log_dir 是 http_logger.py 现有模块级函数（L27-32），直接 monkeypatch 替换
    fake_dir = tmp_path / "fake_logs"
    monkeypatch.setattr(hl_mod, "_get_log_dir", lambda: fake_dir)

    # _write_log_entry 真实签名是 (seq: int, entry: dict)
    hl_mod._write_log_entry(1, {"test": "data"})
    assert not fake_dir.exists(), "logging.enabled=false 时不应写 raw_http 日志文件"


def test_write_raw_log_skipped_when_logging_disabled(tmp_path, monkeypatch):
    """logging.enabled=false 时，_write_raw_log 不写 {seq}_request.json / _response.json"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=False)

    import agent.generic.litellm_adapter as la_mod

    # monkeypatch 新增的 _get_app_log_dir 函数（litellm_adapter 用绝对路径，chdir 无效）
    fake_dir = tmp_path / "fake_app_logs"
    monkeypatch.setattr(la_mod, "_get_app_log_dir", lambda: fake_dir, raising=False)

    # _write_raw_log 真实签名是 (log_type, data, seq=None)
    la_mod._write_raw_log("request", {"test": "data"}, seq=1)
    la_mod._write_raw_log("response", {"test": "data"}, seq=1)

    if fake_dir.exists():
        files = list(fake_dir.rglob("*.json"))
        assert files == [], f"logging.enabled=false 时不应写应用层 raw_http 日志，但找到 {files}"


def test_write_interaction_log_skipped_when_logging_disabled(tmp_path, monkeypatch):
    """logging.enabled=false 时，_write_interaction_log 不写 llm_interaction_*.log"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=False)

    import agent.generic.litellm_adapter as la_mod

    fake_dir = tmp_path / "fake_app_logs"
    monkeypatch.setattr(la_mod, "_get_app_log_dir", lambda: fake_dir, raising=False)

    # _write_interaction_log 真实签名是 (log_entry: Dict)
    la_mod._write_interaction_log({
        "user_input": "test input",
        "assistant_output": "test output",
        "model": "test-model",
    })

    if fake_dir.exists():
        interaction_files = list(fake_dir.glob("llm_interaction_*.log"))
        assert interaction_files == [], f"logging.enabled=false 时不应写 interaction 日志，但找到 {interaction_files}"
