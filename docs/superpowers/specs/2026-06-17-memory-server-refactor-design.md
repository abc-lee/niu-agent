# Memory Server 重构设计

## 背景

Memory Server 当前包含两套完全独立的记忆体系：

1. **体系 A（vectors.db）**：remember/recall 等 6 个工具，写入独立 SQLite 向量库（L0/L1/L2 三层），同时双写到 brain_graph（知识图谱）。这是从旧向量库迁移时遗留的，现在 agent 自动检索路径已切换到 LightRAG，vectors.db 成为孤岛——写进去但日常检索看不到。
2. **体系 B（memory.json permanent 数组）**：user_memory_remember/forget/list 3 个工具，写入 `~/.niu/memory.json` 的 permanent 数组，全量驻留 system prompt。

## 问题

1. **vectors.db 是历史遗留**：从未写入过实际数据（0 行），代码中已标注 deprecated
2. **知识图谱记忆写入重复**：brain_graph.store_memory 和 memory-server/remember 干的活一样，日常知识提取由内容提取 Agent 自动完成，不需要单独的"记忆写入"入口
3. **知识图谱记忆读取不可靠**：brain_graph 写入时 entity_type 是 Skill/Concept/Event（不是"记忆"），读取时只看关系是否涉及 Niu——无法区分"记忆"和"普通知识"
4. **permanent 数组描述丢失**：Task 类型的设计意图（长程工作便签，防压缩/重启后遗忘）在代码中找不到完整描述
5. **主 Agent 提示词矛盾**：大量使用 MCP 工具描述，但主 Agent 用的是磁盘工具（disk），没有 MCP 映射

## 设计

### 删除范围

| 组件 | 位置 | 说明 |
|------|------|------|
| `storage.py` | memory-server | 整个文件（MemoryStorage 类 + vectors.db 体系） |
| 6 个 MCP 工具 | `__init__.py` | remember, recall, update_memory, get_memory_stats, cleanup_memories, link_memories 及对应 handler |
| `storage = MemoryStorage()` | `__init__.py` | 模块级实例化 |
| `do_save_memory` | handler.py | 死代码（LLM 看不到、无显式调用），双写 vectors.db + brain_graph |
| `store_memory` | brain_graph.py | 知识图谱记忆写入方法 |
| `recall_memories` | brain_graph.py | 知识图谱记忆读取方法 |
| `MEMORY_TYPE_TO_RELATION` 常量 | brain_graph.py | 仅 store_memory 使用 |
| `DEFAULT_RELATION_TYPE` 常量 | brain_graph.py | 仅 store_memory 使用 |
| `DEFAULT_MIN_WEIGHT` 常量 | brain_graph.py | 仅 recall_memories 使用 |
| `_infer_entity_type` | brain_graph.py | 仅 store_memory 使用 |
| `_extract_entity_label` | brain_graph.py | 仅 store_memory 使用 |
| `_extract_brain_memories_from_structured` | brain_graph.py | 仅 recall_memories 使用 |
| `_extract_brain_memories_from_text` | brain_graph.py | 仅 recall_memories 使用 |
| `remember_memory` | brain_api.py | HTTP API 端点函数 + Request Model |
| `recall_memories` | brain_api.py | HTTP API 端点函数 + Request Model |
| runner.py recall_memories 调用 | runner.py 第 1489-1498 行 | `_inject_dynamic_resources` 中的 brain graph memory recall 步骤 |
| 6 个磁盘工具映射 | config/disk/memory-server.yaml | |
| 6 个 hidden 工具声明 | config/mcp-servers.yaml | |
| `scripts/reindex_vectors.py` | scripts | vectors.db 重索引脚本，已无意义 |
| `scripts/test_memory_server.py` | scripts | 直接 import MemoryStorage，测试 vectors.db 体系 |
| `tests/test_brain_graph.py` 4 个测试类 | tests | TestBrainGraphStoreMemory, TestBrainGraphRecallMemories, TestMemoryTypeMapping, TestMetadataEmbedding |
| config-manager vectors.db 引用 | config-manager `__init__.py` 第 721 行 | `(workspace_path / "vectors.db").parent.mkdir(exist_ok=True)` 过时引用 |

### 修改范围

#### 1. permanent 数组扩容

文件：`mcp-servers/memory-server/src/niu_memory_server/__init__.py`

```
MAX_PERMANENT_ITEMS: 5 → 10
MAX_MEMORY_ITEMS: 4 → 9
```

同步更新：
- TOOL_SCHEMAS 中 `user_memory_remember` 的描述（"最多4条" → "最多9条"）
- TOOL_SCHEMAS 中 `user_memory_forget` 的描述（"1-5" → "1-10"）
- Tool definition 中的对应描述
- `config/disk/memory-server.yaml` 中 `user_memory_remember` 的 short/long 描述
- `tests/test_user_memory.py` 第 49 行（"/5条" → "/10条"）

#### 2. 主 Agent 提示词修复

文件：`config/agents/niu.md`

将当前笼统的"使用 memory-server 工具管理用户长期记忆和工作便签"改为：

```
# 用户长期记忆

使用磁盘工具 `disk("/memory/user_memory_remember ...")` 管理用户长期记忆和工作便签。

**工作便签（task）**：最多 1 条，新任务自动覆盖旧任务。
当执行长程复杂任务时，先记录当前进度、关键参数和下一步到工作便签，
防止上下文压缩或意外重启后遗忘当前工作状态。

**长期记忆（memory）**：最多 9 条，每条不超过 200 token。
只有用户主动要求"记住"某事时才写入（如"以后不能这样"、"你需要记住这个"）。
日常偏好、事实、技能由内容提取 Agent 自动提取到知识图谱，不需要手动存储。

相关工具：
- `disk("/memory/user_memory_remember <content> --type task|memory")` — 添加
- `disk("/memory/user_memory_forget <content>")` — 删除
- `disk("/memory/user_memory_list")` — 查看所有

修改 identity/workspace/user 字段时，用 `read` + `edit` 工具读写 `~/.niu/memory.json`。
```

#### 3. runner.py 渲染和注入清理

文件：`agent/runner.py`

- `_render_permanent_section` 中的尾部提示从"共N/5条"改为"共N/10条"
- 删除 `_inject_dynamic_resources` 中第 1489-1498 行的 `bg.recall_memories()` 调用块

#### 4. __init__.py 清理

文件：`mcp-servers/memory-server/src/niu_memory_server/__init__.py`

- 删除 `from niu_memory_server.storage import MemoryStorage` 和 `storage = MemoryStorage()`
- 删除 6 个工具的 TOOL_SCHEMAS 定义
- 删除 6 个工具的 handler 函数
- 删除 6 个工具的 Tool definition（list_tools）
- 删除 6 个工具的 call_tool 分支
- 删除 `get_tool_schemas()` 中对 6 个工具的返回
- 删除模块级函数别名（remember, recall, update_memory, get_memory_stats, cleanup_memories, link_memories）

#### 5. vectors.db 文件清理

在 `niu_api/__main__.py` 的 startup 阶段添加：检测并删除 `~/.niu/work/vectors.db` 文件（如果存在）。

### 不动的部分

- `user_memory_remember/forget/list` 3 个工具
- `runner.py` 的 `_load_memory_for_prompt` / `_render_permanent_section`（只微调常量）
- 内容提取 Agent（自动提取知识到知识图谱）
- `brain_graph.py` 的 `ensure_niu_entity`、`format_memories_for_prompt` 和其他非记忆方法
- `brain_api.py` 的 `/api/brain/status` 端点（保留）
- `compat.py` 的已有 vectors.db 兼容代码（已是 skip 状态，后续可随整体清理）
- `lightrag_sync.py` 的 `_sync_vectors_db()` 已硬编码 skip，保留不动（低优先级，后续清理）

## 影响分析

所有删除目标均经 GitNexus 确认为 LOW 风险：

| 符号 | 上游调用方 | 风险 |
|------|-----------|------|
| `do_save_memory` | 0（死代码） | LOW |
| `BrainGraph.store_memory` | do_save_memory + remember_memory | LOW（两者同时删除） |
| `BrainGraph.recall_memories` | runner.py:1494 | LOW（调用块一并删除） |
| `remember_memory` (brain_api.py) | 0 | LOW |
| `recall_memories` (brain_api.py) | 0 | LOW |
| `MemoryStorage` (storage.py) | 仅 __init__.py 导入 | LOW |

handler.py 的 `dispatch()` 使用 `hasattr(self, method_name)` 检查，删除 `do_save_memory` 后不会报错。前端无 `/api/brain/remember` 或 `/api/brain/recall` 调用。
