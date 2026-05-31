"""测试 mcp_loader.py 的外部服务器功能"""

import pytest
from agent.mcp_loader import is_external_server


class TestIsExternalServer:
    """验证 is_external_server() 判断逻辑"""

    def test_no_mode_returns_false(self):
        """无 mode 字段 → 内部服务器"""
        config = {"command": "python", "args": ["-m", "niu_photo_server"]}
        assert is_external_server(config) is False

    def test_empty_mode_returns_false(self):
        """空 mode 字段 → 内部服务器"""
        config = {"command": "python", "mode": ""}
        assert is_external_server(config) is False

    def test_stdio_mode_returns_true(self):
        """mode=stdio → 外部服务器"""
        config = {"mode": "stdio", "command": "npx", "args": ["-y", "@mcp/server"]}
        assert is_external_server(config) is True

    def test_http_mode_returns_true(self):
        """mode=http → 外部服务器"""
        config = {"mode": "http", "url": "https://mcp.example.com/mcp"}
        assert is_external_server(config) is True

    def test_preload_true_still_internal(self):
        """preload=true 但无 mode → 内部服务器"""
        config = {"command": "python", "preload": True}
        assert is_external_server(config) is False

    def test_unknown_mode_returns_false(self):
        """未知 mode 值 → 内部服务器"""
        config = {"mode": "websocket"}
        assert is_external_server(config) is False

    def test_internal_server_config_compatibility(self):
        """内部服务器配置格式不变，仍然返回 False"""
        config = {
            "command": "${PYTHON_PATH}",
            "args": ["-m", "niu_photo_server"],
            "workdir": "../mcp-servers/photo-server/src",
            "preload": True,
            "tools": {
                "ingest": {"visibility": "hidden"},
            }
        }
        assert is_external_server(config) is False
