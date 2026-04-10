# Step 1: 架构调整 - 动态工具注入（已完成）

> 完成日期：2026-04-10
> 状态：✅ 已完成
> 验证：测试通过

---

## 实施内容

### 1.1 定义基础MCP工具列表

**修改文件**：`agent/runner.py`

**添加常量**：
```python
BASE_MCP_TOOLS = [
    # memory-server (6个)
    "memory-server/remember",
    "memory-server/recall",
    "memory-server/update_memory",
    "memory-server/get_memory_stats",
    "memory-server/cleanup_memories",
    "memory-server/link_memories",

    # vector-store (5个)
    "vector-store/add_document",
    "vector-store/search_documents",
    "vector-store/get_document",
    "vector-store/delete_document",
    "vector-store/list_documents",
]
```

**决策依据**：参见 `docs/tool-layer-decision.md`

---

### 1.2 实现工具Schema检索

**添加方法**：`NiuRunner._get_tool_schema_by_name(tool_name: str)`

**功能**：从已注册的MCP工具列表中查找指定工具的Schema

**代码**：
```python
def _get_tool_schema_by_name(self, tool_name: str) -> Optional[Dict]:
    """
    根据工具名获取工具Schema

    Args:
        tool_name: 工具名，格式为 "server-name/tool-name"

    Returns:
        工具Schema字典，找不到返回None
    """
    for tool in self._mcp_tools_schema:
        if tool.get("function", {}).get("name") == tool_name:
            return tool
    return None
```

---

### 1.3 修改工具注入逻辑

**修改位置**：`agent/runner.py` 的 `chat()` 方法

**旧逻辑**：
```python
# 组装 tools_schema = 内置工具 + MCP 工具
tools_schema = self.base_tools_schema.copy()
if self._mcp_tools_schema:
    tools_schema.extend(self._mcp_tools_schema)  # 全部66个
```

**新逻辑**：
```python
# 组装 tools_schema = 内置工具 + 基础MCP工具
tools_schema = self.base_tools_schema.copy()

# 固定注入基础MCP工具（memory-server + vector-store，共11个）
for tool_name in BASE_MCP_TOOLS:
    schema = self._get_tool_schema_by_name(tool_name)
    if schema:
        tools_schema.append(schema)

# TODO: 动态注入其他工具（Step 2实现）
# dynamic_tools = self._get_dynamic_tools(user_input)
# tools_schema.extend(dynamic_tools)
```

---

### 1.4 验证config-manager工具未注入

**结果**：
- config-manager 服务器保留在 `config/agents/niu.md` 的 `mcpServers` 列表中
- 但其工具不在 `BASE_MCP_TOOLS` 列表中
- 因此不会被注入主Agent

**符合架构决策**：
- 保留服务器供子Agent使用（如 file-processor 可能需要 `mkdir`, `copy_to_path`）
- 主Agent不直接调用config-manager工具
- 使用 `bash + file_read/file_write` 替代

---

## 测试验证

### 测试脚本

**文件**：`scripts/test_dynamic_tool_injection.py`

### 测试结果

```
=== 测试基础MCP工具列表 ===
预期工具数量: 11
实际工具数量: 11
✓ 工具列表匹配

=== 测试内置工具Schema ===
内置工具数量: 11
工具列表: ['code_run', 'file_read', 'file_patch', 'file_write', 'web_scan', 'web_execute_js', 'update_working_checkpoint', 'start_long_term_update', 'chat-with-file-processor', 'chat-with-event-manager', 'chat-with-context-manager']
✓ 内置工具匹配

=== 测试工具注入逻辑 ===
所有MCP工具数量: 66
内置工具: 11
基础MCP工具: 11
总工具数: 22
预期总工具数: 22 (11 内置 + 11 基础MCP)
✓ 工具注入正确

=== 所有测试完成 ===
```

---

## 效果对比

| 指标 | 优化前 | 优化后 | 减少 |
|------|--------|--------|------|
| 主Agent工具总数 | 77 | 22 | 71% |
| 内置工具 | 11 | 11 | 0 |
| MCP工具 | 66 | 11 | 83% |

---

## 后续步骤

**Step 2：工具生命周期管理**（待实施）
- 实现 `ToolLifecycleManager` 类
- 工具命中状态管理（100分 → -10衰减/轮 → <50分移除）
- 集成到 `NiuRunner.chat()` 方法

**Step 3：触发机制改进**（待实施）
- 扩展触发源：user_input, llm_response, tool_result, subagent_return
- 实现上下文监控
- 在 `agent_runner_loop` 中检查是否需要新工具

**Step 4：测试验证**（待实施）
- 向量检索精度测试
- 工具生命周期测试
- 动态工具注入测试
- 多轮对话测试
- 端到端测试

---

## 相关文档

- `docs/implementation-plan-tool-injection-optimization.md` — 完整实施计划
- `docs/tool-layer-decision.md` — 工具分层决策
- `scripts/test_dynamic_tool_injection.py` — 测试脚本
