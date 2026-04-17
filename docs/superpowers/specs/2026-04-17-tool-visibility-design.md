# 工具可见性与动态注入过滤设计

## 背景

主 Agent 的工具注入存在三个问题：

1. **co-activation 泄漏**：调用一个 kg-server 工具，同 server 全部 21 个工具都被激活到 65 分
2. **向量库冗余**：52 个 MCP 工具描述全部存入向量库，包括主 Agent 不该看到的写入工具
3. **子 Agent 污染**：子 Agent 调用工具时写入主 Agent 的 tool_lifecycle 分数，导致持久化文件积累无用条目

## 设计目标

- 每个工具有明确的可见性标识，控制主 Agent 是否可见及如何可见
- 子 Agent 工具调用不影响主 Agent 的 tool_lifecycle 分数
- 向量库只存主 Agent 需要动态发现的工具
- 子 Agent 保持纯静态工具注入，不受 visibility 配置影响

## Visibility 三值模型

| visibility | 含义 | 主 Agent | 向量库 |
|-----------|------|---------|--------|
| `static` | 固定注入，每轮都可用 | 始终可见 | 不存入 |
| `dynamic` | 通过向量检索按需注入 | 检索命中时可见 | 存入 |
| `hidden` | 主 Agent 不可见 | 不可见 | 不存入 |

**默认值**：未在配置中声明的工具，默认 `visibility: dynamic`

**子 Agent 不受影响**：子 Agent 通过 `mcpServers` 列表纯静态获取工具，与 visibility 无关。

## 配置层

### mcp-servers.yaml 增加 tools 字段

只配非默认值（`static` 和 `hidden`），未配置的工具默认 `dynamic`。

```yaml
memory-server:
  preload: true
  tools:
    remember: {visibility: static}
    recall: {visibility: static}
    update_memory: {visibility: static}
    get_memory_stats: {visibility: static}
    cleanup_memories: {visibility: static}
    link_memories: {visibility: static}

vector-store:
  preload: true
  tools:
    add_document: {visibility: static}
    search_documents: {visibility: static}
    get_document: {visibility: static}
    delete_document: {visibility: static}
    list_documents: {visibility: static}
    # count_documents 默认 dynamic

kg-server:
  preload: true
  tools:
    explore_node: {visibility: static}
    get_related_entities: {visibility: static}
    search_documents: {visibility: dynamic}
    get_document: {visibility: dynamic}
    list_documents: {visibility: dynamic}
    query_graph: {visibility: dynamic}
    hub_entities: {visibility: dynamic}
    find_path: {visibility: dynamic}
    get_related_concepts: {visibility: dynamic}
    graph_stats: {visibility: dynamic}
    create_document: {visibility: hidden}
    create_entity: {visibility: hidden}
    create_concept: {visibility: hidden}
    link_document_entity: {visibility: hidden}
    link_document_concept: {visibility: hidden}
    link_entities: {visibility: hidden}
    surprising_connections: {visibility: hidden}
    graph_changelog: {visibility: hidden}
    graph_snapshot: {visibility: hidden}
    list_entities: {visibility: hidden}
    list_concepts: {visibility: hidden}

browser-server:
  preload: false
  tools:
    browser_navigate: {visibility: static}
    # browser_interact, browser_new_tab 默认 dynamic

# photo-server, config-manager, file-parser, scheduler-server, session-manager
# 的工具默认全部 dynamic（主 Agent 按需发现）
```

### mcp_loader.py 解析

`load_mcp_tools()` 解析 `tools:` 配置，将 visibility 写入每个 normalized_schema 的 `visibility` 字段，传给 ToolRegistry。

## ToolRegistry 存储 + 查询

### agent/tool_registry.py

1. `register_server()` 时，从 mcp_loader 传入的 visibility 配置写入每个 schema 的 `visibility` 字段
2. 新增方法：

```python
def get_visibility(self, tool_name: str) -> str:
    """返回工具的 visibility: static / dynamic / hidden（默认 dynamic）"""

def get_static_tools(self) -> list[str]:
    """返回所有 visibility=static 的工具名列表（替代硬编码 BASE_MCP_TOOLS）"""

def get_dynamic_tools(self) -> list[str]:
    """返回所有 visibility=dynamic 的工具名列表（向量库初始化时用）"""
```

### agent/runner.py

1. **删除硬编码 `BASE_MCP_TOOLS`**，改为从 `ToolRegistry.get_static_tools()` 获取
2. **`_inject_dynamic_resources()` 中 mcp_tool_scores 过滤**：构建 mcp_tool_scores 时，跳过 `visibility: hidden` 的工具，不调用 `update_from_search()`
3. 递归检索结果同理：`search_multi()` 返回后，`hidden` 工具在 mcp_tool_scores 构建时被过滤

## tool_lifecycle 过滤

### agent/tool_lifecycle.py

1. **`_coactivate_same_server_tools()`**：激活同 server 工具时，从 ToolRegistry 查询 visibility，跳过 `hidden` 的工具
2. **`get_active_tools()`**：返回时过滤掉 `visibility: hidden` 的工具（防御性，防止持久化文件残留）
3. **`update_from_search()`**：不改动，分数更新本身没问题，过滤在调用方做
4. **`hit_tool()`**：不改动，分数记录本身没问题，过滤在 `get_active_tools()` 做

关键点：`hidden` 工具可能因为历史残留进入 `active_tools` 字典，但 `get_active_tools()` 返回时会过滤掉，不会注入到主 Agent 的 schema。

## 子 Agent 隔离

### agent/subagent.py

创建子 Agent 的 handler 时，标记 `_is_subagent = True`：

```python
handler = NiuHandler(mcp_client=mcp_client)
handler._is_subagent = True  # 标记为子 Agent，dispatch() 中跳过 hit_tool()
```

**重要约定**：以后新增子 Agent 时，创建 handler 必须标记 `_is_subagent = True`，否则子 Agent 的工具调用会污染主 Agent 的 tool_lifecycle 分数。此约定必须记录在代码注释中。

### agent/handler.py

`dispatch()` 中判断 `_is_subagent`，子 Agent 跳过 `hit_tool()`：

```python
if not getattr(self, '_is_subagent', False):
    runner.tool_lifecycle.hit_tool(tool_name)
```

## 向量库初始化同步

### scripts/init_vector_db.py

`register_mcp_tools()` 改为：从 `mcp-servers.yaml` 读取每个工具的 visibility，只有 `visibility: dynamic` 的工具才写入向量库。`static` 和 `hidden` 的都不存。

### 递归检索联动

`search_multi()` 的递归检索可能命中 `hidden` 工具。过滤在调用方 `_inject_dynamic_resources()` 的 mcp_tool_scores 构建时统一处理，`search_multi()` 内部不需要改。

如果某个 `query_pattern` 的 `refined_query` 只能命中 `hidden` 工具（过滤后 mcp_tool 桶为空），说明这条 `query_pattern` 数据有问题，应删除该记录。

## 数据清理

1. **`~/.niu/tool_scores.json`**：删除所有 `visibility: hidden` 工具的分数条目
2. **向量库**：删除重建（最干净的方式）
3. **`query_pattern` 记录**：检查是否有 `refined_query` 只能命中 `hidden` 工具的记录，有的话删除

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `config/mcp-servers.yaml` | 加 `tools:` 下的 `visibility` 配置 |
| `agent/mcp_loader.py` | 解析 `tools:` 配置，传给 ToolRegistry |
| `agent/tool_registry.py` | schema 加 `visibility` 字段；新增 `get_visibility()`、`get_static_tools()`、`get_dynamic_tools()` |
| `agent/runner.py` | 删除硬编码 `BASE_MCP_TOOLS`，改从 ToolRegistry 获取；`mcp_tool_scores` 过滤 `hidden` |
| `agent/tool_lifecycle.py` | `_coactivate_same_server_tools()` 跳过 `hidden`；`get_active_tools()` 过滤 `hidden` |
| `agent/handler.py` | `dispatch()` 中判断 `_is_subagent`，子 Agent 跳过 `hit_tool()` |
| `agent/subagent.py` | 创建 handler 时标记 `handler._is_subagent = True`，加注释说明约定 |
| `scripts/init_vector_db.py` | `register_mcp_tools()` 只写入 `visibility: dynamic` 的工具 |
| `~/.niu/tool_scores.json` | 删除 `hidden` 工具的分数条目 |
| 向量库 | 删除重建 |