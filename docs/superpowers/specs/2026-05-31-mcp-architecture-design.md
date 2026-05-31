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
- **改造约束**：
  - browser-server 仍作为内部服务器（同进程模式），其持久化机制不受 MCP 改造影响
  - browser-server 的工具仍通过 ToolRegistry 注册，visibility 和磁盘机制照常工作
  - browser-server 使用 `playwright.async_api`，现有代码通过 `call_async()` 桥接同步调用，这种模式在双轨架构下仍然有效——内部工具的同步调用路径不变

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
        """通过 MCP Client 调用工具（异步）"""
        session = self._connections[server_name]
        result = await session.call_tool(tool_name, arguments)
        return result

    def call_tool_sync(self, server_name: str, tool_name: str, arguments: dict) -> dict:
        """同步调用 MCP Client（通过 _run_coroutine 桥接，供 handler.py 使用）"""
        from agent.mcp_sync_bridge import _run_coroutine
        return _run_coroutine(self.call_tool(server_name, tool_name, arguments))

    async def list_tools(self, server_name: str) -> list[dict]:
        """获取工具列表"""
        session = self._connections[server_name]
        result = await session.list_tools()
        return result.tools
```

**初始化时机**：`runner.py` 在 `__init__` 中创建 MCPClientManager 实例，在 `load_mcp_tools()` 之后、`agent_loop` 启动之前，调用 `connect_external_servers()` 连接所有外部服务器：

```python
# runner.py 初始化流程
class GenericAgentRunner:
    def __init__(self, ...):
        ...
        # 1. 加载内部 MCP 服务器（现有流程）
        load_mcp_tools()

        # 2. 注入 ask_agent callback
        registry = get_registry()
        registry.set_ask_agent(self._make_ask_agent_callback())

        # 3. 连接外部 MCP 服务器（新增）
        self._mcp_client = MCPClientManager(sampling_callback=self._sampling_callback)
        self._connect_external_servers()  # 读取 mcp-servers.yaml 中 mode=stdio/http 的配置
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

    # 调用 LLM（使用 agent.llmcore 的配置加载）
    try:
        from agent.llmcore import load_llm_config, create_client
        config = load_llm_config()
        client = create_client(config)
        response = client.chat.completions.create(
            model=config["model"],
            messages=llm_messages,
            max_tokens=max_tokens,
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
        # Sampling 失败时返回错误提示，不中断工具调用
        return CreateMessageResult(
            role="assistant",
            content=TextContent(type="text", text=f"[Sampling 失败: {e}]"),
            model="error",
            stopReason="endTurn",
        )
```

**注意**：`stopSequences` 和 `metadata` 字段暂不处理——当前内部服务器（photo-server）不需要这些高级功能。后续如有需求再扩展。

### 改动3：ToolRegistry 双轨统一

`agent/tool_registry.py` 新增外部工具注册路径：

```python
class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Callable] = {}       # 内部工具（函数引用）
        self._external_tools: Dict[str, str] = {}    # 外部工具（server_name + tool_name）
        self._mcp_client: MCPClientManager = None    # MCP Client 管理器

    def register_external_server(self, server_name: str, mcp_client: MCPClientManager,
                                  visibility_map: dict[str, str] = None):
        """注册外部 MCP 服务器（通过 MCP Client）

        visibility_map: 从配置文件读取的 {tool_name: visibility} 映射
        未列出的工具默认 visibility: hidden
        """
        tools = await mcp_client.list_tools(server_name)
        for tool in tools:
            full_name = f"{server_name}/{tool.name}"
            self._external_tools[full_name] = (server_name, tool.name)
            # visibility 从配置文件读取，与内部工具一致
            tool_vis = (visibility_map or {}).get(tool.name, "hidden")
            self._schemas[full_name] = {
                "name": full_name,
                "description": tool.description,
                "input_schema": tool.inputSchema,
                "visibility": tool_vis,
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

    # 以下方法行为不变，对内部/外部工具一视同仁：
    # - get_schemas(): 返回所有工具（含 hidden），visibility 字段保留
    # - get_static_tools(): 只返回 visibility=static 的工具
    # - get_dynamic_tools(): 只返回 visibility=dynamic 的工具
    # - get_visibility(): 返回工具的 visibility 值
```

**关键约束**：
- `get_schemas()` 仍返回所有工具（含 hidden），供子 Agent 使用
- `get_static_tools()` / `get_dynamic_tools()` 仍按 visibility 过滤，主 Agent 只看到非 hidden 工具
- 子 Agent 的 `get_subagent_mcp_tools_schema()` 仍按 mcpServers 白名单过滤，忽略 visibility
- 磁盘导航（disk_parser）仍通过 ToolRegistry 获取工具，disk hidden 是独立维度

### 改动4：配置文件扩展

`config/mcp-servers.yaml` 新增外部服务器配置格式。**现有内部服务器配置格式不变**，新增 `mode` 字段区分内部/外部：

```yaml
# 内部服务器（同进程模式，性能优先）——配置格式不变
photo-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - niu_photo_server
  workdir: ../mcp-servers/photo-server/src
  preload: true
  tools:
    ingest:
      visibility: hidden
    ingest_document:
      visibility: hidden
    # ... 其他工具

# 外部服务器（stdio 模式，标准 MCP）——新增格式
external-filesystem:
  mode: stdio                    # mode 字段区分：无 mode = 内部，stdio/http = 外部
  command: npx
  args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
  sampling: true                 # 支持向 Agent 请求 LLM 推理
  tools:
    read_file:
      visibility: static         # 外部工具也支持 visibility 配置
    write_file:
      visibility: hidden         # 写操作隐藏，通过磁盘调用

# 外部服务器（HTTP 模式，标准 MCP）——新增格式
external-api:
  mode: http
  url: https://mcp-server.example.com/mcp
  sampling: true
  tools:
    search:
      visibility: static
    # 未列出的工具默认 visibility: hidden
```

**关键规则**：
- **向后兼容**：无 `mode` 字段的服务器仍按内部服务器处理，`mcp_loader.py` 的 `REQUIRED_SERVERS` 硬编码列表不变
- **区分方式**：`mode: stdio` 或 `mode: http` 表示外部服务器，无 `mode` 表示内部服务器
- 外部服务器的 `tools` 配置与内部服务器格式一致
- 未在 `tools` 中列出的外部工具，默认 `visibility: hidden`
- 外部工具的 visibility 仍由配置文件控制，不由外部服务器自身决定
- 子 Agent 白名单机制不变：在 `config/agents/*.md` 的 `mcpServers` 中添加外部服务器名即可

### 改动5：handler.py 工具调用适配

`agent/handler.py` 中 MCP 工具调用路径需要区分内部/外部。

**关键约束**：`handler.py` 的 `dispatch()` 是同步生成器（使用 `yield`），不能使用 `await`。现有代码通过 `_run_coroutine()` 在同步上下文中执行协程（第 1153 行）。

**方案**：外部工具的 `registry.get()` 返回同步包装器，内部使用 `_run_coroutine()` 桥接异步调用。handler.py 无需区分内部/外部——调用方式统一为 `func(**args)`。

```python
# ToolRegistry.get() 返回的包装器（同步外观，内部异步桥接）
def wrapper(**kwargs):
    return self._mcp_client.call_tool_sync(server_name, tool_name_raw, kwargs)
```

**MCPClientManager 新增同步方法**：
```python
class MCPClientManager:
    def call_tool_sync(self, server_name: str, tool_name: str, arguments: dict) -> dict:
        """同步调用 MCP Client（通过 _run_coroutine 桥接）"""
        return _run_coroutine(self.call_tool(server_name, tool_name, arguments))
```

**handler.py 无需改动**——所有工具统一 `func(**args)` 调用，内部工具直接执行，外部工具通过同步包装器执行。

**disk_executor.py 同理**——现有 `registry.get(full_name)(**kwargs)` 调用方式不变，外部工具的同步包装器自动适配。

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
    def __init__(self):
        ...
        self._ask_agent = None  # callable(prompt: str, system_prompt: str = "", max_tokens: int = 500) -> str

    def set_ask_agent(self, fn):
        """注入 Agent LLM 回调函数，供内部 MCP Server 调用。由 runner.py 在初始化时注入。"""
        self._ask_agent = fn

    def ask_agent(self, prompt: str, system_prompt: str = "", max_tokens: int = 500) -> str | None:
        """请求 Agent LLM 生成回答。返回文本或 None（如果不可用）"""
        if self._ask_agent is None:
            return None
        return self._ask_agent(prompt=prompt, system_prompt=system_prompt, max_tokens=max_tokens)
```

**为什么放在 ToolRegistry**：内部服务器（如 photo-server）已经通过 `get_registry()` 调用 lightrag-server 的工具。ask_agent 走同样的路径，保持调用方式一致。ToolRegistry 是内部服务器的统一入口点。

**注入时机**：`runner.py` 在初始化 ToolRegistry 后、启动 agent_loop 前注入：
```python
registry = get_registry()
registry.set_ask_agent(self._make_ask_agent_callback())
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

### 可见性系统约束（必须保留）

改造后的 MCP 架构必须完整保留现有的三层可见性机制，所有规则对内部和外部工具一视同仁。

#### 第一层：ToolRegistry visibility（工具注册可见性）

- 来源：`config/mcp-servers.yaml` 中每个工具的 `visibility` 字段
- 取值：`static`（始终可见）、`dynamic`（向量检索可见）、`hidden`（不可见）
- 作用范围：主 Agent 的 `get_schemas()` 返回结果
- **规则**：`hidden` 工具不出现在主 Agent 的工具列表中，但 ToolRegistry 仍注册该工具（可通过 `get(name)` 获取）
- **改造约束**：外部 MCP 服务器的工具也必须在注册时支持 visibility 配置。外部工具的 visibility 仍由 `mcp-servers.yaml` 配置，不是由外部服务器自身决定

#### 第二层：DiskConfig hidden（虚拟磁盘软隐藏）

- 来源：`config/disk/*.yaml` 中每个工具条目的 `hidden: true`
- 作用范围：虚拟磁盘（disk_parser）的导航界面
- **规则**：`hidden: true` 的工具在磁盘目录浏览时不显示，但 LLM 仍可通过 `disk()` 命令直接调用（如果知道路径）
- **与第一层的关系**：独立的隐藏维度。一个工具可以 `visibility: hidden`（第一层）但 `disk hidden: false`（第二层不隐藏），反之亦然
- **改造约束**：外部 MCP 服务器添加到配置后，其工具应能自动出现在磁盘 YAML 中（或通过配置手动添加）。磁盘导航必须能发现和调用外部工具

#### 第三层：SubAgent mcpServers 白名单（子 Agent 可见性）

- 来源：`config/agents/*.md` 中子 Agent 定义的 `mcpServers` 列表
- 作用范围：子 Agent 的工具列表
- **规则**：子 Agent 只能看到 `mcpServers` 白名单中列出的服务器的工具。子 Agent **忽略**第一层 visibility——即使工具是 `hidden`，只要其服务器在白名单中，子 Agent 就能看到和调用
- **实现**：`agent/subagent.py` 的 `get_subagent_mcp_tools_schema()` 从 ToolRegistry 获取全部工具，按 mcpServers 过滤，不过滤 visibility
- **裸名调用**：子 Agent 使用裸工具名调用（如 `remember`），`handler.py` 通过 `Auto-resolve bare tool names` 逻辑（第 1016-1025 行）解析为完整名（如 `memory-server/remember`）
- **改造约束**：
  1. 子 Agent 的白名单机制必须同时适用于内部和外部服务器
  2. 外部 MCP 服务器名也可出现在 `mcpServers` 白名单中
  3. `handler.py` 的裸名解析逻辑需要同时覆盖内部和外部工具——外部工具注册时需确保 `_server_tools` 映射正确

#### 三层交互矩阵

| 工具状态 | 主 Agent 可见 | 磁盘导航可见 | 子 Agent 可见 |
|----------|-------------|-------------|-------------|
| visibility=static, disk 不隐藏 | ✅ | ✅ | ✅（白名单内） |
| visibility=hidden, disk 不隐藏 | ❌ | ✅ | ✅（白名单内） |
| visibility=static, disk hidden | ✅ | ❌ | ✅（白名单内） |
| visibility=hidden, disk hidden | ❌ | ❌ | ✅（白名单内） |
| 不在子 Agent 白名单 | 视第一层 | 视第二层 | ❌ |

#### 外部服务器注册流程

1. 用户在 `mcp-servers.yaml` 中添加外部服务器配置（mode: stdio/http + visibility）
2. 应用启动时，MCPClientManager 连接外部服务器，`list_tools()` 获取工具列表
3. ToolRegistry 注册外部工具，visibility 从配置文件读取（与内部工具一致）
4. 外部工具注册时，`_server_tools` 映射同步更新，确保裸名解析和子 Agent 白名单正常工作
5. 磁盘 YAML 需手动配置：如果外部工具需要磁盘导航，在 `config/disk/*.yaml` 中添加条目（与内部工具一致）
6. `disk_executor.py` 无需改动——外部工具的 `registry.get()` 返回同步包装器，`func(**kwargs)` 调用方式不变
7. 子 Agent 白名单可选：如果子 Agent 需要使用外部工具，在 `config/agents/*.md` 中添加服务器名

## 实施顺序

1. **P0: 实现 MCP ClientManager** — stdio + HTTP 连接 + Sampling callback
2. **P1: ToolRegistry 双轨注册** — 内部函数 + 外部 Client 包装器，保留完整 visibility 机制
3. **P1: 配置文件扩展** — 支持 stdio/http 模式声明 + visibility 配置
4. **P2: handler.py 适配** — 内部同步/外部异步区分
5. **P2: ToolRegistry.ask_agent()** — 内部服务器 Sampling 等价接口
6. **P3: photo-server 使用 ask_agent** — 文档分类自动化
7. **P3: 入库功能恢复** — classify_path + 目录入库 + mode 参数

## 测试标准

### 内部 MCP 测试
- 所有 85 个现有工具正常工作
- 性能无回退（同进程调用速度不变）
- photo-server 文档入库 + Sampling 分类正常
- 三层可见性机制完整保留：
  - ToolRegistry visibility（static/dynamic/hidden）正常过滤
  - 磁盘 disk hidden 机制正常工作
  - 子 Agent mcpServers 白名单忽略 visibility

### 外部 MCP 测试
- 接入一个标准 stdio MCP 服务器（如 `@modelcontextprotocol/server-filesystem`）
- 接入一个标准 HTTP MCP 服务器
- 工具列表正确显示
- 工具调用正常返回
- Sampling 正常工作（如果外部服务器支持）
- 外部工具的 visibility 配置生效（hidden 工具主 Agent 不可见）
- 外部工具可通过磁盘导航调用（如果 disk YAML 配置了）
- 外部工具可被子 Agent 使用（如果 mcpServers 白名单包含）

### 不改代码只改配置
- 在 `mcp-servers.yaml` 中添加外部服务器配置
- 重启应用后外部工具自动可用
- 无需修改任何 Python 代码
- 外部工具的 visibility 通过配置文件控制