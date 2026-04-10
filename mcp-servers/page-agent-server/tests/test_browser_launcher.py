# tests/test_browser_launcher.py
import pytest
import subprocess
from unittest.mock import Mock, patch, MagicMock
from niu_page_agent.browser_launcher import BrowserLauncher


def test_launcher_initialization():
    launcher = BrowserLauncher(
        extension_path="/path/to/extension",
        port=38401
    )
    assert launcher.extension_path == "/path/to/extension"
    assert launcher.port == 38401
    assert launcher.process is None


@patch('subprocess.Popen')
def test_launch_creates_process(mock_popen):
    mock_process = MagicMock()
    mock_popen.return_value = mock_process

    launcher = BrowserLauncher(
        extension_path="/path/to/extension",
        port=38401
    )
    result = launcher.launch()

    assert result == mock_process
    assert launcher.process == mock_process
    mock_popen.assert_called_once()

    # 验证命令参数包含扩展路径
    call_args = mock_popen.call_args[0][0]
    assert "--load-extension=/path/to/extension" in call_args


@patch('subprocess.Popen')
def test_launch_with_custom_binary(mock_popen):
    launcher = BrowserLauncher(
        extension_path="/path/to/extension",
        chrome_binary="/custom/chrome"
    )
    launcher.launch()

    call_args = mock_popen.call_args[0][0]
    assert "/custom/chrome" in call_args


def test_shutdown_kills_process():
    launcher = BrowserLauncher(extension_path="/path/to/extension")
    mock_process = MagicMock()
    launcher.process = mock_process
    launcher.shutdown()

    mock_process.kill.assert_called_once()
    assert launcher.process is None


def test_shutdown_without_process():
    launcher = BrowserLauncher(extension_path="/path/to/extension")
    # 不应该抛异常
    launcher.shutdown()
    assert launcher.process is None
