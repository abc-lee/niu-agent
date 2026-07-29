"""测试飞书 adapter 子进程 stderr 在 logging.enabled=false 时用 DEVNULL。

现状：niu_api/channel/gateway.py:156 open(log_dir / "im_adapter_stderr.log", "a")
作为子进程 stderr，导致每次启动都写日志文件。
整改后：logging.enabled=false 时用 subprocess.DEVNULL，enabled=true 时保留文件重定向。

关键修正（审查 Critical 5）：_launch_adapter 的 logger.error（L131/L144/L163）被
Task 2 的 logger.disable('') gate 后 enabled=false 时丢失。增加 _log_gateway_error()
独立写 logs/gateway_error.log，不受 flag 控制，确保启动失败仍可诊断。
"""
import json
import subprocess
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


def _make_gateway(tmp_path, monkeypatch):
    """构造一个 IMGateway 实例用于测试（mock 掉 preferences.json + adapter_workdir）

    _launch_adapter 实际逻辑（gateway.py:115-160）：
    - L118 读 Path.home() / ".niu" / "preferences.json"，不存在直接 return
    - L122-126 读 im.adapter，为空直接 return
    - L129-132 检查 adapter_workdir 是否存在，不存在 return
    - L143-145 检查 app_id/app_secret，为空 return
    所以为让 Popen 真被调，必须 mock Path.home() 返回 tmp_path，
    并在 tmp_path/.niu/preferences.json 写入飞书配置。
    """
    from unittest.mock import MagicMock

    import niu_api.channel.gateway as gw_mod

    # mock Path.home() 返回 tmp_path（让 preferences.json 读到 tmp_path/.niu/）
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    # 在 tmp_path/.niu/preferences.json 写入飞书 adapter 配置
    niu_dir = tmp_path / ".niu"
    niu_dir.mkdir(parents=True, exist_ok=True)
    prefs = {
        "im": {"adapter": "feishu"},
        "feishu": {"app_id": "test_app_id", "app_secret": "test_app_secret"},
    }
    (niu_dir / "preferences.json").write_text(json.dumps(prefs), encoding="utf-8")

    # mock adapter_workdir.exists() 返回 True（避免真路径检查失败）
    # gateway.py:129 adapter_workdir = Path(__file__).resolve().parent.parent.parent / "im-adapters" / adapter_type / "src"
    original_exists = __import__("pathlib").Path.exists

    def mock_exists(self):
        if "im-adapters" in str(self):
            return True
        return original_exists(self)

    monkeypatch.setattr("pathlib.Path.exists", mock_exists)

    # IMGateway.__init__(self, channel_router, port: int = 19877)
    channel_router = MagicMock()
    gateway = gw_mod.IMGateway(channel_router=channel_router, port=0)
    return gateway


def test_gateway_stderr_devnull_when_logging_disabled(tmp_path, monkeypatch):
    """logging.enabled=false 时，飞书 adapter 子进程 stderr 用 DEVNULL"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=False)

    captured_popen = mock.MagicMock()
    monkeypatch.setattr(subprocess, "Popen", captured_popen)

    gateway = _make_gateway(tmp_path, monkeypatch)
    try:
        gateway._launch_adapter()
    except Exception:
        pass  # 测试只关心 Popen 被调时的 stderr 参数

    assert captured_popen.called, "Popen 应该被调用（mock 不全会导致静默通过）"
    _, kwargs = captured_popen.call_args
    assert kwargs.get("stderr") == subprocess.DEVNULL, \
        f"logging.enabled=false 时 stderr 应该是 DEVNULL，但传了 {kwargs.get('stderr')}"


def test_gateway_stderr_file_when_logging_enabled(tmp_path, monkeypatch):
    """logging.enabled=true 时，飞书 adapter 子进程 stderr 重定向到文件"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=True)

    captured_popen = mock.MagicMock()
    monkeypatch.setattr(subprocess, "Popen", captured_popen)

    gateway = _make_gateway(tmp_path, monkeypatch)
    try:
        gateway._launch_adapter()
    except Exception:
        pass

    assert captured_popen.called, "Popen 应该被调用（mock 不全会导致静默通过）"
    _, kwargs = captured_popen.call_args
    assert kwargs.get("stderr") != subprocess.DEVNULL, \
        "logging.enabled=true 时 stderr 应该重定向到文件，不应是 DEVNULL"


def test_gateway_error_logged_to_file_on_launch_failure(tmp_path, monkeypatch):
    """飞书 adapter 启动失败时，_log_gateway_error 写 logs/gateway_error.log（不受 flag 控制）"""
    _setup_config(tmp_path, monkeypatch, logging_enabled=False)  # flag 关闭

    # monkeypatch _get_gateway_log_dir 让 gateway_error.log 写到 tmp_path
    import niu_api.channel.gateway as gw_mod
    fake_log_dir = tmp_path / "fake_logs"
    monkeypatch.setattr(gw_mod, "_get_gateway_log_dir", lambda: fake_log_dir, raising=False)

    # 直接调 _log_gateway_error
    gw_mod._log_gateway_error("test error message")

    error_log = fake_log_dir / "gateway_error.log"
    assert error_log.exists(), "gateway_error.log 应该被创建"
    content = error_log.read_text(encoding="utf-8")
    assert "test error message" in content, f"错误消息应写入文件，但内容是：{content}"
