import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestMCPClientManagerInit:
    """验证 MCPClientManager 初始化"""

    def test_init_with_sampling_callback(self):
        """初始化时传入 sampling_callback"""
        from agent.mcp_client import MCPClientManager
        callback = MagicMock()
        manager = MCPClientManager(sampling_callback=callback)
        assert manager._sampling_callback is callback

    def test_init_empty_connections(self):
        """初始化时无连接"""
        from agent.mcp_client import MCPClientManager
        manager = MCPClientManager(sampling_callback=None)
        assert len(manager._connections) == 0

    def test_init_has_connection_contexts(self):
        """初始化时有 _connection_contexts 属性"""
        from agent.mcp_client import MCPClientManager
        manager = MCPClientManager(sampling_callback=None)
        assert hasattr(manager, '_connection_contexts')
        assert len(manager._connection_contexts) == 0


class TestMCPClientManagerCallToolSync:
    """验证同步调用桥接"""

    def test_call_tool_sync_calls_async_method(self):
        """call_tool_sync 内部调用 call_tool 异步方法"""
        from agent.mcp_client import MCPClientManager
        manager = MCPClientManager(sampling_callback=None)
        with patch.object(manager, 'call_tool', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"content": [{"type": "text", "text": "ok"}]}
            result = manager.call_tool_sync("test-server", "test-tool", {"arg": "val"})
            mock_call.assert_called_once_with("test-server", "test-tool", {"arg": "val"})

    def test_call_tool_sync_returns_result(self):
        """call_tool_sync 返回异步调用的结果"""
        from agent.mcp_client import MCPClientManager
        manager = MCPClientManager(sampling_callback=None)
        expected = {"content": [{"type": "text", "text": "result"}]}
        with patch.object(manager, 'call_tool', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = expected
            result = manager.call_tool_sync("test-server", "test-tool", {})
            assert result == expected


class TestMCPClientManagerListTools:
    """验证工具列表获取"""

    @pytest.mark.asyncio
    async def test_list_tools_returns_tools(self):
        """list_tools 返回工具列表"""
        from agent.mcp_client import MCPClientManager
        manager = MCPClientManager(sampling_callback=None)
        mock_session = AsyncMock()
        mock_tool = MagicMock()
        mock_tool.name = "read_file"
        mock_tool.description = "Read a file"
        mock_tool.inputSchema = {"type": "object", "properties": {"path": {"type": "string"}}}
        mock_session.list_tools.return_value = MagicMock(tools=[mock_tool])
        manager._connections["test-server"] = mock_session

        tools = await manager.list_tools("test-server")
        assert len(tools) == 1
        assert tools[0].name == "read_file"

    @pytest.mark.asyncio
    async def test_list_tools_raises_for_unknown_server(self):
        """list_tools 对未知服务器抛出 KeyError"""
        from agent.mcp_client import MCPClientManager
        manager = MCPClientManager(sampling_callback=None)
        with pytest.raises(KeyError):
            await manager.list_tools("unknown-server")


class TestMCPSamplingCallback:
    """验证 Sampling callback 传递给 ClientSession"""

    def test_sampling_callback_stored(self):
        """sampling_callback 被存储"""
        from agent.mcp_client import MCPClientManager
        cb = MagicMock()
        manager = MCPClientManager(sampling_callback=cb)
        assert manager._sampling_callback is cb

    def test_no_sampling_callback_is_none(self):
        """未传 sampling_callback 时为 None"""
        from agent.mcp_client import MCPClientManager
        manager = MCPClientManager()
        assert manager._sampling_callback is None


class TestSamplingCallback:
    """验证 Sampling callback 实现"""

    def test_make_sampling_callback_returns_callable(self):
        """make_sampling_callback 返回可调用对象"""
        from agent.mcp_client import make_sampling_callback
        callback = make_sampling_callback()
        assert callable(callback)

    @pytest.mark.asyncio
    async def test_sampling_callback_calls_llm(self):
        """Sampling callback 调用 LLM 并返回结果"""
        from agent.mcp_client import make_sampling_callback
        from mcp.types import CreateMessageRequestParams, TextContent, SamplingMessage
        callback = make_sampling_callback()

        with patch("niu_api.llm_proxy.get_llm_config", return_value={"model": "test-model"}), \
             patch("niu_api.llm_proxy.call_llm_via_litellm", return_value={"content": "文档分类：技术文档"}) as mock_call:
            params = CreateMessageRequestParams(
                messages=[SamplingMessage(role="user", content=TextContent(type="text", text="请分类"))],
                maxTokens=100,
            )
            result = await callback(None, params)
            assert result.role == "assistant"
            assert isinstance(result.content, TextContent)
            assert "技术文档" in result.content.text
            mock_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_sampling_callback_returns_error_on_failure(self):
        """LLM 调用失败时返回错误提示"""
        from agent.mcp_client import make_sampling_callback
        from mcp.types import CreateMessageRequestParams, TextContent, SamplingMessage
        callback = make_sampling_callback()

        with patch("niu_api.llm_proxy.get_llm_config", side_effect=RuntimeError("LLM unavailable")):
            params = CreateMessageRequestParams(
                messages=[SamplingMessage(role="user", content=TextContent(type="text", text="test"))],
                maxTokens=100,
            )
            result = await callback(None, params)
            assert result.role == "assistant"
            assert "Sampling" in result.content.text or "失败" in result.content.text
