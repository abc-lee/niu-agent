# tests/test_protocol.py
import pytest
from niu_page_agent.protocol import (
    BrowserCommand,
    NavigateCommand,
    ClickCommand,
    InputCommand,
    ScreenshotCommand,
    ExecuteTaskCommand,
    BrowserResponse,
)


def test_navigate_command_serialization():
    cmd = NavigateCommand(url="https://example.com")
    data = cmd.to_dict()
    assert data == {"type": "navigate", "url": "https://example.com"}


def test_click_command_serialization():
    cmd = ClickCommand(selector="#login-button")
    data = cmd.to_dict()
    assert data == {"type": "click", "selector": "#login-button"}


def test_input_command_serialization():
    cmd = InputCommand(selector="#username", text="user123")
    data = cmd.to_dict()
    assert data == {"type": "input", "selector": "#username", "text": "user123"}


def test_execute_task_command_serialization():
    cmd = ExecuteTaskCommand(
        task="填写登录表单",
        data={"username": "user123", "password": "pass456"}
    )
    data = cmd.to_dict()
    assert data == {
        "type": "execute_task",
        "task": "填写登录表单",
        "data": {"username": "user123", "password": "pass456"}
    }


def test_browser_response_success():
    resp = BrowserResponse(success=True, data="操作成功")
    assert resp.success is True
    assert resp.data == "操作成功"
    assert resp.error is None


def test_browser_response_error():
    resp = BrowserResponse(success=False, error="元素未找到")
    assert resp.success is False
    assert resp.data is None
    assert resp.error == "元素未找到"


def test_browser_response_from_dict():
    data = {"success": True, "data": "操作成功"}
    resp = BrowserResponse.from_dict(data)
    assert resp.success is True
    assert resp.data == "操作成功"
