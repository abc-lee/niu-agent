# tests/test_ws_client.py
import pytest
from unittest.mock import Mock, patch, MagicMock
import json
from niu_page_agent.ws_client import BrowserWSClient
from niu_page_agent.protocol import NavigateCommand, BrowserResponse


@patch('websocket.create_connection')
def test_client_initialization(mock_create):
    client = BrowserWSClient(port=38401)
    assert client.port == 38401
    assert client.url == "ws://localhost:38401"


@patch('websocket.create_connection')
def test_connect_creates_websocket(mock_create):
    mock_ws = MagicMock()
    mock_create.return_value = mock_ws

    client = BrowserWSClient(port=38401)
    client.connect()

    mock_create.assert_called_once_with("ws://localhost:38401")
    assert client.ws == mock_ws


@patch('websocket.create_connection')
def test_send_command_and_receive_response(mock_create):
    mock_ws = MagicMock()
    mock_create.return_value = mock_ws

    # 模拟响应
    mock_ws.recv.return_value = json.dumps({
        "success": True,
        "data": "导航成功"
    })

    client = BrowserWSClient(port=38401)
    client.connect()

    cmd = NavigateCommand(url="https://example.com")
    response = client.send_command(cmd)

    assert response.success is True
    assert response.data == "导航成功"

    # 验证发送的消息
    sent_data = json.loads(mock_ws.send.call_args[0][0])
    assert sent_data["type"] == "navigate"
    assert sent_data["url"] == "https://example.com"


@patch('websocket.create_connection')
def test_close_connection(mock_create):
    mock_ws = MagicMock()
    mock_create.return_value = mock_ws

    client = BrowserWSClient(port=38401)
    client.connect()
    client.close()

    mock_ws.close.assert_called_once()
    assert client.ws is None
