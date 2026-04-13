"""测试 ToolRegistry 对 MCP call_tool() 模式的支持。

这些测试验证 ToolRegistry 是否能够处理 MCP 标准的 call_tool() 处理器模式。

问题背景：
- 提交 ed4c46f 只添加了 get_tool_schemas()，但没有添加工具函数
- ToolRegistry 期望模块级函数名与工具名匹配，或 get_tool_function(name) 方法
- 以前的架构使用 stdio 进程间通信，通过统一的 call_tool() 处理器
- 迁移到同进程 ToolRegistry 时不完整

MCP 标准：
- call_tool(tool_name: str, arguments: dict) 是 MCP 标准的统一处理器
- 所有工具调用都通过这一个函数路由
- 这是 MCP stdio 架构的标准模式
"""
import pytest
from unittest.mock import Mock

from agent.tool_registry import ToolRegistry, get_registry, reset_registry


class TestCallToolWrapper:
    """测试 call_tool() 包装器功能。"""

    def setup_method(self):
        """每个测试前重置 registry。"""
        reset_registry()

    def test_wrap_call_tool_handler(self):
        """ToolRegistry 应该包装 call_tool() 当单独的函数不存在时。"""

        # 创建模拟 MCP 模块，有 call_tool 但没有单独的函数
        # 使用 spec=[] 防止 Mock 自动返回 Mock 对象
        mock_module = Mock(spec=['call_tool', 'get_tool_schemas', 'TOOL_SCHEMAS'])

        # 定义 call_tool 处理器（MCP 标准模式）
        def mock_call_tool(tool_name: str, arguments: dict):
            if tool_name == "test_tool":
                return {"status": "success", "data": arguments}
            raise ValueError(f"未知工具: {tool_name}")

        mock_module.call_tool = mock_call_tool

        # 定义 schema
        mock_module.TOOL_SCHEMAS = {
            "test_tool": {
                "name": "test_tool",
                "description": "测试工具",
                "inputSchema": {"type": "object", "properties": {}}
            }
        }

        # 模拟 get_tool_schemas
        mock_module.get_tool_schemas = Mock(return_value=[
            {"name": "test_tool", "description": "测试工具", "inputSchema": {}}
        ])

        # 创建 registry
        registry = ToolRegistry()

        # 注册服务器
        registry.register_server("test-server", mock_module)

        # 获取包装后的函数
        tool_fn = registry.get("test-server/test_tool")

        # 验证它能工作
        assert tool_fn is not None, "ToolRegistry 应该为 call_tool 模式创建包装器"
        result = tool_fn(param1="value1")
        assert result == {"status": "success", "data": {"param1": "value1"}}

    def test_fallback_to_module_level_function(self):
        """ToolRegistry 应该优先使用模块级函数如果它们存在。"""

        # 明确指定模块有哪些属性
        mock_module = Mock(spec=['specific_tool', 'call_tool', 'get_tool_schemas', 'TOOL_SCHEMAS'])

        # 同时定义 call_tool 和单独的函数
        def specific_tool(arg1: str):
            return {"from": "individual_function", "arg1": arg1}

        def call_tool_handler(tool_name: str, arguments: dict):
            return {"from": "call_tool", "tool": tool_name}

        mock_module.specific_tool = specific_tool
        mock_module.call_tool = call_tool_handler
        mock_module.TOOL_SCHEMAS = {
            "specific_tool": {"name": "specific_tool", "description": "..."}
        }
        mock_module.get_tool_schemas = Mock(return_value=[
            {"name": "specific_tool", "description": "..."}
        ])

        registry = ToolRegistry()
        registry.register_server("test-server", mock_module)

        # 应该使用单独的函数，而不是 call_tool 包装器
        tool_fn = registry.get("test-server/specific_tool")
        result = tool_fn(arg1="test")

        assert result == {"from": "individual_function", "arg1": "test"}

    def test_call_tool_with_multiple_tools(self):
        """call_tool 包装器应该能够处理多个工具。"""

        mock_module = Mock(spec=['call_tool', 'get_tool_schemas'])

        def mock_call_tool(tool_name: str, arguments: dict):
            if tool_name == "tool_a":
                return {"tool": "a", "args": arguments}
            elif tool_name == "tool_b":
                return {"tool": "b", "args": arguments}
            raise ValueError(f"未知工具: {tool_name}")

        mock_module.call_tool = mock_call_tool
        mock_module.get_tool_schemas = Mock(return_value=[
            {"name": "tool_a", "description": "工具A"},
            {"name": "tool_b", "description": "工具B"},
        ])

        registry = ToolRegistry()
        registry.register_server("multi-server", mock_module)

        # 两个工具都应该能用
        tool_a = registry.get("multi-server/tool_a")
        tool_b = registry.get("multi-server/tool_b")

        assert tool_a is not None
        assert tool_b is not None

        result_a = tool_a(x=1)
        result_b = tool_b(y=2)

        assert result_a == {"tool": "a", "args": {"x": 1}}
        assert result_b == {"tool": "b", "args": {"y": 2}}

    def test_call_tool_exception_propagation(self):
        """call_tool 包装器应该正确传播异常。"""

        mock_module = Mock(spec=['call_tool', 'get_tool_schemas'])

        def mock_call_tool(tool_name: str, arguments: dict):
            raise RuntimeError("工具执行失败")

        mock_module.call_tool = mock_call_tool
        mock_module.get_tool_schemas = Mock(return_value=[
            {"name": "failing_tool", "description": "会失败的工具"}
        ])

        registry = ToolRegistry()
        registry.register_server("error-server", mock_module)

        tool_fn = registry.get("error-server/failing_tool")
        assert tool_fn is not None

        # 应该传播原始异常
        with pytest.raises(RuntimeError, match="工具执行失败"):
            tool_fn()
