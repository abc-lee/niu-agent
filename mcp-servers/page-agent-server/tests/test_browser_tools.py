# tests/test_browser_tools.py
import pytest
from unittest.mock import Mock, patch, MagicMock
from niu_page_agent.browser_tools import (
    browser_navigate,
    browser_click,
    browser_input,
    browser_screenshot,
    execute_browser_task,
)


@patch('niu_page_agent.browser_tools.get_ws_client')
def test_browser_navigate(mock_get_client):
    mock_client = MagicMock()
    mock_client.send_command.return_value = Mock(success=True, data="导航成功")
    mock_get_client.return_value = mock_client

    result = browser_navigate("https://example.com")

    assert result == "导航成功"
    mock_client.send_command.assert_called_once()


@patch('niu_page_agent.browser_tools.get_ws_client')
def test_browser_click(mock_get_client):
    mock_client = MagicMock()
    mock_client.send_command.return_value = Mock(success=True, data="点击成功")
    mock_get_client.return_value = mock_client

    result = browser_click("#login-button")

    assert result == "点击成功"


@patch('niu_page_agent.browser_tools.get_ws_client')
def test_browser_input(mock_get_client):
    mock_client = MagicMock()
    mock_client.send_command.return_value = Mock(success=True, data="输入成功")
    mock_get_client.return_value = mock_client

    result = browser_input("#username", "user123")

    assert result == "输入成功"


@patch('niu_page_agent.browser_tools.get_ws_client')
def test_browser_screenshot(mock_get_client):
    mock_client = MagicMock()
    mock_client.send_command.return_value = Mock(
        success=True,
        data="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
    mock_get_client.return_value = mock_client

    result = browser_screenshot()

    assert result.startswith("data:image/png;base64,")


@patch('niu_page_agent.browser_tools.get_ws_client')
def test_execute_browser_task(mock_get_client):
    mock_client = MagicMock()
    mock_client.send_command.return_value = Mock(
        success=True,
        data="任务完成：已填写登录表单"
    )
    mock_get_client.return_value = mock_client

    result = execute_browser_task(
        task="填写登录表单",
        data={"username": "user123", "password": "pass456"}
    )

    assert "任务完成" in result


@patch('niu_page_agent.browser_tools.get_ws_client')
def test_browser_tool_error_handling(mock_get_client):
    mock_client = MagicMock()
    mock_client.send_command.return_value = Mock(
        success=False,
        error="元素未找到"
    )
    mock_get_client.return_value = mock_client

    result = browser_click("#nonexistent")

    assert "错误" in result
    assert "元素未找到" in result
