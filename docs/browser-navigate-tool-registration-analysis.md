# browser_navigate MCP 工具注册流程分析报告

## 执行时间
2026-04-12 15:05 - 15:08

## 检查结果总结

### ✓ 所有检查点通过

| 检查点 | 状态 | 详情 |
|--------|------|------|
| 1. browser-server 模块导出 | ✓ 通过 | `get_tool_schemas()` 和 `browser_navigate()` 函数正确导出 |
| 2. ToolRegistry 注册 | ✓ 通过 | browser-server 成功注册 1 个工具 |
| 3. MCP Loader 加载 | ✓ 通过 | `load_mcp_tools()` 成功加载 9 个服务器，共 67 个工具 |
| 4. BASE_MCP_TOOLS 配置 | ✓ 通过 | browser_navigate 在 BASE_MCP_TOOLS 列表中（第 12 个） |
| 5. 工具 schema 传递 | ✓ 通过 | NiuRunner 成功注入 67 个工具 schema |
| 6. 实际工具调用 | ✓ 通过 | API 成功调用 browser_navigate 访问 https://example.com |

---

## 详细分析

### 1. browser-server 模块导出

**文件位置**: `mcp-servers/browser-server/src/niu_browser_server/__init__.py`

**导出内容**:
- `get_tool_schemas()` - 返回工具 schema 列表（1 个工具）
- `browser_navigate()` - 浏览器导航工具函数

**工具 Schema**:
```python
{
    "name": "browser_navigate",
    "description": "浏览器导航工具...",
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "目标 URL"},
            "wait_until": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                "default": "domcontentloaded"
            }
        },
        "required": ["url"]
    }
}
```

**状态**: ✓ 成功导出

---

### 2. ToolRegistry 注册

**文件位置**: `agent/tool_registry.py`

**注册流程**:
```python
# 在 mcp_loader.py 中
module = __import__("niu_browser_server", fromlist=["get_tool_schemas"])
registry.register_server("browser-server", module)
```

**注册结果**:
```
DEBUG | agent.tool_registry:register_server:107 - Registered tool: browser-server/browser_navigate
INFO  | agent.tool_registry:register_server:114 - Registered 1 tools from browser-server
```

**工具验证**:
- `registry.has_tool("browser-server/browser_navigate")` → True
- `registry.get("browser-server/browser_navigate")` → 返回工具函数对象

**状态**: ✓ 成功注册

---

### 3. MCP Loader 加载

**文件位置**: `agent/mcp_loader.py`

**加载流程**:
```python
# REQUIRED_SERVERS 列表中包含 browser-server
REQUIRED_SERVERS = [
    ...
    ("browser-server", "niu_browser_server"),
]

# load_mcp_tools() 函数
registry = load_mcp_tools()
# 返回 ToolRegistry 实例，包含所有工具
```

**加载结果**:
```
INFO | agent.mcp_loader:load_mcp_tools:120 - All 9 servers loaded
INFO | agent.tool_registry:set_registry:205 - Set global ToolRegistry instance
```

**状态**: ✓ 成功加载

---

### 4. BASE_MCP_TOOLS 配置

**文件位置**: `agent/runner.py`

**配置内容**:
```python
BASE_MCP_TOOLS = [
    # memory-server (6个)
    "memory-server/remember",
    "memory-server/recall",
    ...

    # vector-store (5个)
    "vector-store/add_document",
    ...

    # browser-server (1个)
    "browser-server/browser_navigate",  # ← 第 12 个
]
```

**验证结果**:
- browser_navigate 在 BASE_MCP_TOOLS 中
- 位置：第 12 个（共 12 个基础工具）

**状态**: ✓ 配置正确

---

### 5. 工具 schema 传递

**文件位置**: `niu_api/chat.py`

**传递流程**:
```python
# API 启动时（niu_api/__main__.py）
tool_registry = load_mcp_tools()
init_runner(tool_registry)

# chat.py
def init_runner(tool_registry):
    runner = get_runner(llm_config=llm_config, mcp_client=None)
    mcp_tools_schema = tool_registry.get_schemas()
    runner.set_mcp_tools_schema(mcp_tools_schema)
```

**传递结果**:
```
[NiuRunner] Loaded 67 MCP tools
```

**状态**: ✓ 成功传递

---

### 6. chat() 中的工具注入

**文件位置**: `agent/runner.py`

**注入逻辑**:
```python
def chat(self, session_id: str, user_input: str, ...):
    # 1. 获取 base_tools_schema（内置工具）
    tools_schema = self.base_tools_schema.copy()

    # 2. 固定注入 BASE_MCP_TOOLS
    for tool_name in BASE_MCP_TOOLS:
        schema = self._get_tool_schema_by_name(tool_name)
        if schema:
            tools_schema.append(schema)

    # 3. 传递给 agent_runner_loop
    gen = agent_runner_loop(
        ...
        tools_schema=tools_schema,  # ← 最终工具列表
        ...
    )
```

**注入结果**:
- base_tools_schema: 9 个（内置工具）
- 注入的基础 MCP 工具: 12 个（包括 browser_navigate）
- **总工具数量: 21 个**

**状态**: ✓ 成功注入

---

### 7. Handler 工具调用

**文件位置**: `agent/handler.py`

**调用流程**:
```python
def dispatch(self, tool_name, args, response, index=0):
    # 检查 MCP 工具（工具名格式：server/tool）
    if "/" in tool_name:
        from agent.tool_registry import get_registry
        func = get_registry().get(tool_name)

        if func is None:
            yield f"[MCP Error] Tool not found: {tool_name}\n"
            return StepOutcome(...)

        # 直接调用工具函数
        result = func(**args)
        yield f"[MCP] {tool_name} executed\n"
        return StepOutcome(result, ...)
```

**实际调用测试**:
- 发送消息："使用 browser_navigate 工具访问 https://example.com"
- LLM 成功调用 browser_navigate
- 日志显示："Browser started successfully"

**状态**: ✓ 成功调用

---

## API 日志分析

### 启动日志（关键部分）

```
INFO | agent.mcp_loader:load_mcp_tools - All 9 servers loaded
INFO | agent.tool_registry:set_registry - Set global ToolRegistry instance
INFO | niu_api.__main__:lifespan - MCP tools loaded: 67 tools
[NiuRunner] Loaded 67 MCP tools
```

### 工具调用日志

```
INFO | niu_browser_server:_start_browser - Browser started successfully
```

**结论**: 工具在 API 启动时成功加载，运行时可以正常调用。

---

## 最终工具列表

### 固定注入的工具（BASE_MCP_TOOLS，共 12 个）

1. memory-server/remember
2. memory-server/recall
3. memory-server/update_memory
4. memory-server/get_memory_stats
5. memory-server/cleanup_memories
6. memory-server/link_memories
7. vector-store/add_document
8. vector-store/search_documents
9. vector-store/get_document
10. vector-store/delete_document
11. vector-store/list_documents
12. **browser-server/browser_navigate** ← 已包含

### 内置工具（base_tools_schema，共 9 个）

- bash
- read
- write
- edit
- glob
- grep
- web_search
- web_fetch
- chat-with-file-processor
- chat-with-event-manager
- chat-with-context-manager

**总计**: 21 个工具（12 个基础 MCP + 9 个内置）

---

## 结论

### ✓ browser_navigate 工具注册流程完整

1. **模块导出**: browser-server 正确导出 `get_tool_schemas()` 和 `browser_navigate()`
2. **ToolRegistry 注册**: 工具成功注册到全局 ToolRegistry
3. **MCP Loader 加载**: API 启动时成功加载 browser-server
4. **BASE_MCP_TOOLS 配置**: browser_navigate 在基础工具列表中
5. **工具 schema 传递**: NiuRunner 成功接收 67 个工具 schema
6. **实际工具调用**: API 可以成功调用 browser_navigate 工具

### 工具可用性

browser_navigate 工具已正确注册到系统中，应该出现在 LLM 的可用工具列表中。

**如果 LLM 没有调用该工具，可能的原因**:
1. LLM 认为当前任务不需要浏览器工具
2. LLM 选择使用其他工具
3. 提示词中未明确要求使用浏览器工具

**建议**:
- 在用户请求中明确提及"浏览器"、"导航"、"打开网页"等关键词
- 提供明确的 URL
- 观察日志中的工具调用记录

---

## 测试脚本

已创建以下测试脚本用于验证：

1. `scripts/test_browser_tool_registration.py` - 检查工具注册流程
2. `scripts/test_browser_tool_chain.py` - 检查完整调用链
3. `scripts/test_api_browser_tool.py` - 检查实际 API 调用

所有测试均通过。
