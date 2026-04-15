# MCP工具分层决策

> 日期：2026-04-10
> 决策依据：基于架构优化讨论，确定主Agent最小工具集

---

## 一、主Agent基础工具（固定注入，共22个）

### 1.1 内置工具（11个）

```python
BASE_BUILTIN_TOOLS = [
    # 代码执行
    "code_run",  # bash执行能力

    # 文件操作
    "file_read",     # 读取文件
    "file_patch",    # 修改文件
    "file_write",    # 写入文件

    # 网络操作
    "web_scan",      # 网页扫描
    "web_execute_js", # 执行JS

    # Checkpoint管理
    "update_working_checkpoint",
    "start_long_term_update",

    # 子Agent委托
    "chat-with-file-processor",   # 委托文件处理
    "chat-with-event-manager",    # 委托日程管理
    "chat-with-context-manager",  # 委托记忆管理
]
```

### 1.2 memory-server工具（6个）

**决策依据**：主Agent需要直接操作记忆，响应"记住这个"、"之前我说过什么"等需求。

```python
BASE_MEMORY_TOOLS = [
    "memory-server/remember",        # 保存记忆（自动生成L0/L1/L2）
    "memory-server/recall",          # 检索记忆
    "memory-server/update_memory",   # 更新记忆
    "memory-server/get_memory_stats", # 统计信息
    "memory-server/cleanup_memories", # 清理过期记忆
    "memory-server/link_memories",   # 关联记忆
]
```

**使用场景**：
- 用户："记住，我喜欢用Python"
- 主Agent直接调用 `remember` 保存
- 无需委托给子Agent

### 1.3 vector-store工具（5个）

**决策依据**：主Agent需要直接操作向量库，用于知识检索、文档管理等。

```python
BASE_VECTOR_TOOLS = [
    "vector-store/add_document",      # 添加文档到向量库
    "vector-store/search_documents",  # 语义搜索
    "vector-store/get_document",      # 获取文档
    "vector-store/delete_document",   # 删除文档
    "vector-store/list_documents",    # 列出文档
]
```

**说明**：
- `count_documents` 不包含：主Agent不需要统计数量
- 与子Agent共享：event-manager、context-manager也使用这些工具
- **共享是合理的**：向量库是中心化存储，多个Agent可以访问

---

## 二、子Agent专用工具（主Agent不注入）

### 2.1 file-processor专用（14个）

**来源**：photo-server

```python
PHOTO_SERVER_TOOLS = [
    "photo-server/ingest_document",      # 文档入库
    "photo-server/ingest_documents",     # 批量文档入库
    "photo-server/ingest_photo",         # 照片入库
    "photo-server/ingest_photos",        # 智能照片入库
    "photo-server/store_document_l1",    # 存储L1摘要
    "photo-server/store_documents_l1",   # 批量存储L1
    "photo-server/name_person",          # 人物命名
    "photo-server/merge_persons",        # 合并人物
    "photo-server/search_persons",       # 搜索人物
    "photo-server/get_unnamed_persons",  # 获取未命名人物
    "photo-server/get_person_photos",    # 获取人物照片
    "photo-server/delete_person",        # 删除人物
    "photo-server/cleanup_deleted_photos", # 清理残留
    "photo-server/unload_face_model",    # 卸载模型
]
```

**原因**：照片处理是耗时任务，必须委托给子Agent。

### 2.2 event-manager专用（10个）

**来源**：scheduler-server (4个) + vector-store共享 (6个)

```python
SCHEDULER_TOOLS = [
    "scheduler-server/schedule_task",        # 创建定时任务
    "scheduler-server/list_scheduled_tasks", # 查询任务
    "scheduler-server/cancel_task",          # 取消任务
    "scheduler-server/update_task",          # 更新任务
]

# vector-store工具与主Agent共享
```

**原因**：日程管理需要专门的定时任务工具，主Agent通过 `chat-with-event-manager` 委托。

### 2.3 context-manager专用（2个）

**来源**：session-manager (2个) + vector-store共享 (6个)

```python
SESSION_TOOLS = [
    "session-manager/get_messages",    # 获取消息列表
    "session-manager/delete_messages", # 删除消息
]

# vector-store工具与主Agent共享
```

**原因**：会话管理是子Agent的内部操作，主Agent不需要直接访问。

### 2.4 底层操作（不暴露给任何Agent）

**kg-server (12个)**：
```python
KG_SERVER_TOOLS = [
    "kg-server/create_document",
    "kg-server/create_entity",
    "kg-server/create_concept",
    "kg-server/link_document_entity",
    "kg-server/link_document_concept",
    "kg-server/link_entities",
    "kg-server/get_document",
    "kg-server/list_documents",
    "kg-server/search_documents",
    "kg-server/get_related_entities",
    "kg-server/get_related_concepts",
    "kg-server/query_graph",
]

> **Note (2026-04-15):** `create_document`, `create_entity`, `link_document_entity` are called via two paths:
> 1. **Programmatically** by `sync_to_kg()` / `sync_photo_to_kg()` during ingestion (same-process call)
> 2. **By dream-evolver sub-agent** during sleep (LLM-driven, via mcpServers config)
>
> They remain classified as 底层操作 for the main Agent — not exposed to main Agent LLM tool calls. The dream-evolver is the only sub-agent authorized to use kg-server write tools.
```

**file-parser (2个)**：
```python
FILE_PARSER_TOOLS = [
    "file-parser/parse_file",
    "file-parser/list_supported_formats",
]
```

**原因**：这些是底层存储操作，应该封装在上层工具中，不直接暴露。

---

## 三、已删除工具（20个）

**config-manager (20个)**：

```python
DELETED_CONFIG_TOOLS = [
    "config-manager/get_llm_config",
    "config-manager/set_llm_config",
    "config-manager/list_llm_presets",
    "config-manager/test_llm_connection",
    "config-manager/get_storage_config",
    "config-manager/set_storage_config",
    "config-manager/get_identity",
    "config-manager/update_identity",
    "config-manager/get_workspace",
    "config-manager/set_workspace",
    "config-manager/get_user_info",
    "config-manager/set_user_info",
    "config-manager/add_user_preference",
    "config-manager/is_first_run",
    "config-manager/complete_setup",
    "config-manager/get_full_memory",
    "config-manager/mkdir",
    "config-manager/copy_to_path",
    "config-manager/move_to_path",
    "config-manager/list_files_in_workspace",
]
```

**删除原因**：
1. **不必要**：主Agent已有 `bash + file_read/file_write` 能力
2. **替代方案**：配置文件结构写入系统管理手册（Skills）
3. **灵活性**：bash命令比专用工具更灵活

**示例**：
```bash
# 用户：修改API Key
# 旧方式：调用 config-manager/set_llm_config
# 新方式：
1. file_read: 读取 config/user-config.json
2. file_patch: 修改 apikey 字段
3. file_write: 写回文件
```

---

## 四、动态注入工具（暂时为空）

**说明**：
- 初始版本不实现动态注入
- 所有主Agent需要的工具已包含在基础工具中
- 后续根据实际使用情况，评估是否需要动态注入

**可能的动态工具**：
```python
# 待评估
DYNAMIC_TOOLS_CANDIDATES = [
    # 如果主Agent需要操作知识图谱
    # "kg-server/query_graph",

    # 如果主Agent需要高级文件操作
    # "photo-server/ingest_photo",  # 但应该委托给file-processor
]
```

---

## 五、工具统计

| 分类 | 数量 | 说明 |
|------|------|------|
| 主Agent基础工具 | 22 | 固定注入 |
| 子Agent专用 | 30 | 不注入主Agent |
| 底层操作 | 14 | 不暴露 |
| 已删除 | 20 | 移除 |
| **总计** | **86** | 原始66 + 新增14(vector-store/memory-server与子Agent共享) |

**减少比例**：
- 原始：77个工具（11内置 + 66 MCP）
- 优化后：22个工具（11内置 + 11 MCP）
- **减少：71%**

---

## 六、特殊情况处理

### 6.1 vector-store工具共享

**问题**：主Agent、event-manager、context-manager都需要访问向量库

**解决方案**：
- 主Agent：固定注入（5个工具）
- event-manager：通过配置注入（6个工具，包含count_documents）
- context-manager：通过配置注入（6个工具，包含count_documents）

**工具过滤**：
```python
# agent/subagent.py 改进
def get_subagent_mcp_tools_schema(agent_name: str) -> List[Dict]:
    config = get_subagent_config(agent_name)
    mcp_servers = config.get("mcpServers", [])

    registry = get_registry()
    all_tools = registry.get_schemas()

    schema = []
    for tool in all_tools:
        tool_name = tool.get("name", "")
        if "/" in tool_name:
            server = tool_name.split("/")[0]
            if server in mcp_servers:
                # 子Agent特殊处理
                if agent_name == "event-manager" and tool_name == "vector-store/count_documents":
                    schema.append(...)  # 包含count_documents
                elif tool_name != "vector-store/count_documents":
                    schema.append(...)  # 排除count_documents

    return schema
```

### 6.2 config-manager服务器保留

**决策**：config-manager服务器保留，但工具不注入主Agent

**原因**：
- file-processor可能需要 `mkdir`, `copy_to_path` 等工具
- 作为可选MCP服务器，按需加载

**配置示例**：
```yaml
# config/mcp-servers.yaml
config-manager:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_config_manager"
  workdir: ../mcp-servers/config-manager/src
  preload: false  # 不预加载
```

---

## 七、实施要点

### 7.1 索引主Agent基础工具

**文件**：`scripts/index_mcp_tools.py`

**工具列表**：
```python
MAIN_AGENT_MCP_TOOLS = [
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

### 7.2 索引子Agent专用工具

**文件**：`scripts/index_subagent_tools.py`（可选）

**说明**：
- 子Agent工具不一定需要索引到向量库
- 子Agent配置文件已明确指定工具列表
- 索引的目的：如果主Agent需要动态委托，可以检索到

---

## 八、验证清单

- [ ] 主Agent工具数量 = 22个
- [ ] 子Agent工具配置正确
- [ ] 底层工具不暴露
- [ ] config-manager工具已移除
- [ ] 向量库索引完成
- [ ] 功能测试通过

---

## 九、后续优化方向

### 9.1 短期（1周内）

1. 监控主Agent工具使用频率
2. 识别从未使用的工具
3. 调整基础工具列表

### 9.2 中期（1个月内）

1. 评估动态注入必要性
2. 优化工具描述质量
3. 扩展查询模式库

### 9.3 长期（3个月内）

1. 考虑CLI+Skills方案
2. 实现工具自动发现
3. 架构重构
