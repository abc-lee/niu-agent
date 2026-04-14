# 紧急修复清单

## 问题汇总（按严重性排序）

### 🔴 严重问题 1：LiteLLM API 错误
**症状**：`litellm.BadRequestError: tool call result does not follow tool call (2013)`

**原因**：`agent/generic/agent_loop.py` 中，assistant 消息缺少 content 字段，且某些 tool_call 没有对应的 tool result

**修复文件**：`agent/generic/agent_loop.py`

**修复内容**：
```python
# 第 146-157 行，为 assistant 消息添加 content 字段
assistant_msg = {
    "role": "assistant",
    "content": full_content or "",  # 添加此字段
    "tool_calls": []
}

# 第 190-197 行，确保每个 tool_call 都有 tool result
if outcome.data is not None:
    datastr = ...
    tool_results.append({"tool_use_id": tid, "content": datastr})
else:
    # 即使 data 为 None，也要添加空 result
    tool_results.append({"tool_use_id": tid, "content": ""})

# 第 164-165 行，为 no_tool 添加空 result
if tool_name == "no_tool":
    tool_results.append({"tool_use_id": tid, "content": ""})
    continue
```

---

### 🔴 严重问题 2：Tool function not found 导致功能失效
**症状**：
```
WARNING | agent.tool_registry:register_server - Tool function not found for: memory-server/remember
WARNING | agent.tool_registry:register_server - Tool function not found for: scheduler-server/schedule_task
```

**原因**：MCP 服务器没有按照 ToolRegistry 期望的方式暴露工具函数

**影响**：记忆检索、定时任务等核心功能失效

**修复文件**：为每个 MCP 服务器添加 `get_tool_function()` 方法

**修复示例**（`mcp-servers/memory-server/src/niu_memory_server/__init__.py`）：
```python
def get_tool_function(tool_name: str):
    """返回工具函数，适配 ToolRegistry"""
    from niu_memory_server.handlers import (
        remember_handler, recall_handler, update_memory_handler,
        get_memory_stats_handler, cleanup_memories_handler, link_memories_handler
    )

    handlers = {
        "remember": remember_handler,
        "recall": recall_handler,
        "update_memory": update_memory_handler,
        "get_memory_stats": get_memory_stats_handler,
        "cleanup_memories": cleanup_memories_handler,
        "link_memories": link_memories_handler,
    }
    return handlers.get(tool_name)
```

需要在以下文件中添加类似代码：
- `mcp-servers/memory-server/src/niu_memory_server/__init__.py`
- `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py`
- `mcp-servers/file-parser/src/niu_file_parser/__init__.py`
- `mcp-servers/session-manager/src/niu_session_manager/__init__.py`

---

### 🟡 中等问题 3：Skills 搜索返回 0 结果
**症状**：
```
[Debug] Dynamic injection - Skills: 0 results
[Debug] Dynamic injection - MCP tools: 5 results
```

**原因**：当 context 是空或通用问候时，Skills 分数低于 0.35 阈值

**解决方案 A**（推荐）：降低 Skills 搜索阈值

**修复文件**：`agent/runner.py` 第 406 行

**修复内容**：
```python
# 当前
skills = self.vector_search.search(
    query=context, limit=3, min_score=0.35, filter={"level": "l1", "category": "skill"}
)

# 修改为
skills = self.vector_search.search(
    query=context, limit=3, min_score=0.20, filter={"level": "l1", "category": "skill"}
)
```

**解决方案 B**（更好）：添加通用 Skills

创建 `memory/skills/general.md`，处理通用对话场景。

---

## 修复优先级

1. **立即修复**：LiteLLM 错误（问题 1）- 导致对话失败
2. **尽快修复**：Tool function not found（问题 2）- 核心功能失效
3. **优化改进**：Skills 搜索阈值（问题 3）- 提升召回率

---

## 测试验证

修复后，运行以下测试验证：

```bash
# 测试 1：验证 LiteLLM 错误是否修复
curl -X POST http://localhost:9876/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "帮我查今日热点", "session_id": "test"}'

# 测试 2：验证 ToolRegistry 功能
python -c "
from agent.tool_registry import get_registry
registry = get_registry()
print('Tools with functions:', len(registry._tools))
print('Example:', 'memory-server/recall' in registry._tools)
"

# 测试 3：验证 Skills 注入
python test_skills_injection.py
```

---

## 相关文件清单

| 文件 | 问题 | 修复优先级 |
|------|------|-----------|
| `agent/generic/agent_loop.py` | LiteLLM 错误 | 🔴 立即 |
| `mcp-servers/memory-server/src/niu_memory_server/__init__.py` | Tool function | 🔴 尽快 |
| `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py` | Tool function | 🔴 尽快 |
| `mcp-servers/file-parser/src/niu_file_parser/__init__.py` | Tool function | 🔴 尽快 |
| `mcp-servers/session-manager/src/niu_session_manager/__init__.py` | Tool function | 🔴 尽快 |
| `agent/runner.py` | Skills 阈值 | 🟡 优化 |
