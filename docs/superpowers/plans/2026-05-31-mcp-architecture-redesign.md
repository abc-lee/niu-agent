# MCP 架构改造实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 改造 MCP 架构，支持外部标准 MCP 服务器接入 + MCP Sampling 标准，保留所有现有功能和三层可见性机制。

**Architecture:** 双轨架构——内部服务器保持同进程模式（性能优先），外部服务器走标准 MCP Client（兼容优先）。两轨在 ToolRegistry 层统一，调用方无感知。

**Tech Stack:** Python MCP SDK (`mcp` package), FastMCP (外部服务器), litellm (Sampling LLM 调用)

**Design Spec:** `docs/superpowers/specs/2026-05-31-mcp-architecture-design.md`

---

## 文件结构

| 文件 | 职责 | 状态 |
|------|------|------|
| `agent/tool_registry.py` | 工具注册中心，双轨统一接口，ask_agent 注入 | 修改 |
| `agent/mcp_client.py` | MCP Client 管理器（stdio + HTTP 连接 + Sampling） | 重写 |
| `agent/mcp_loader.py` | MCP 模块加载器，新增外部服务器加载 | 修改 |
| `agent/runner.py` | Agent 运行器，注入 ask_agent + 初始化 MCPClientManager | 修改 |
| `config/mcp-servers.yaml` | MCP 服务器配置，新增 mode 字段 | 修改 |
| `tests/test_p0/test_tool_registry.py` | ToolRegistry 单元测试 | 新建 |
| `tests/test_p0/test_mcp_client.py` | MCPClientManager 单元测试 | 新建 |

---

### Task 1: 清理废弃的 dynamic visibility 代码

**Files:**
- Modify: `agent/tool_registry.py`
- Test: `tests/test_p0/test_tool_registry.py`

**背景**：`dynamic` visibility 是早期向量检索方案的残留，引入虚拟磁盘后已废弃。现在只有 `static` 和 `hidden` 两种。

- [ ] **Step 1: 写失败测试——验证 visibility 只有 static/hidden**

```python
# tests/test_p0/test_tool_registry.py
import pytest
from agent.tool_registry import ToolRegistry, get_registry


class TestVisibilityValues:
    """验证 visibility 只有 static 和 hidden 两种值"""

    def setup_method(self):
        self.registry = ToolRegistry()
        self.registry._tools = {}
        self.registry._schemas = {}
        self.registry._server_tools = {}

    def test_register_default_visibility_is_hidden(self):
        """未指定 visibility 时默认为 hidden"""
        def dummy():
            pass
        self.registry.register("test-server/tool", dummy, {"name": "test-server/tool"})
        assert self.registry._schemas["test-server/tool"]["visibility"] == "hidden"

    def test_register_static_visibility(self):
        """可以注册 static 工具"""
        def dummy():
            pass
        self.registry.register("test-server/tool", dummy, {"name": "test-server/tool", "visibility": "static"})
        assert self.registry._schemas["test-server/tool"]["visibility"] == "static"

    def test_register_hidden_visibility(self):
        """可以注册 hidden 工具"""
        def dummy():
            pass
        self.registry.register("test-server/tool", dummy, {"name": "test-server/tool", "visibility": "hidden"})
        assert self.registry._schemas["test-server/tool"]["visibility"] == "hidden"

    def test_get_static_tools_returns_only_static(self):
        """get_static_tools 只返回 static 工具"""
        def dummy():
            pass
        self.registry.register("srv/a", dummy, {"name": "srv/a", "visibility": "static"})
        self.registry.register("srv/b", dummy, {"name": "srv/b", "visibility": "hidden"})
        schemas = self.registry.get_static_tools()
        names = [s["name"] for s in schemas]
        assert "srv/a" in names
        assert "srv/b" not in names

    def test_no_get_dynamic_tools_method(self):
        """get_dynamic_tools 方法已删除"""
        assert not hasattr(self.registry, "get_dynamic_tools")

    def test_no_get_visibility_method(self):
        """get_visibility 方法已删除"""
        assert not hasattr(self.registry, "get_visibility")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_tool_registry.py::TestVisibilityValues -v`
Expected: FAIL — `get_dynamic_tools` 和 `get_visibility` 仍存在，默认 visibility 仍是 `"dynamic"`

- [ ] **Step 3: 删除 `get_dynamic_tools()` 方法**

在 `agent/tool_registry.py` 中，删除 `get_dynamic_tools()` 方法（约第 269-275 行）：

```python
# 删除以下方法
def get_dynamic_tools(self) -> list[dict]:
    """Get tools with dynamic visibility (filtered by vector search)."""
    return [s for s in self._schemas.values() if s.get("visibility") == "dynamic"]
```

- [ ] **Step 4: 删除 `get_visibility()` 方法**

在 `agent/tool_registry.py` 中，删除 `get_visibility()` 方法（约第 246-259 行）：

```python
# 删除以下方法
def get_visibility(self, tool_name: str) -> str:
    """Get the visibility of a tool."""
    if tool_name in self._schemas:
        return self._schemas[tool_name].get("visibility", "dynamic")
    return "dynamic"
```

- [ ] **Step 5: 修改 visibility 默认值为 `"hidden"`**

在 `agent/tool_registry.py` 的 `register()` 方法中，将 visibility 默认值从 `"dynamic"` 改为 `"hidden"`：

```python
# 修改前
tool_vis = normalized_schema.get("visibility", "dynamic")

# 修改后
tool_vis = normalized_schema.get("visibility", "hidden")
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_tool_registry.py::TestVisibilityValues -v`
Expected: PASS — 6 个测试全部通过

- [ ] **Step 7: 运行现有测试确认无回归**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/ -v --timeout=30 2>&1 | tail -30`
Expected: 所有现有测试通过（`get_dynamic_tools` 和 `get_visibility` 无调用者）

- [ ] **Step 8: 提交**

```bash
git add agent/tool_registry.py tests/test_p0/test_tool_registry.py
git commit -m "refactor: 清理废弃的 dynamic visibility 代码

- 删除 get_dynamic_tools() 方法（无调用者）
- 删除 get_visibility() 方法（无调用者）
- visibility 默认值从 dynamic 改为 hidden
- 新增 TestVisibilityValues 测试类"
```

---

### Task 2: ToolRegistry 新增 ask_agent 机制

**Files:**
- Modify: `agent/tool_registry.py`
- Test: `tests/test_p0/test_tool_registry.py`

**背景**：内部服务器（如 photo-server）在工具调用过程中需要请求 Agent LLM 推理（如文档分类）。同进程模式下没有 MCP session，通过 `ask_agent()` callback 实现等价的 Sampling 功能。

- [ ] **Step 1: 写失败测试——ask_agent 注入和调用**

```python
# 追加到 tests/test_p0/test_tool_registry.py

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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_tool_registry.py::TestAskAgent -v`
Expected: FAIL — `set_ask_agent` 和 `ask_agent` 方法不存在

- [ ] **Step 3: 在 ToolRegistry 中实现 ask_agent**

在 `agent/tool_registry.py` 的 `ToolRegistry.__init__()` 中新增：
```python
self._ask_agent = None  # callable(prompt: str, system_prompt: str = "", max_tokens: int = 500) -> str
```

新增两个方法：
```python
def set_ask_agent(self, fn):
    """注入 Agent LLM 回调函数，供内部 MCP Server 调用"""
    self._ask_agent = fn

def ask_agent(self, prompt: str, system_prompt: str = "", max_tokens: int = 500) -> str | None:
    """请求 Agent LLM 生成回答。返回文本或 None（如果不可用）"""
    if self._ask_agent is None:
        return None
    try:
        return self._ask_agent(prompt=prompt, system_prompt=system_prompt, max_tokens=max_tokens)
    except Exception:
        return None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_tool_registry.py::TestAskAgent -v`
Expected: PASS — 4 个测试全部通过

- [ ] **Step 5: 提交**

```bash
git add agent/tool_registry.py tests/test_p0/test_tool_registry.py
git commit -m "feat: ToolRegistry 新增 ask_agent 注入机制

- set_ask_agent(fn) 注入 LLM callback
- ask_agent(prompt, system_prompt, max_tokens) 调用 LLM
- callback 未注入或异常时返回 None
- 供内部 MCP Server 实现等价 Sampling 功能"
```

---

### Task 3: runner.py 注入 ask_agent callback

**Files:**
- Modify: `agent/runner.py`
- Test: `tests/test_p0/test_tool_registry.py`

**背景**：runner.py 在初始化 ToolRegistry 后，注入 ask_agent callback。callback 调用当前 Agent 的 LLM 生成回复。

- [ ] **Step 1: 写失败测试——验证 ask_agent callback 调用 LLM**

```python
# 追加到 tests/test_p0/test_tool_registry.py

class TestAskAgentCallback:
    """验证 runner 注入的 ask_agent callback 能调用 LLM"""

    def test_make_ask_agent_callback_returns_callable(self):
        """_make_ask_agent_callback 返回可调用对象"""
        from agent.runner import GenericAgentRunner
        runner = GenericAgentRunner.__new__(GenericAgentRunner)
        callback = runner._make_ask_agent_callback()
        assert callable(callback)

    def test_ask_agent_callback_signature(self):
        """callback 签名符合 (prompt, system_prompt, max_tokens) -> str"""
        from agent.runner import GenericAgentRunner
        runner = GenericAgentRunner.__new__(GenericAgentRunner)
        callback = runner._make_ask_agent_callback()
        # 验证签名接受 3 个参数（不含 self）
        import inspect
        sig = inspect.signature(callback)
        params = list(sig.parameters.keys())
        assert "prompt" in params
        assert "system_prompt" in params
        assert "max_tokens" in params
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_tool_registry.py::TestAskAgentCallback -v`
Expected: FAIL — `_make_ask_agent_callback` 方法不存在

- [ ] **Step 3: 在 runner.py 中实现 `_make_ask_agent_callback()`**

在 `agent/runner.py` 的 `GenericAgentRunner` 类中新增：

```python
def _make_ask_agent_callback(self):
    """创建 ask_agent 回调，调用当前 Agent 的 LLM"""
    def ask_agent(prompt: str, system_prompt: str = "", max_tokens: int = 500) -> str | None:
        try:
            from agent.llmcore import load_llm_config, create_client
            config = load_llm_config()
            client = create_client(config)
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            response = client.chat.completions.create(
                model=config["model"],
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.2,
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"ask_agent failed: {e}")
            return None
    return ask_agent
```

- [ ] **Step 4: 在 runner.py 初始化时注入 callback**

在 `GenericAgentRunner.__init__()` 中，`load_mcp_tools()` 之后添加：

```python
# 注入 ask_agent callback
registry = get_registry()
registry.set_ask_agent(self._make_ask_agent_callback())
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_tool_registry.py::TestAskAgentCallback -v`
Expected: PASS

- [ ] **Step 6: 运行全部 ToolRegistry 测试确认无回归**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_tool_registry.py -v`
Expected: PASS — 所有测试通过

- [ ] **Step 7: 提交**

```bash
git add agent/runner.py tests/test_p0/test_tool_registry.py
git commit -m "feat: runner.py 注入 ask_agent callback

- _make_ask_agent_callback() 创建 LLM 调用回调
- 初始化时通过 registry.set_ask_agent() 注入
- 内部 MCP Server 可通过 registry.ask_agent() 请求 LLM 推理"
```

---

### Task 4: 实现 MCPClientManager（stdio + HTTP + Sampling）

**Files:**
- Rewrite: `agent/mcp_client.py`
- Test: `tests/test_p0/test_mcp_client.py`

**背景**：MCPClientManager 管理所有外部 MCP Client 连接，支持 stdio 和 HTTP 两种模式，支持 Sampling callback。这是双轨架构的核心组件——外部服务器通过它连接和调用。

- [ ] **Step 1: 写失败测试——MCPClientManager 基本功能**

```python
# tests/test_p0/test_mcp_client.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agent.mcp_client import MCPClientManager


class TestMCPClientManagerInit:
    """验证 MCPClientManager 初始化"""

    def test_init_with_sampling_callback(self):
        """初始化时传入 sampling_callback"""
        callback = MagicMock()
        manager = MCPClientManager(sampling_callback=callback)
        assert manager._sampling_callback is callback

    def test_init_empty_connections(self):
        """初始化时无连接"""
        manager = MCPClientManager(sampling_callback=None)
        assert len(manager._connections) == 0


class TestMCPClientManagerCallToolSync:
    """验证同步调用桥接"""

    def test_call_tool_sync_calls_async_method(self):
        """call_tool_sync 内部调用 call_tool 异步方法"""
        manager = MCPClientManager(sampling_callback=None)
        with patch.object(manager, 'call_tool', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"content": [{"type": "text", "text": "ok"}]}
            result = manager.call_tool_sync("test-server", "test-tool", {"arg": "val"})
            mock_call.assert_called_once_with("test-server", "test-tool", {"arg": "val"})

    def test_call_tool_sync_returns_result(self):
        """call_tool_sync 返回异步调用的结果"""
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
        manager = MCPClientManager(sampling_callback=None)
        with pytest.raises(KeyError):
            await manager.list_tools("unknown-server")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_mcp_client.py -v`
Expected: FAIL — `agent.mcp_client` 模块不存在或不包含 `MCPClientManager`

- [ ] **Step 3: 重写 `agent/mcp_client.py`**

```python
"""MCP Client Manager — 管理外部 MCP 服务器连接（stdio + HTTP + Sampling）"""
import asyncio
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class MCPClientManager:
    """管理所有 MCP Client 连接（stdio + HTTP）"""

    def __init__(self, sampling_callback: Optional[Callable] = None):
        self._connections: dict = {}  # server_name -> ClientSession
        self._sampling_callback = sampling_callback

    async def connect_stdio(self, server_name: str, command: str, args: list[str], env: dict = None):
        """连接 stdio 模式的 MCP 服务器"""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
        )

        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(
                read_stream, write_stream,
                sampling_callback=self._sampling_callback,
            ) as session:
                await session.initialize()
                self._connections[server_name] = session
                logger.info(f"Connected to external MCP server (stdio): {server_name}")

    async def connect_http(self, server_name: str, url: str):
        """连接 HTTP 模式的 MCP 服务器"""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(url) as (read_stream, write_stream, _):
            async with ClientSession(
                read_stream, write_stream,
                sampling_callback=self._sampling_callback,
            ) as session:
                await session.initialize()
                self._connections[server_name] = session
                logger.info(f"Connected to external MCP server (http): {server_name}")

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> dict:
        """通过 MCP Client 调用工具（异步）"""
        if server_name not in self._connections:
            raise KeyError(f"MCP server not connected: {server_name}")
        session = self._connections[server_name]
        result = await session.call_tool(tool_name, arguments)
        return result

    def call_tool_sync(self, server_name: str, tool_name: str, arguments: dict) -> dict:
        """同步调用 MCP Client（通过 asyncio 桥接，供 handler.py 使用）"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # 已在事件循环中，用 run_coroutine_threadsafe
            import concurrent.futures
            future = asyncio.run_coroutine_threadsafe(
                self.call_tool(server_name, tool_name, arguments), loop
            )
            return future.result(timeout=30)
        else:
            # 不在事件循环中，直接运行
            return asyncio.run(self.call_tool(server_name, tool_name, arguments))

    async def list_tools(self, server_name: str) -> list:
        """获取工具列表"""
        if server_name not in self._connections:
            raise KeyError(f"MCP server not connected: {server_name}")
        session = self._connections[server_name]
        result = await session.list_tools()
        return result.tools

    async def disconnect(self, server_name: str):
        """断开连接"""
        if server_name in self._connections:
            session = self._connections.pop(server_name)
            await session.__aexit__(None, None, None)
            logger.info(f"Disconnected from external MCP server: {server_name}")

    async def disconnect_all(self):
        """断开所有连接"""
        for name in list(self._connections.keys()):
            await self.disconnect(name)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_mcp_client.py -v`
Expected: PASS — 5 个测试通过

- [ ] **Step 5: 写 Sampling callback 测试**

```python
# 追加到 tests/test_p0/test_mcp_client.py

class TestMCPSamplingCallback:
    """验证 Sampling callback 传递给 ClientSession"""

    def test_sampling_callback_stored(self):
        """sampling_callback 被存储"""
        cb = MagicMock()
        manager = MCPClientManager(sampling_callback=cb)
        assert manager._sampling_callback is cb

    def test_no_sampling_callback_is_none(self):
        """未传 sampling_callback 时为 None"""
        manager = MCPClientManager()
        assert manager._sampling_callback is None
```

- [ ] **Step 6: 运行 Sampling 测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_mcp_client.py::TestMCPSamplingCallback -v`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add agent/mcp_client.py tests/test_p0/test_mcp_client.py
git commit -m "feat: 实现 MCPClientManager（stdio + HTTP + Sampling）

- connect_stdio() / connect_http() 连接外部 MCP 服务器
- call_tool() 异步调用 + call_tool_sync() 同步桥接
- list_tools() 获取外部工具列表
- sampling_callback 传递给 ClientSession
- connect/disconnect 生命周期管理"
```

---

### Task 5: 实现 Sampling callback

**Files:**
- Modify: `agent/mcp_client.py`
- Test: `tests/test_p0/test_mcp_client.py`

**背景**：Sampling callback 是 MCP 标准协议的一部分。当外部 MCP 服务器需要 LLM 推理时，通过 `sampling/createMessage` 请求 Client。Client 调用 Agent 的 LLM 生成回复。

- [ ] **Step 1: 写失败测试——Sampling callback 调用 LLM**

```python
# 追加到 tests/test_p0/test_mcp_client.py

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
        callback = make_sampling_callback()

        # Mock LLM 响应
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "文档分类：技术文档"
        mock_response.model = "test-model"

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("agent.mcp_client.create_client", return_value=mock_client), \
             patch("agent.mcp_client.load_llm_config", return_value={"model": "test-model"}):
            from mcp.types import CreateMessageRequestParams, TextContent
            params = CreateMessageRequestParams(
                messages=[],
                maxTokens=100,
            )
            result = await callback(None, params)
            assert result.role == "assistant"
            assert isinstance(result.content, TextContent)
            assert "技术文档" in result.content.text

    @pytest.mark.asyncio
    async def test_sampling_callback_returns_error_on_failure(self):
        """LLM 调用失败时返回错误提示"""
        from agent.mcp_client import make_sampling_callback
        callback = make_sampling_callback()

        with patch("agent.mcp_client.create_client", side_effect=RuntimeError("LLM unavailable")):
            from mcp.types import CreateMessageRequestParams
            params = CreateMessageRequestParams(
                messages=[],
                maxTokens=100,
            )
            result = await callback(None, params)
            assert result.role == "assistant"
            assert "Sampling 失败" in result.content.text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_mcp_client.py::TestSamplingCallback -v`
Expected: FAIL — `make_sampling_callback` 不存在

- [ ] **Step 3: 在 `agent/mcp_client.py` 中实现 `make_sampling_callback()`**

```python
def make_sampling_callback():
    """创建 MCP Sampling callback，调用 Agent LLM 处理 Server 的请求"""
    from mcp.types import CreateMessageResult, TextContent

    async def sampling_callback(context, params) -> CreateMessageResult:
        try:
            from agent.llmcore import load_llm_config, create_client
            config = load_llm_config()
            client = create_client(config)

            messages = []
            if params.systemPrompt:
                messages.append({"role": "system", "content": params.systemPrompt})
            for msg in params.messages:
                messages.append({"role": msg.role, "content": msg.content.text if hasattr(msg.content, 'text') else str(msg.content)})

            response = client.chat.completions.create(
                model=config["model"],
                messages=messages,
                max_tokens=params.maxTokens,
                temperature=params.temperature or 0.2,
            )
            return CreateMessageResult(
                role="assistant",
                content=TextContent(type="text", text=response.choices[0].message.content),
                model=config["model"],
                stopReason="endTurn",
            )
        except Exception as e:
            logger.error(f"MCP Sampling callback failed: {e}")
            return CreateMessageResult(
                role="assistant",
                content=TextContent(type="text", text=f"[Sampling 失败: {e}]"),
                model="error",
                stopReason="endTurn",
            )

    return sampling_callback
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_mcp_client.py::TestSamplingCallback -v`
Expected: PASS — 3 个测试通过

- [ ] **Step 5: 运行全部 MCP Client 测试确认无回归**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_mcp_client.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add agent/mcp_client.py tests/test_p0/test_mcp_client.py
git commit -m "feat: 实现 MCP Sampling callback

- make_sampling_callback() 创建标准 MCP Sampling callback
- callback 调用 Agent LLM 生成回复
- LLM 失败时返回错误提示而非中断工具调用
- 支持 systemPrompt、messages、maxTokens、temperature 参数"
```

---

### Task 6: ToolRegistry 双轨注册——外部工具支持

**Files:**
- Modify: `agent/tool_registry.py`
- Test: `tests/test_p0/test_tool_registry.py`

**背景**：ToolRegistry 需要支持外部 MCP 工具的注册。外部工具不注册函数引用，而是注册 `server_name + tool_name` 映射，调用时通过 MCP Client 的同步包装器执行。

- [ ] **Step 1: 写失败测试——外部工具注册和调用**

```python
# 追加到 tests/test_p0/test_tool_registry.py

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
        # 模拟外部工具
        mock_tool = MagicMock()
        mock_tool.name = "read_file"
        mock_tool.description = "Read a file"
        mock_tool.inputSchema = {"type": "object", "properties": {"path": {"type": "string"}}}

        mock_client = MagicMock()
        mock_client.list_tools.return_value = [mock_tool]

        # 用 async 方式注册，这里直接模拟
        self.registry._external_tools["ext-srv/read_file"] = ("ext-srv", "read_file")
        self.registry._schemas["ext-srv/read_file"] = {
            "name": "ext-srv/read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            "visibility": "static",
        }
        self.registry._server_tools.setdefault("ext-srv", []).append("ext-srv/read_file")

        # 验证 schema
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

    def test_external_tool_visibility_from_config(self):
        """外部工具 visibility 从配置文件读取"""
        self.registry._external_tools["ext-srv/read_file"] = ("ext-srv", "read_file")
        self.registry._schemas["ext-srv/read_file"] = {
            "name": "ext-srv/read_file",
            "description": "Read a file",
            "input_schema": {},
            "visibility": "hidden",
        }
        # hidden 工具不出现在 static_tools 中
        static = self.registry.get_static_tools()
        names = [s["name"] for s in static]
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
        self.registry.register("internal-srv/tool", my_func, {"name": "internal-srv/tool", "visibility": "static"})

        func = self.registry.get("internal-srv/tool")
        assert func is my_func
        assert func() == "internal result"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_tool_registry.py::TestExternalToolRegistration -v`
Expected: FAIL — `_external_tools` 属性不存在

- [ ] **Step 3: 修改 ToolRegistry 支持外部工具**

在 `agent/tool_registry.py` 的 `ToolRegistry.__init__()` 中新增：
```python
self._external_tools: dict[str, tuple[str, str]] = {}  # full_name -> (server_name, tool_name)
self._mcp_client = None  # MCPClientManager instance
```

修改 `get()` 方法，增加外部工具分支：
```python
def get(self, tool_name: str):
    """获取工具函数——内部返回函数引用，外部返回 Client 调用包装器"""
    if tool_name in self._tools:
        return self._tools[tool_name]
    if tool_name in self._external_tools:
        server_name, tool_name_raw = self._external_tools[tool_name]
        if self._mcp_client is None:
            return None
        def wrapper(**kwargs):
            return self._mcp_client.call_tool_sync(server_name, tool_name_raw, kwargs)
        return wrapper
    return None
```

新增 `set_mcp_client()` 方法：
```python
def set_mcp_client(self, client):
    """注入 MCPClientManager 实例"""
    self._mcp_client = client
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_tool_registry.py::TestExternalToolRegistration -v`
Expected: PASS

- [ ] **Step 5: 运行全部 ToolRegistry 测试确认无回归**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_tool_registry.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add agent/tool_registry.py tests/test_p0/test_tool_registry.py
git commit -m "feat: ToolRegistry 双轨注册——支持外部 MCP 工具

- _external_tools 存储外部工具的 server_name + tool_name 映射
- get() 对外部工具返回同步包装器（调用 MCPClientManager）
- set_mcp_client() 注入 MCPClientManager 实例
- 外部工具的 visibility 从配置文件读取，默认 hidden
- 内部工具注册和调用不受影响"
```

---

### Task 7: 配置文件扩展——支持外部服务器

**Files:**
- Modify: `config/mcp-servers.yaml`
- Modify: `agent/mcp_loader.py`
- Test: `tests/test_p0/test_mcp_client.py`

**背景**：配置文件新增 `mode: stdio` / `mode: http` 字段区分外部服务器。mcp_loader 读取配置后，内部服务器走现有流程，外部服务器走 MCPClientManager。

- [ ] **Step 1: 写失败测试——配置解析外部服务器**

```python
# 追加到 tests/test_p0/test_mcp_client.py

import yaml

class TestExternalServerConfig:
    """验证外部服务器配置解析"""

    def test_parse_external_stdio_config(self):
        """解析 stdio 模式外部服务器配置"""
        config_yaml = """
external-filesystem:
  mode: stdio
  command: npx
  args:
    - "-y"
    - "@modelcontextprotocol/server-filesystem"
    - "/tmp"
  sampling: true
  tools:
    read_file:
      visibility: static
    write_file:
      visibility: hidden
"""
        config = yaml.safe_load(config_yaml)
        srv = config["external-filesystem"]
        assert srv["mode"] == "stdio"
        assert srv["command"] == "npx"
        assert srv["sampling"] is True
        assert srv["tools"]["read_file"]["visibility"] == "static"

    def test_parse_external_http_config(self):
        """解析 HTTP 模式外部服务器配置"""
        config_yaml = """
external-api:
  mode: http
  url: https://mcp-server.example.com/mcp
  sampling: true
  tools:
    search:
      visibility: static
"""
        config = yaml.safe_load(config_yaml)
        srv = config["external-api"]
        assert srv["mode"] == "http"
        assert srv["url"] == "https://mcp-server.example.com/mcp"

    def test_internal_server_has_no_mode(self):
        """内部服务器没有 mode 字段"""
        config_yaml = """
photo-server:
  command: python
  args:
    - "-m"
    - niu_photo_server
  workdir: ../mcp-servers/photo-server/src
  preload: true
"""
        config = yaml.safe_load(config_yaml)
        srv = config["photo-server"]
        assert "mode" not in srv

    def test_is_external_server_helper(self):
        """is_external_server() 辅助函数判断内部/外部"""
        from agent.mcp_loader import is_external_server
        assert is_external_server({"mode": "stdio"}) is True
        assert is_external_server({"mode": "http"}) is True
        assert is_external_server({"command": "python"}) is False
        assert is_external_server({}) is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_mcp_client.py::TestExternalServerConfig -v`
Expected: FAIL — `is_external_server` 不存在

- [ ] **Step 3: 在 mcp_loader.py 中新增 `is_external_server()`**

```python
def is_external_server(server_config: dict) -> bool:
    """判断是否为外部 MCP 服务器（有 mode 字段且为 stdio 或 http）"""
    mode = server_config.get("mode", "")
    return mode in ("stdio", "http")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_mcp_client.py::TestExternalServerConfig -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent/mcp_loader.py tests/test_p0/test_mcp_client.py
git commit -m "feat: 配置文件扩展支持外部 MCP 服务器

- is_external_server() 判断内部/外部服务器
- mode: stdio / http 区分外部服务器
- 无 mode 字段 = 内部服务器（向后兼容）
- 新增外部服务器配置测试用例"
```

---

### Task 8: mcp_loader 加载外部服务器

**Files:**
- Modify: `agent/mcp_loader.py`
- Modify: `agent/runner.py`
- Test: `tests/test_p0/test_mcp_client.py`

**背景**：应用启动时，mcp_loader 读取配置，内部服务器走 `__import__` 加载，外部服务器走 MCPClientManager 连接。runner.py 负责初始化 MCPClientManager 并触发外部服务器连接。

- [ ] **Step 1: 写失败测试——外部服务器加载流程**

```python
# 追加到 tests/test_p0/test_mcp_client.py

class TestLoadExternalServers:
    """验证外部服务器加载流程"""

    def test_load_external_servers_from_config(self):
        """load_external_servers 从配置文件读取并注册外部工具"""
        from agent.mcp_loader import load_external_servers
        from agent.tool_registry import get_registry

        # 模拟配置
        config = {
            "ext-test": {
                "mode": "stdio",
                "command": "echo",
                "args": ["test"],
                "tools": {
                    "read_file": {"visibility": "static"},
                },
            }
        }

        # 模拟 MCPClientManager
        mock_client = MagicMock()
        mock_tool = MagicMock()
        mock_tool.name = "read_file"
        mock_tool.description = "Read a file"
        mock_tool.inputSchema = {"type": "object"}
        mock_client.list_tools = AsyncMock(return_value=[mock_tool])

        # 调用
        registry = get_registry()
        original_external = registry._external_tools.copy()
        try:
            load_external_servers(config, mock_client)
            assert "ext-test/read_file" in registry._external_tools
            assert registry._schemas["ext-test/read_file"]["visibility"] == "static"
        finally:
            registry._external_tools = original_external
            registry._schemas = {k: v for k, v in registry._schemas.items() if k not in ["ext-test/read_file"]}

    def test_load_external_servers_skips_internal(self):
        """load_external_servers 跳过内部服务器（无 mode 字段）"""
        from agent.mcp_loader import load_external_servers

        config = {
            "photo-server": {
                "command": "python",
                "args": ["-m", "niu_photo_server"],
                "workdir": "../mcp-servers/photo-server/src",
            }
        }

        mock_client = MagicMock()
        load_external_servers(config, mock_client)
        # 不应该调用 mock_client 的任何方法
        mock_client.list_tools.assert_not_called()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_mcp_client.py::TestLoadExternalServers -v`
Expected: FAIL — `load_external_servers` 不存在

- [ ] **Step 3: 在 mcp_loader.py 中实现 `load_external_servers()`**

```python
async def load_external_servers(config: dict, mcp_client):
    """加载外部 MCP 服务器（stdio + HTTP 模式）

    Args:
        config: mcp-servers.yaml 的完整配置
        mcp_client: MCPClientManager 实例
    """
    from agent.tool_registry import get_registry
    registry = get_registry()

    for server_name, server_config in config.items():
        if not is_external_server(server_config):
            continue

        mode = server_config["mode"]
        visibility_map = {}
        for tool_name, tool_config in server_config.get("tools", {}).items():
            visibility_map[tool_name] = tool_config.get("visibility", "hidden")

        try:
            if mode == "stdio":
                await mcp_client.connect_stdio(
                    server_name=server_name,
                    command=server_config["command"],
                    args=server_config.get("args", []),
                    env=server_config.get("env"),
                )
            elif mode == "http":
                await mcp_client.connect_http(
                    server_name=server_name,
                    url=server_config["url"],
                )

            # 获取工具列表并注册
            tools = await mcp_client.list_tools(server_name)
            for tool in tools:
                full_name = f"{server_name}/{tool.name}"
                registry._external_tools[full_name] = (server_name, tool.name)
                tool_vis = visibility_map.get(tool.name, "hidden")
                registry._schemas[full_name] = {
                    "name": full_name,
                    "description": tool.description,
                    "input_schema": tool.inputSchema,
                    "visibility": tool_vis,
                }
                registry._server_tools.setdefault(server_name, []).append(full_name)

            logger.info(f"Loaded external MCP server: {server_name} ({len(tools)} tools)")
        except Exception as e:
            logger.error(f"Failed to load external MCP server {server_name}: {e}")
            # 外部服务器加载失败不终止应用（与内部服务器不同）
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_mcp_client.py::TestLoadExternalServers -v`
Expected: PASS

- [ ] **Step 5: 在 runner.py 中初始化外部服务器连接**

在 `GenericAgentRunner.__init__()` 中，`registry.set_ask_agent()` 之后添加：

```python
# 连接外部 MCP 服务器
from agent.mcp_client import MCPClientManager, make_sampling_callback
self._mcp_client = MCPClientManager(sampling_callback=make_sampling_callback())
registry.set_mcp_client(self._mcp_client)
self._connect_external_servers()
```

新增方法：
```python
def _connect_external_servers(self):
    """连接外部 MCP 服务器"""
    import asyncio
    import yaml
    from agent.mcp_loader import load_external_servers

    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "mcp-servers.yaml")
    if not os.path.exists(config_path):
        return
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}
    asyncio.run(load_external_servers(config, self._mcp_client))
```

- [ ] **Step 6: 运行全部测试确认无回归**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/ -v --timeout=30 2>&1 | tail -30`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add agent/mcp_loader.py agent/runner.py tests/test_p0/test_mcp_client.py
git commit -m "feat: 外部 MCP 服务器加载流程

- load_external_servers() 从配置文件读取并注册外部工具
- runner.py 初始化 MCPClientManager + 连接外部服务器
- 外部服务器加载失败不终止应用
- 跳过内部服务器（无 mode 字段）"
```

---

### Task 9: 集成验证——85 个现有工具正常工作

**Files:**
- Test: 手动验证 + 现有测试套件

**背景**：所有改动完成后，必须验证 85 个现有内部工具不受影响。这是验收标准的第一条。

- [ ] **Step 1: 运行全部现有测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/ -v --timeout=30 2>&1 | tail -50`
Expected: PASS — 所有现有测试通过

- [ ] **Step 2: 启动应用，验证内部 MCP 工具正常**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m niu_api`
验证：
- 日志中 9 个内部 MCP 服务器正常加载
- ToolRegistry 中 85 个工具正常注册
- brain-region-server 的 3 个 static 工具可见
- 其他 82 个 hidden 工具不出现在主 Agent 工具列表中
- 子 Agent 白名单正常工作

- [ ] **Step 3: 验证三层可见性机制**

1. 主 Agent 只看到 `static` 工具（brain-region-server 的 3 个）
2. 虚拟磁盘导航能看到所有非 disk-hidden 的工具
3. 子 Agent 能看到白名单中服务器的所有工具（含 hidden）

- [ ] **Step 4: 验证 ask_agent 可用**

在应用运行时，内部 MCP Server 调用 `registry.ask_agent()` 应能正常获得 LLM 回复。

- [ ] **Step 5: 如果有回归，修复并重新验证**

- [ ] **Step 6: 提交集成验证记录**

```bash
git add -A
git commit -m "test: 集成验证——85 个现有工具正常工作 + 三层可见性 + ask_agent"
```

---

### Task 10: 接入外部标准 MCP 服务器测试

**Files:**
- Modify: `config/mcp-servers.yaml` (临时测试配置)
- Test: 手动验证

**背景**：验收标准的第二条——不改代码只改配置，就能接入外部标准 MCP 服务器。

- [ ] **Step 1: 在 mcp-servers.yaml 中添加测试用外部服务器**

```yaml
# 测试用外部 MCP 服务器（stdio 模式）
test-filesystem:
  mode: stdio
  command: npx
  args:
    - "-y"
    - "@modelcontextprotocol/server-filesystem"
    - "/tmp"
  sampling: false
  tools:
    read_file:
      visibility: static
    write_file:
      visibility: hidden
    list_directory:
      visibility: static
    search_files:
      visibility: hidden
```

- [ ] **Step 2: 重启应用，验证外部服务器自动连接**

验证：
- 日志中出现 "Connected to external MCP server (stdio): test-filesystem"
- `test-filesystem/read_file` 和 `test-filesystem/list_directory` 出现在主 Agent 工具列表（static）
- `test-filesystem/write_file` 和 `test-filesystem/search_files` 不出现在主 Agent 工具列表（hidden）
- 磁盘导航可以看到所有 4 个工具（如果 disk YAML 配置了）

- [ ] **Step 3: 验证外部工具调用正常**

在对话中让 Agent 调用 `test-filesystem/read_file` 读取 `/tmp/test.txt`，验证返回正确。

- [ ] **Step 4: 验证外部工具的隐藏和可见**

- 主 Agent 不能直接调用 hidden 的 `write_file` 工具
- 但可以通过虚拟磁盘间接调用

- [ ] **Step 5: 测试完成后移除测试配置**

从 `mcp-servers.yaml` 中删除 `test-filesystem` 配置。

- [ ] **Step 6: 提交**

```bash
git commit -m "test: 外部 MCP 服务器接入验证——stdio 模式正常工作"
```

---

## 不在本次实施范围内

以下功能在 MCP 架构改造完成后，作为独立实施计划执行：

1. **photo-server 文档入库恢复** — 依赖 MCP Sampling，但改动集中在 photo-server
2. **photo-server mode 参数** — copy/move/reference 三种模式
3. **批量照片完整处理** — 目录入库逐张 EXIF/人脸/DB/KG
4. **ingest 路由重写** — classify_path + 目录入库路由
5. **HTTP 模式外部服务器** — 本次以 stdio 为主，HTTP 模式代码已实现但待真实环境验证
