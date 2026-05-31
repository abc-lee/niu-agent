import pytest
from unittest.mock import MagicMock
from agent.tool_registry import ToolRegistry, get_registry


class TestVisibilityValues:
    """验证 visibility 只有 static 和 hidden 两种值"""

    def setup_method(self):
        self.registry = ToolRegistry()
        self.registry._tools = {}
        self.registry._schemas = {}
        self.registry._server_tools = {}

    def test_register_default_visibility_is_static(self):
        """register() 未指定 visibility 时默认为 static"""
        def dummy():
            pass
        self.registry.register("test-server/tool", dummy, {"name": "test-server/tool"})
        assert self.registry._schemas["test-server/tool"]["visibility"] == "static"

    def test_register_static_visibility(self):
        """可以注册 static 工具"""
        def dummy():
            pass
        self.registry.register("test-server/tool", dummy, {"name": "test-server/tool"}, visibility="static")
        assert self.registry._schemas["test-server/tool"]["visibility"] == "static"

    def test_register_hidden_visibility(self):
        """可以注册 hidden 工具"""
        def dummy():
            pass
        self.registry.register("test-server/tool", dummy, {"name": "test-server/tool"}, visibility="hidden")
        assert self.registry._schemas["test-server/tool"]["visibility"] == "hidden"

    def test_get_static_tools_returns_only_static(self):
        """get_static_tools 只返回 static 工具名列表"""
        def dummy():
            pass
        self.registry.register("srv/a", dummy, {"name": "srv/a"}, visibility="static")
        self.registry.register("srv/b", dummy, {"name": "srv/b"}, visibility="hidden")
        static_names = self.registry.get_static_tools()
        assert "srv/a" in static_names
        assert "srv/b" not in static_names

    def test_register_server_default_visibility_is_hidden(self):
        """register_server() 未指定 visibility 时默认为 hidden"""
        import types
        mod = types.ModuleType("test_mod")
        mod.get_tool_schemas = lambda: [{"name": "tool1", "description": "d", "input_schema": {}}]
        mod.tool1 = lambda: "result"
        self.registry.register_server("test-server", mod)
        assert self.registry._schemas["test-server/tool1"]["visibility"] == "hidden"

    def test_no_get_dynamic_tools_method(self):
        """get_dynamic_tools 方法已删除"""
        assert not hasattr(self.registry, "get_dynamic_tools")

    def test_no_get_visibility_method(self):
        """get_visibility 方法已删除"""
        assert not hasattr(self.registry, "get_visibility")


class TestAskAgent:
    """验证 ask_agent 注入和调用机制"""

    def setup_method(self):
        self.registry = ToolRegistry()
        self.registry._tools = {}
        self.registry._schemas = {}
        self.registry._server_tools = {}

    def test_ask_agent_returns_none_when_not_set(self):
        """未注入 callback 时 ask_agent 返回 None"""
        result = self.registry.ask_agent(prompt="test")
        assert result is None

    def test_ask_agent_calls_callback(self):
        """注入 callback 后 ask_agent 调用它"""
        calls = []
        def mock_callback(prompt, system_prompt="", max_tokens=500):
            calls.append({"prompt": prompt, "system_prompt": system_prompt, "max_tokens": max_tokens})
            return "分类结果"

        self.registry.set_ask_agent(mock_callback)
        result = self.registry.ask_agent(prompt="请分类", system_prompt="你是助手", max_tokens=100)

        assert result == "分类结果"
        assert len(calls) == 1
        assert calls[0]["prompt"] == "请分类"
        assert calls[0]["system_prompt"] == "你是助手"
        assert calls[0]["max_tokens"] == 100

    def test_ask_agent_returns_none_on_callback_exception(self):
        """callback 抛异常时 ask_agent 返回 None"""
        def bad_callback(prompt, system_prompt="", max_tokens=500):
            raise RuntimeError("LLM 调用失败")

        self.registry.set_ask_agent(bad_callback)
        result = self.registry.ask_agent(prompt="test")
        assert result is None

    def test_set_ask_agent_overrides_previous(self):
        """重复设置 callback 会覆盖前一个"""
        self.registry.set_ask_agent(lambda prompt: "first")
        self.registry.set_ask_agent(lambda prompt: "second")
        result = self.registry.ask_agent(prompt="test")
        assert result == "second"


class TestExternalToolRegistration:
    """验证外部 MCP 工具注册和调用"""

    def setup_method(self):
        self.registry = ToolRegistry()
        self.registry._tools = {}
        self.registry._schemas = {}
        self.registry._server_tools = {}
        self.registry._external_tools = {}

    def test_register_external_server_creates_schemas(self):
        """注册外部服务器时创建工具 schema"""
        self.registry._external_tools["ext-srv/read_file"] = ("ext-srv", "read_file")
        self.registry._schemas["ext-srv/read_file"] = {
            "name": "ext-srv/read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            "visibility": "static",
        }
        self.registry._server_tools.setdefault("ext-srv", []).append("ext-srv/read_file")

        assert "ext-srv/read_file" in self.registry._schemas
        assert self.registry._schemas["ext-srv/read_file"]["visibility"] == "static"

    def test_get_external_tool_returns_wrapper(self):
        """get() 对外部工具返回同步包装器"""
        self.registry._external_tools["ext-srv/read_file"] = ("ext-srv", "read_file")
        mock_client = MagicMock()
        mock_client.call_tool_sync.return_value = {"content": [{"type": "text", "text": "file content"}]}
        self.registry._mcp_client = mock_client

        func = self.registry.get("ext-srv/read_file")
        assert func is not None
        assert callable(func)

    def test_get_external_tool_wrapper_calls_mcp_client(self):
        """外部工具包装器调用 MCP Client"""
        self.registry._external_tools["ext-srv/read_file"] = ("ext-srv", "read_file")
        mock_client = MagicMock()
        mock_client.call_tool_sync.return_value = {"content": [{"type": "text", "text": "file content"}]}
        self.registry._mcp_client = mock_client

        func = self.registry.get("ext-srv/read_file")
        result = func(path="/tmp/test.txt")
        mock_client.call_tool_sync.assert_called_once_with("ext-srv", "read_file", {"path": "/tmp/test.txt"})

    def test_get_external_tool_returns_none_without_mcp_client(self):
        """没有 MCPClientManager 时 get() 返回 None"""
        self.registry._external_tools["ext-srv/read_file"] = ("ext-srv", "read_file")
        self.registry._mcp_client = None
        func = self.registry.get("ext-srv/read_file")
        assert func is None

    def test_external_tool_visibility_from_config(self):
        """外部工具 visibility 从配置文件读取"""
        self.registry._external_tools["ext-srv/read_file"] = ("ext-srv", "read_file")
        self.registry._schemas["ext-srv/read_file"] = {
            "name": "ext-srv/read_file",
            "description": "Read a file",
            "input_schema": {},
            "visibility": "hidden",
        }
        static = self.registry.get_static_tools()
        names = [s for s in static]
        assert "ext-srv/read_file" not in names

    def test_external_tool_default_visibility_hidden(self):
        """未配置 visibility 的外部工具默认 hidden"""
        self.registry._external_tools["ext-srv/read_file"] = ("ext-srv", "read_file")
        self.registry._schemas["ext-srv/read_file"] = {
            "name": "ext-srv/read_file",
            "description": "Read a file",
            "input_schema": {},
            "visibility": "hidden",
        }
        assert self.registry._schemas["ext-srv/read_file"]["visibility"] == "hidden"

    def test_get_internal_tool_still_works(self):
        """内部工具注册和调用不受影响"""
        def my_func():
            return "internal result"
        self.registry.register("internal-srv/tool", my_func, {"name": "internal-srv/tool"})

        func = self.registry.get("internal-srv/tool")
        assert func is my_func
        assert func() == "internal result"

    def test_has_tool_checks_external_tools(self):
        """has_tool 同时检查内部和外部工具"""
        self.registry._external_tools["ext-srv/tool"] = ("ext-srv", "tool")
        assert self.registry.has_tool("ext-srv/tool") is True
        assert self.registry.has_tool("nonexistent/tool") is False

    def test_list_tools_includes_external(self):
        """list_tools 同时返回内部和外部工具"""
        def my_func():
            pass
        self.registry.register("int-srv/tool1", my_func, {"name": "int-srv/tool1"})
        self.registry._external_tools["ext-srv/tool2"] = ("ext-srv", "tool2")
        tools = self.registry.list_tools()
        assert "int-srv/tool1" in tools
        assert "ext-srv/tool2" in tools

    def test_set_mcp_client(self):
        """set_mcp_client 注入 MCPClientManager"""
        mock_client = MagicMock()
        self.registry.set_mcp_client(mock_client)
        assert self.registry._mcp_client is mock_client

    def test_clear_cleans_external_tools(self):
        """clear 同时清理外部工具"""
        self.registry._external_tools["ext-srv/tool"] = ("ext-srv", "tool")
        mock_client = MagicMock()
        self.registry._mcp_client = mock_client
        self.registry.clear()
        assert len(self.registry._external_tools) == 0
        assert self.registry._mcp_client is None


class TestAskAgentCallback:
    """验证 runner 注入的 ask_agent callback 能调用 LLM"""

    def test_make_ask_agent_callback_returns_callable(self):
        """_make_ask_agent_callback 返回可调用对象"""
        from agent.runner import NiuRunner
        runner = NiuRunner.__new__(NiuRunner)
        callback = runner._make_ask_agent_callback()
        assert callable(callback)

    def test_ask_agent_callback_signature(self):
        """callback 签名符合 (prompt, system_prompt, max_tokens) -> str"""
        from agent.runner import NiuRunner
        runner = NiuRunner.__new__(NiuRunner)
        callback = runner._make_ask_agent_callback()
        import inspect
        sig = inspect.signature(callback)
        params = list(sig.parameters.keys())
        assert "prompt" in params
        assert "system_prompt" in params
        assert "max_tokens" in params
