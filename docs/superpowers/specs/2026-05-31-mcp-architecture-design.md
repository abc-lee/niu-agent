# MCP 架构改造设计

## 验收标准

1. **内部 MCP 服务器**正常使用（现有 10 个服务器、85 个工具）
2. **外部标准 MCP 服务器**——不改代码，只改配置文件就能接入
3. **MCP Sampling** 标准——工具调用过程中能向 Agent 请求 LLM 推理（如文档分类）
4. **性能不能回退**——同进程调用仍是最优路径

## 当前问题

| 问题 | 说明 |
|------|------|
| 无 MCP Client | 当前只有 Server 端代码，没有 ClientSession 实现 |
| 同进程无 session | ToolRegistry 直接 `func(**args)` 调用，不传 MCP context/session |
| 无法支持外部 MCP | 外部 stdio/HTTP MCP 服务器无法接入（没有 Client） |
| 无法 Sampling | 工具调用过程中无法请求 Agent LLM 推理 |
| 不符合 MCP 标准 | 当前架构绕过了 MCP 协议，直接 Python 函数调用 |

## 现有实现分析

### 同进程模式（内部服务器）
- `mcp_loader.py` 直接 `__import__` 加载模块
- `tool_registry.py` 注册函数引用，`func(**args)` 调用
- 无 MCP 协议层（无 initialize、无 JSON-RPC、无 session）
- 性能最优（直接函数调用，~40000x 快于 stdio）

### 浏览器持久化
- 不走 MCP 协议
- 用 Chrome Extension + WebSocket Bridge 自建持久化机制
- 这是一个合理的特例——浏览器需要硬件级交互，MCP 协议本身不支持这种持久化

### 虚拟磁盘
- 通过 ToolRegistry 调用 MCP 工具
- disk_executor.py:68-73 直接 `registry.get(full_name)(**kwargs)`

## 设计方案

### 核心思路

**双轨架构**：内部服务器保持同进程模式（性能优先），外部服务器走标准 MCP Client（兼容优先）。两轨在 ToolRegistry 层统一——调用方无感知。

### 架构图

```
                    ┌─────────────────────────┐
                    │       ToolRegistry       │
                    │  (统一接口，调用方无感知)   │
                    └──────────┬──────────────┘
                               │
               ┌───────────────┼───────────────┐
               │               │               │
    ┌──────────▼──────┐  ┌─────▼──────┐  ┌────▼─────────┐
    │  内部服务器轨道  │  │  MCP Client │  │  MCP Client   │
    │  (同进程模式)    │  │  (stdio)    │  │  (HTTP)       │
    │                 │  │             │  │               │
    │  __import__()   │  │  ClientSession│ │ ClientSession │
    │  func(**args)   │  │  + Sampling   │  │ + Sampling   │
    │                 │  │  + stdio 连接 │  │ + HTTP 连接   │
    └─────────────────┘  └──────────────┘  └───────────────┘
         内部服务器        外部MCP服务器      外部MCP服务器
      (photo-server等)    (stdio模式)       (HTTP模式)
```

### 改动1：实现标准 MCP Client

新增 `agent/mcp_client.py`，实现标准 MCP ClientSession：

```python
class MCPClientManager:
    """管理所有 MCP Client 连接（stdio + HTTP）"""

    def __init__(self, sampling_callback):
        self._connections: dict[str, ClientSession] = {}
        self._sampling_callback = sampling_callback

    async def connect_stdio(self, server_name: str, command: str, args: list[str], env: dict = None):
        """连接 stdio 模式的 MCP 服务器"""
        # 启动子进程
        # 创建 read_stream / write_stream
        # 创建 ClientSession（带 sampling_callback）
        # 调用 session.initialize()

    async def connect_http(self, server_name: str, url: str):
        """连接 HTTP 模式的 MCP 服务器"""
        # 创建 StreamableHTTPTransport
        # 创建 ClientSession（带 sampling_callback）
        # 调用 session.initialize()

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> dict:
        """通过 MCP Client 调用工具"""
        session = self._connections[server_name]
        result = await session.call_tool(tool_name, arguments)
        return result

    async def list_tools(self, server_name: str) -> list[dict]:
        """获取工具列表"""
        session = self._connections[server_name]
        result = await session.list_tools()
        return result.tools
```

### 改动2：Sampling callback 实现

Sampling callback 调用 Agent 的 LLM 生成回复：

```python
async def sampling_callback(context, params: CreateMessageRequestParams) -> CreateMessageResult:
    """MCP Sampling callback：调用 Agent LLM 处理 Server 的请求"""

    # 从 params 提取请求内容
    messages = params.messages
    system_prompt = params.systemPrompt
    max_tokens = params.maxTokens

    # 转换为 LLM 调用格式
    llm_messages = []
    if system_prompt:
        llm_messages.append({"role": "system", "content": system_prompt})
    for msg in messages:
        llm_messages.append({"role": msg.role, "content": msg.content.text})

    # 调用 LLM
    config = load_llm_config()
    response = litellm_completion(
        model=config["model"],
        messages=llm_messages,
        max_tokens=max_tokens,
        temperature=params.temperature or 0.2,
        api_key=config.get("api_key"),
        api_base=config.get("api_base"),
    )

    return CreateMessageResult(
        role="assistant",
        content=TextContent(type="text", text=response.choices[0].message.content),
        model=config["model"],
        stopReason="endTurn",
    )
```

### 改动3：ToolRegistry 双轨统一

`agent/tool_registry.py` 新增外部工具注册路径：

```python
class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Callable] = {}       # 内部工具（函数引用）
        self._external_tools: Dict[str, str] = {}    # 外部工具（server_name + tool_name）
        self._mcp_client: MCPClientManager = None    # MCP Client 管理器

    def register_external_server(self, server_name: str, mcp_client: MCPClientManager):
        """注册外部 MCP 服务器（通过 MCP Client）"""
        # 获取工具列表
        tools = await mcp_client.list_tools(server_name)
        for tool in tools:
            full_name = f"{server_name}/{tool.name}"
            self._external_tools[full_name] = (server_name, tool.name)
            self._schemas[full_name] = {
                "name": full_name,
                "description": tool.description,
                "input_schema": tool.inputSchema,
                "visibility": "dynamic",
            }

    def get(self, tool_name: str) -> Optional[Callable]:
        """获取工具函数——内部返回函数引用，外部返回 Client 调用包装器"""
        if tool_name in self._tools:
            return self._tools[tool_name]
        if tool_name in self._external_tools:
            server_name, tool_name_raw = self._external_tools[tool_name]
            # 返回一个包装器，调用 MCP Client
            def wrapper(**kwargs):
                return self._mcp_client.call_tool(server_name, tool_name_raw, kwargs)
            return wrapper
        return None
```

### 改动4：配置文件扩展

`config/mcp-servers.yaml` 新增外部服务器配置格式：

```yaml
# 内部服务器（同进程模式，性能优先）
photo-server:
  module: niu_photo_server
  workdir: ../mcp-servers/photo-server/src
  preload: true

# 外部服务器（stdio 模式，标准 MCP）
external-filesystem:
  mode: stdio
  command: npx
  args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
  sampling: true   # 支持向 Agent 请求 LLM 推理

# 外部服务器（HTTP 模式，标准 MCP）
external-api:
  mode: http
  url: https://mcp-server.example.com/mcp
  sampling: true
```

### 改动5：handler.py 工具调用适配

`agent/handler.py` 中 MCP 工具调用路径需要区分内部/外部：

```python
# 内部工具：同步调用 func(**args)
# 外部工具：异步调用 mcp_client.call_tool()

if "/" in tool_name:
    func = get_registry().get(tool_name)
    if func is not None:
        if tool_name in registry._external_tools:
            # 外部工具：异步调用 MCP Client
            result = await func(**args)
        else:
            # 内部工具：同步调用
            result = func(**args)
```

### 改动6：内部 MCP 服务器如何使用 Sampling

**问题**：内部服务器（如 photo-server）是同进程模式，没有 MCP session，无法通过标准 Sampling 请求 Agent。

**方案**：内部服务器需要 Sampling 时，走 ToolRegistry 的统一接口：

```python
# photo-server 内部
def ingest_document(file_path, category="", mode="copy"):
    if not category:
        preview = read_file_content(file_path)
        prefs = get_preferences()
        available_categories = prefs["categories"]["documents"]

        # 尝试通过 ToolRegistry 的 Sampling 接口请求 Agent 分类
        from agent.tool_registry import get_registry
        registry = get_registry()
        category = registry.ask_agent(
            prompt=f"请根据以下文档内容选择分类。\n\n内容预览：{preview}\n可选分类：{available_categories}",
            system_prompt="你是文档分类助手，只回答分类名称。",
            max_tokens=100,
        )

        if category and category in available_categories:
            # Agent 已回答，继续入库
            ...
        else:
            # Sampling 不可用，回退到 need_category 模式
            return {"status": "need_category", ...}
```

**ToolRegistry.ask_agent()** 实现等同于 Sampling callback：

```python
class ToolRegistry:
    def ask_agent(self, prompt: str, system_prompt: str = "", max_tokens: int = 500) -> str | None:
        """内部工具的 Sampling 等价接口"""
        if self._sampling_fn is None:
            return None
        return self._sampling_fn(prompt=prompt, system_prompt=system_prompt, max_tokens=max_tokens)
```

这不是 hack——这是同进程模式下的 Sampling 等价实现。对外部服务器，Sampling 通过 MCP 协议标准实现。对内部服务器，Sampling 通过函数调用实现。两者效果完全相同。

### 改动7：MCP 服务器改造为标准 call_tool 签名

当前内部服务器的 `call_tool` 签名是：
```python
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
```

MCP 标准的 `call_tool` handler 应该能访问 `RequestContext`：
```python
@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    # 通过 server.request_context 获取 session（仅 stdio 模式可用）
    ctx = server.request_context
    session = ctx.session  # ServerSession 实例
    result = await session.create_message(...)  # Sampling
```

但在同进程模式下，`request_context` 不可用。内部服务器需要通过 ToolRegistry.ask_agent() 替代。

## 实施顺序

1. **P0: 实现 MCP ClientManager** — stdio + HTTP 连接 + Sampling callback
2. **P1: ToolRegistry 双轨注册** — 内部函数 + 外部 Client 包装器
3. **P1: 配置文件扩展** — 支持 stdio/http 模式声明
4. **P2: handler.py 适配** — 内部同步/外部异步区分
5. **P2: ToolRegistry.ask_agent()** — 内部服务器 Sampling 等价接口
6. **P3: photo-server 使用 ask_agent** — 文档分类自动化
7. **P3: 入库功能恢复** — classify_path + 目录入库 + mode 参数

## 测试标准

### 内部 MCP 测试
- 所有 85 个现有工具正常工作
- 性能无回退（同进程调用速度不变）
- photo-server 文档入库 + Sampling 分类正常

### 外部 MCP 测试
- 接入一个标准 stdio MCP 服务器（如 `@modelcontextprotocol/server-filesystem`）
- 接入一个标准 HTTP MCP 服务器
- 工具列表正确显示
- 工具调用正常返回
- Sampling 正常工作（如果外部服务器支持）

### 不改代码只改配置
- 在 `mcp-servers.yaml` 中添加外部服务器配置
- 重启应用后外部工具自动可用
- 无需修改任何 Python 代码