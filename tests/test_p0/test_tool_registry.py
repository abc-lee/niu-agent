import pytest
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
