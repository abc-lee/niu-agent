# 修复 LiteLLM 错误和 Tool Registry 问题

> **给代理工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 来逐任务实施此计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 修复 Anthropic API 错误 2013（tool call result does not follow tool call）并适配 ToolRegistry 支持 MCP 标准的 call_tool() 模式。

**架构：**
1. 确保 agent_loop.py 中每个 tool_call 都有对应的 tool_result 消息
2. 修改 ToolRegistry 使其能检测并包装 MCP call_tool() 处理器，无需为每个工具单独定义函数

**技术栈：** Python, LiteLLM, Anthropic API, MCP 协议

---

## 问题分析

### 问题 1：LiteLLM API 错误 2013

**根本原因：** `agent/generic/agent_loop.py:190-196` 使用条件判断 `if outcome.data is not None`，导致某些 tool_call 缺少对应的 tool_result 消息。

**Anthropic API 要求：** 每个 tool_call 必须紧跟一个 tool_result 消息。

**影响：** 对话崩溃，报错 `litellm.BadRequestError: tool call result does not follow tool call (2013)`

**注意：** 此修复是防御性的，即使发生问题 0，也能避免 API 崩溃，让 LLM 看到错误提示并自我纠正。

### 问题 2：Tool function not found 警告

**根本原因：** 提交 `ed4c46f` 只添加了 `get_tool_schemas()`，但没有添加工具函数。ToolRegistry 期望：
- 模块级函数名与工具名匹配，或
- `get_tool_function(name)` 方法

**影响：** memory、scheduler、file-parser、session-manager 等工具无法注册可调用函数。

**历史背景：** 以前的架构使用 stdio 进程间通信（MCP 标准），通过统一的 `call_tool()` 处理器。迁移到同进程 ToolRegistry 时不完整。

---

## 文件结构

```
agent/
├── generic/
│   └── agent_loop.py          # 修复 tool_result 处理（第 186-197 行）
└── tool_registry.py            # 添加 MCP call_tool() 支持（第 99-122 行）

tests/
├── test_agent_loop_tool_results.py   # 测试 tool_result 完整性
└── test_tool_registry_mcp.py         # 测试 call_tool() 包装
```

---

## 任务 1：编写 tool_result 完整性的失败测试

**文件：**
- 创建：`tests/test_agent_loop_tool_results.py`

- [ ] **步骤 1：编写缺失 tool_result 的失败测试**

```python
"""测试每个 tool_call 都有对应的 tool_result 消息。"""
import pytest
from unittest.mock import Mock, MagicMock
from agent.generic.agent_loop import agent_runner_loop


def test_tool_result_for_none_data():
    """当 outcome.data 为 None 时，仍应添加空的 tool_result。"""
    # 设置模拟客户端
    client = Mock()
    client.last_tools = ""

    # 模拟 LLM 响应包含工具调用
    mock_response = Mock()
    mock_response.content = "测试中"
    mock_response.tool_calls = [
        Mock(
            id="call_123",
            function=Mock(name="unknown_tool", arguments="{}")
        )
    ]

    # 模拟 handler 返回 None 数据
    handler = Mock()
    handler.dispatch = Mock(return_value=Mock(
        data=None,
        next_prompt="Unknown tool: unknown_tool",
        exit_code=None
    ))

    # 收集消息
    messages = []

    def capture_messages(**kwargs):
        messages.extend(kwargs.get("messages", []))
        # 模拟 LLM 响应
        return mock_response

    client.chat = capture_messages

    # 运行一次迭代
    gen = agent_runner_loop(
        client=client,
        system_prompt="测试",
        user_input="测试输入",
        handler=handler,
        tools_schema=[],
        max_turns=1
    )

    try:
        list(gen)
    except StopIteration:
        pass

    # 验证消息结构
    # 应该有：user -> assistant(tool_calls) -> tool -> user
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    tool_msgs = [m for m in messages if m.get("role") == "tool"]

    assert len(assistant_msgs) == 1
    assert len(assistant_msgs[0].get("tool_calls", [])) == 1

    # 关键：即使 data 为 None，也必须有 tool 消息
    assert len(tool_msgs) == 1, "预期有 tool 消息对应 tool_call"
    assert tool_msgs[0]["tool_call_id"] == "call_123"
    assert tool_msgs[0]["content"] == ""  # 空但存在
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/test_agent_loop_tool_results.py::test_tool_result_for_none_data -v`

预期：失败，错误信息 "Expected tool message for tool_call"（当前代码在 data 为 None 时不添加 tool_result）

---

## 任务 2：修复 agent_loop.py 确保 tool_result 完整性

**文件：**
- 修改：`agent/generic/agent_loop.py:186-197`

- [ ] **步骤 3：读取当前实现**

运行：`grep -A 15 "if outcome.data is not None:" agent/generic/agent_loop.py`

- [ ] **步骤 4：修改代码为 None 数据添加 tool_result**

```python
# 文件：agent/generic/agent_loop.py
# 行号：186-197

# 修改前（有bug）：
if outcome.next_prompt.startswith("未知工具"):
    client.last_tools = ""
if outcome.data is not None:
    datastr = (
        json.dumps(outcome.data, ensure_ascii=False, default=json_default)
        if type(outcome.data) in [dict, list]
        else str(outcome.data)
    )
    tool_results.append({"tool_use_id": tid, "content": datastr})
next_prompts.add(outcome.next_prompt)

# 修改后（修复）：
if outcome.next_prompt.startswith("未知工具"):
    client.last_tools = ""

# 关键：Anthropic API 要求每个 tool_call 都有 tool_result
# 即使 outcome.data 为 None，也必须添加空的 tool_result
if outcome.data is not None:
    datastr = (
        json.dumps(outcome.data, ensure_ascii=False, default=json_default)
        if type(outcome.data) in [dict, list]
        else str(outcome.data)
    )
    tool_results.append({"tool_use_id": tid, "content": datastr})
else:
    # 添加空的 tool_result 以满足 Anthropic API 要求
    tool_results.append({"tool_use_id": tid, "content": ""})

next_prompts.add(outcome.next_prompt)
```

- [ ] **步骤 5：运行测试验证修复**

运行：`pytest tests/test_agent_loop_tool_results.py::test_tool_result_for_none_data -v`

预期：通过

---

## 任务 3：编写 MCP call_tool() 包装器的失败测试

**文件：**
- 创建：`tests/test_tool_registry_mcp.py`

- [ ] **步骤 6：编写 call_tool() 包装测试**

```python
"""测试 ToolRegistry 对 MCP call_tool() 模式的支持。"""
import pytest
from agent.tool_registry import ToolRegistry, get_registry


def test_wrap_call_tool_handler():
    """ToolRegistry 应该包装 call_tool() 当单独的函数不存在时。"""

    # 创建模拟 MCP 模块，有 call_tool 但没有单独的函数
    mock_module = Mock()

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
    assert tool_fn is not None
    result = tool_fn(param1="value1")
    assert result == {"status": "success", "data": {"param1": "value1"}}


def test_fallback_to_module_level_function():
    """ToolRegistry 应该优先使用模块级函数如果它们存在。"""

    mock_module = Mock()

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
```

- [ ] **步骤 7：运行测试验证失败**

运行：`pytest tests/test_tool_registry_mcp.py::test_wrap_call_tool_handler -v`

预期：失败（当前 ToolRegistry 不支持 call_tool 模式）

---

## 任务 4：实现 ToolRegistry 的 MCP call_tool() 支持

**文件：**
- 修改：`agent/tool_registry.py:99-122`

- [ ] **步骤 8：读取当前 register_server 实现**

运行：`grep -A 30 "def register_server" agent/tool_registry.py`

- [ ] **步骤 9：实现 call_tool() 包装器逻辑**

```python
# 文件：agent/tool_registry.py
# 行号：99-122（替换整个 register_server 方法）

def register_server(self, server_name: str, module):
    """
    从 MCP 服务器模块注册工具。

    支持三种模式（按顺序尝试）：
    1. 与工具名匹配的模块级函数
    2. get_tool_function(name) 方法
    3. call_tool(tool_name, arguments) 处理器（MCP 标准模式）
    """
    # 获取工具 schemas
    if hasattr(module, "get_tool_schemas"):
        schemas = module.get_tool_schemas()
    elif hasattr(module, "TOOL_SCHEMAS"):
        schemas = list(module.TOOL_SCHEMAS.values())
    else:
        logger.warning(f"{server_name} 中没有 TOOL_SCHEMAS 或 get_tool_schemas()")
        return

    registered_count = 0

    for schema in schemas:
        tool_name = schema.get("name")
        if not tool_name:
            continue

        full_name = f"{server_name}/{tool_name}"

        # 尝试获取工具函数（模式 1 & 2）
        tool_fn = None
        if hasattr(module, tool_name):
            tool_fn = getattr(module, tool_name)
        elif hasattr(module, "get_tool_function"):
            tool_fn = module.get_tool_function(tool_name)

        # 模式 3：如果没找到单独的函数，包装 call_tool()
        if tool_fn is None and hasattr(module, "call_tool"):
            # 创建一个包装函数来调用 call_tool
            def make_wrapper(server_module, name):
                def wrapper(**kwargs):
                    return server_module.call_tool(name, kwargs)
                return wrapper

            tool_fn = make_wrapper(module, tool_name)
            logger.debug(f"为 {full_name} 包装了 call_tool()")

        # 如果找到或创建了函数，则注册
        if tool_fn and callable(tool_fn):
            self._tools[full_name] = tool_fn
            self._schemas.append(schema)
            registered_count += 1
        else:
            logger.warning(f"找不到工具函数: {full_name}")

    # 存储 schemas
    if schemas:
        self._server_schemas[server_name] = schemas
        logger.info(f"从 {server_name} 注册了 {registered_count} 个工具")
```

- [ ] **步骤 10：运行测试验证修复**

运行：`pytest tests/test_tool_registry_mcp.py -v`

预期：两个测试都通过

---

## 任务 5：用集成测试验证完整修复

**文件：**
- 创建：`tests/test_integration_tool_flow.py`

- [ ] **步骤 11：编写集成测试**

```python
"""集成测试：从 agent_loop 到 ToolRegistry 的完整工具调用流程。"""
import pytest
from unittest.mock import Mock
from agent.generic.agent_loop import agent_runner_loop


def test_full_tool_call_flow_with_unknown_tool():
    """测试未知工具不会导致 API 错误。"""

    # 设置
    client = Mock()
    client.last_tools = ""

    # LLM 尝试调用未知工具
    mock_response = Mock()
    mock_response.content = ""
    mock_response.tool_calls = [
        Mock(
            id="call_unknown",
            function=Mock(name="nonexistent_tool", arguments="{}")
        )
    ]

    handler = Mock()
    handler.dispatch = Mock(return_value=Mock(
        data=None,
        next_prompt="Unknown tool: nonexistent_tool",
        exit_code=None
    ))

    call_count = [0]
    messages_sent = []

    def mock_chat(**kwargs):
        call_count[0] += 1
        msgs = kwargs.get("messages", [])
        messages_sent.extend(msgs)

        # 第一次调用：LLM 发起工具调用
        if call_count[0] == 1:
            return mock_response
        # 第二次调用：LLM 响应工具结果
        else:
            return Mock(content="我看到工具是未知的", tool_calls=None)

    client.chat = mock_chat

    # 运行
    gen = agent_runner_loop(
        client=client,
        system_prompt="测试",
        user_input="测试",
        handler=handler,
        tools_schema=[],
        max_turns=2
    )

    try:
        list(gen)
    except StopIteration:
        pass

    # 验证消息结构对 Anthropic API 有效
    # 找到包含 tool_calls 的 assistant 消息
    assistant_with_tools = None
    for msg in messages_sent:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            assistant_with_tools = msg
            break

    assert assistant_with_tools is not None

    # 找到对应的 tool 消息
    tool_call_id = assistant_with_tools["tool_calls"][0]["id"]
    tool_msg = None
    for msg in messages_sent:
        if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
            tool_msg = msg
            break

    # 关键：必须有 tool 消息
    assert tool_msg is not None, "缺少 tool_result 消息"
    assert tool_msg["content"] == ""  # 空但存在

    # 验证序列：assistant -> tool -> user
    msg_sequence = [m.get("role") for m in messages_sent]
    assert "assistant" in msg_sequence
    assert "tool" in msg_sequence
```

- [ ] **步骤 12：运行集成测试**

运行：`pytest tests/test_integration_tool_flow.py -v`

预期：通过

---

## 任务 6：清理和文档化

**文件：**
- 修改：`agent/tool_registry.py`（添加文档字符串）
- 创建：`docs/TOOL_REGISTRY_MCP_SUPPORT.md`

- [ ] **步骤 13：为 ToolRegistry 添加全面的文档字符串**

```python
# 添加到 ToolRegistry 类文档字符串
"""
ToolRegistry - MCP 工具函数的中心注册表。

支持三种工具函数模式：

1. **模块级函数**（推荐用于新服务器）：
   ```python
   # 在 mcp-server/src/niu_server/__init__.py 中
   def my_tool(arg1: str):
       return {"result": arg1}

   TOOL_SCHEMAS = {"my_tool": {...}}
   ```

2. **get_tool_function() 方法**：
   ```python
   def get_tool_function(tool_name: str):
       handlers = {"tool1": handler1, "tool2": handler2}
       return handlers.get(tool_name)
   ```

3. **call_tool() 处理器**（MCP 标准模式，自动包装）：
   ```python
   @server.call_tool()
   def call_tool(name: str, arguments: dict):
       # 所有工具的统一处理器
       if name == "tool1":
           return handle_tool1(arguments)
       ...
   ```

模式 3 允许现有 MCP 服务器无需修改即可工作。
ToolRegistry 会自动创建包装函数。
"""
```

- [ ] **步骤 14：创建迁移文档**

```markdown
# Tool Registry MCP 支持

## 概述

ToolRegistry 支持使用标准 `call_tool()` 模式的 MCP 服务器，无需为每个工具单独定义函数。

## 模式检测顺序

注册工具时，ToolRegistry 按以下顺序尝试：

1. 与工具名匹配的模块级函数
2. `get_tool_function(tool_name)` 方法
3. `call_tool(tool_name, arguments)` 处理器（自动包装）

## 示例：包装 call_tool()

```python
# mcp-server/memory-server/src/niu_memory_server/__init__.py

@server.call_tool()
def call_tool(name: str, arguments: dict):
    """MCP 标准模式 - 所有工具的单一处理器。"""
    if name == "remember":
        return remember_handler(arguments)
    elif name == "recall":
        return recall_handler(arguments)
    ...

TOOL_SCHEMAS = {
    "remember": {...},
    "recall": {...},
}
```

ToolRegistry 会自动创建调用 `call_tool()` 的包装函数。

## 迁移指南

**之前（stdio 模式）**：MCP 服务器只需要 `call_tool()` 和 `@server.call_tool()` 装饰器。

**之后（ToolRegistry 模式）**：同样的代码可以工作！无需迁移。ToolRegistry 检测 `call_tool()` 并创建包装器。

## 性能

- 进程内调用：10 次工具调用约 0 秒
- stdio 模式：10 次工具调用约 40 秒
- 提升：约 40 倍加速
```

- [ ] **步骤 15：提交所有更改**

```bash
git add agent/generic/agent_loop.py agent/tool_registry.py tests/ docs/
git commit -m "fix: 确保每个 tool_call 都有 tool_result 并添加 MCP call_tool() 支持

- 修复 Anthropic API 错误 2013，即使 outcome.data 为 None 也添加 tool_result
- 添加 ToolRegistry 对 MCP 标准 call_tool() 模式的支持
- 当单独的函数不存在时自动包装 call_tool() 处理器
- 消除 memory、scheduler、file-parser 服务器的 'Tool function not found' 警告

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## 任务 7：验证警告已消除

- [ ] **步骤 16：重启服务并检查日志**

```bash
# 清理 Python 缓存
find . -name "*.pyc" -delete
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# 重启服务
go run main.go

# 检查日志中的警告
# 预期：没有 memory-server、scheduler-server 等的 "Tool function not found" 警告
```

---

## 验证清单

- [ ] 所有测试通过：`pytest tests/test_agent_loop_tool_results.py tests/test_tool_registry_mcp.py tests/test_integration_tool_flow.py -v`
- [ ] 生产日志中没有 "tool call result does not follow tool call" 错误
- [ ] 启动时没有 "Tool function not found" 警告
- [ ] Memory 工具（remember、recall）正常工作
- [ ] Scheduler 工具（schedule_task、list_scheduled_tasks）正常工作
- [ ] 真实 MCP 服务器的集成测试通过

---

## 实施后

**问题 3 状态：** 已延期。Skills 动态注入时机和逻辑问题将在验证问题 1 和 2 解决后在单独的计划中处理。

**下一步：**
1. 用真实对话测试修复
2. 监控任何边缘情况
3. 为问题 3 创建单独的计划（Skills 注入时机优化）
