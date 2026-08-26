"""测试 mcp_loader.py 的外部服务器功能 + MCP 加载失败状态槽（E4-08/E4-16）"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from agent.mcp_loader import is_external_server, reset_mcp_load_failures


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


class TestMcpLoadFailureSlot:
    """MCP 加载失败状态槽（E4-08/E4-16）——失败收集进可查询状态槽。

    服务端保留至下次加载周期，不随前端显示清除（R4 P1：
    清除则第二窗口/重连拉取恒空 = 静默丢失）。
    """

    def setup_method(self):
        """每个测试前清空状态槽（等价于新加载周期开始）"""
        reset_mcp_load_failures()

    @staticmethod
    def _fake_import_with_optional_failure(fail_mode: str):
        """构造 fake __import__：required 模块成功，可选模块按 fail_mode 失败。

        fail_mode:
          - "import_error": 可选模块导入抛 ImportError
          - "no_get_tool_schemas": 可选模块对象缺 get_tool_schemas（register_server 返回 False）
        """
        import builtins
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "ok-module":
                mod = Mock(spec=["get_tool_schemas"])
                mod.get_tool_schemas.return_value = [
                    {"name": "ok_tool", "description": "ok", "inputSchema": {}}
                ]
                return mod
            if name == "niu_ha_server":
                if fail_mode == "import_error":
                    raise ImportError("No module named 'niu_ha_server'")
                return object()  # 无 get_tool_schemas 属性
            return real_import(name, globals, locals, fromlist, level)

        return fake_import

    def test_load_mcp_tools_optional_import_failure_recorded(self):
        """可选段 ImportError → 收集 (server_name, reason) 进状态槽"""
        from agent.mcp_loader import get_mcp_load_failures, load_mcp_tools
        from agent.tool_registry import reset_registry

        reset_registry()
        try:
            with patch(
                "agent.mcp_loader._load_mcp_config",
                return_value=({"ha-server": {"preload": True}}, set()),
            ), patch(
                "agent.mcp_loader.OPTIONAL_SERVERS",
                [("ha-server", "niu_ha_server")],
            ), patch(
                "builtins.__import__",
                side_effect=self._fake_import_with_optional_failure("import_error"),
            ):
                load_mcp_tools(required_servers=[("ok-server", "ok-module")])
        finally:
            reset_registry()

        failures = get_mcp_load_failures()
        assert len(failures) == 1, f"应恰好收集 1 条失败，实际: {failures}"
        assert failures[0]["server"] == "ha-server"
        assert "No module named 'niu_ha_server'" in failures[0]["reason"]

    def test_load_mcp_tools_optional_registration_failure_recorded(self):
        """可选段注册失败（模块缺 get_tool_schemas → register_server 返回 False）→ 收集"""
        from agent.mcp_loader import get_mcp_load_failures, load_mcp_tools
        from agent.tool_registry import reset_registry

        reset_registry()
        try:
            with patch(
                "agent.mcp_loader._load_mcp_config",
                return_value=({"ha-server": {"preload": True}}, set()),
            ), patch(
                "agent.mcp_loader.OPTIONAL_SERVERS",
                [("ha-server", "niu_ha_server")],
            ), patch(
                "builtins.__import__",
                side_effect=self._fake_import_with_optional_failure("no_get_tool_schemas"),
            ):
                load_mcp_tools(required_servers=[("ok-server", "ok-module")])
        finally:
            reset_registry()

        failures = get_mcp_load_failures()
        assert len(failures) == 1, f"应恰好收集 1 条失败，实际: {failures}"
        assert failures[0]["server"] == "ha-server"
        assert "get_tool_schemas" in failures[0]["reason"]

    def test_load_external_servers_failure_recorded(self):
        """外部服务器连接失败 → 收集 (server_name, reason) 进状态槽；内部服务器跳过不记录"""
        from agent.mcp_loader import get_mcp_load_failures, load_external_servers
        from agent.tool_registry import ToolRegistry

        config = {
            "ext-tool": {"mode": "stdio", "command": "npx", "args": ["-y", "@mcp/server"]},
            "photo-server": {"command": "python", "args": ["-m", "niu_photo_server"]},
        }
        mcp_client = Mock()
        mcp_client.connect_stdio = AsyncMock(side_effect=RuntimeError("Connection refused"))

        registry = ToolRegistry()
        with patch(
            "agent.mcp_loader._load_mcp_config", return_value=(config, set())
        ):
            asyncio.run(load_external_servers(mcp_client, registry=registry))

        failures = get_mcp_load_failures()
        assert len(failures) == 1, f"应恰好收集 1 条外部失败，实际: {failures}"
        assert failures[0]["server"] == "ext-tool"
        assert "Connection refused" in failures[0]["reason"]

    def test_record_deduplicates_same_server_reason(self):
        """同 (server, reason) 只记录一次（防加载周期内重复记录膨胀）"""
        from agent.mcp_loader import get_mcp_load_failures, record_mcp_load_failure

        record_mcp_load_failure("ext-tool", "连接失败: boom")
        record_mcp_load_failure("ext-tool", "连接失败: boom")
        record_mcp_load_failure("ha-server", "模块不可用: gone")

        failures = get_mcp_load_failures()
        assert len(failures) == 2, f"应去重为 2 条，实际: {failures}"

    def test_get_returns_copy_slot_not_cleared_by_query(self):
        """查询返回副本——查询本身不改变状态槽内容（服务端保留至下次加载周期）"""
        from agent.mcp_loader import get_mcp_load_failures, record_mcp_load_failure

        record_mcp_load_failure("ext-tool", "连接失败: boom")
        snapshot = get_mcp_load_failures()
        snapshot.clear()  # 调用方修改查询结果不影响状态槽
        assert get_mcp_load_failures() == [{"server": "ext-tool", "reason": "连接失败: boom"}]

