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
| `_calculate_importance` | handler.py | 仅 do_save_memory 调用，删除后成为死代码 |
| `store_memory` | brain_graph.py | 知识图谱记忆写入方法 |
| `recall_memories` | brain_graph.py | 知识图谱记忆读取方法 |
| `MEMORY_TYPE_TO_RELATION` 常量 | brain_graph.py | 仅 store_memory 使用 |
| `DEFAULT_RELATION_TYPE` 常量 | brain_graph.py | 仅 store_memory 使用 |
| `DEFAULT_MIN_WEIGHT` 常量 | brain_graph.py | 仅 recall_memories 使用 |
| `_infer_entity_type` | brain_graph.py | 仅 store_memory 使用 |
| `_extract_entity_label` | brain_graph.py | 仅 store_memory 使用 |
| `_extract_brain_memories_from_structured` | brain_graph.py | 仅 recall_memories 使用 |
| `_extract_brain_memories_from_text` | brain_graph.py | 仅 recall_memories 使用 |
| `brain_api.py` 整个文件 | brain_api.py | 只剩 `/api/brain/status` 一个端点，整体删除。`/api/brain/status` 功能合并到 `brain_region_api.py` |
| `brain_api.py` 路由注册 | `__main__.py` | `from niu_api.brain_api import router` + `app.include_router(brain_router)` 一并删除 |
| runner.py recall_memories 调用 | runner.py `_inject_dynamic_resources` | brain graph memory recall 步骤（含 import 行和 brain_memories_text 赋值） |
| runner.py brain_memories_text 消费 | runner.py `_inject_dynamic_resources` | `_strip_lightrag_error_lines(brain_memories_text)` + `if brain_memories_text: parts.append`，变量未定义必须一并删除 |
| 6 个磁盘工具映射 | config/disk/memory-server.yaml | |
| 6 个 hidden 工具声明 | config/mcp-servers.yaml | |
| `scripts/reindex_vectors.py` | scripts | vectors.db 重索引脚本，已无意义 |
| `scripts/test_memory_server.py` | scripts | 直接 import MemoryStorage，测试 vectors.db 体系 |
| `scripts/test_agent_evolution.py` | scripts | 引用 `_calculate_importance`，测试 vectors.db 体系 |
| `scripts/lightrag_query_test.py` | scripts | 第 57 行和第 136 行引用 `memory-server/remember`，需更新测试数据 |
| `tests/test_brain_graph.py` 4 个测试类 | tests | TestBrainGraphStoreMemory, TestBrainGraphRecallMemories, TestMemoryTypeMapping, TestMetadataEmbedding（保留 TestNormalizeName, TestMakeEntityName, TestBrainGraphEnsureNiu, TestFormatMemoriesForPrompt, TestGetBrainGraphSingleton） |
| config-manager vectors.db 引用 | config-manager `__init__.py` | `(workspace_path / "vectors.db").parent.mkdir(exist_ok=True)` 过时引用（parent 就是 workspace_path 本身，第 717 行已 mkdir，此行冗余） |
| CLAUDE.md 示例代码 | CLAUDE.md | 第 191 行 `registry.get("memory-server/remember")` 引用已不存在的工具，改为 `registry.get("memory-server/user_memory_remember")` |

### 修改范围

#### 1. permanent 数组扩容

文件：`mcp-servers/memory-server/src/niu_memory_server/__init__.py`

常量更新：
```
MAX_PERMANENT_ITEMS: 5 → 10
MAX_MEMORY_ITEMS: 4 → 9
MAX_TASK_ITEMS: 1（不变）
```

描述文本同步更新（搜索 "4条" 和 "1-5" 全部替换）：
- TOOL_SCHEMAS 中 `user_memory_remember` 的描述（"最多4条" → "最多9条"，出现在第 135 行、第 146 行、第 310 行、第 566 行）
- TOOL_SCHEMAS 中 `user_memory_forget` 的描述（"1-5" → "1-10"）
- Tool definition 中的对应描述（`list_tools` 中 user_memory_forget 的 index description）
- `config/disk/memory-server.yaml` 中 `user_memory_remember` 的 short/long 描述

`config/disk/memory-server.yaml` 修改后只保留 3 个工具：
```yaml
  - name: user_memory_remember
    short: "添加便签或长期记忆(task最多1条,memory最多9条)"
    long: "添加便签或长期记忆(task最多1条,memory最多9条)"
    command: "/memory/user_memory_remember"
    handler: "user_memory_remember_handler"
  - name: user_memory_forget
    short: "删除便签或记忆(1-10)"
    long: "按序号(1-10)或关键词删除"
    command: "/memory/user_memory_forget"
    handler: "user_memory_forget_handler"
  - name: user_memory_list
    short: "查看所有记忆"
    long: "查看当前所有用户长期记忆"
    command: "/memory/user_memory_list"
    handler: "user_memory_list_handler"
```

`config/mcp-servers.yaml` 修改后只保留 3 个工具声明：
```yaml
memory-server:
  command: ${PYTHON_PATH}
  args:
  - -m
  - niu_memory_server
  workdir: mcp-servers/memory-server/src
  preload: true
  tools:
    user_memory_remember:
      visibility: hidden
    user_memory_forget:
      visibility: hidden
    user_memory_list:
      visibility: hidden
```

测试文件更新：
- `tests/test_user_memory.py` 第 49 行：`"/5条"` → `"/10条"`，`"memory-server/user_memory_remember"` → `"disk"`，`"memory-server/user_memory_forget"` → `"disk"`
- `tests/test_user_memory.py` 第 190 行：`assert result["max_memory"] == 4` → `assert result["max_memory"] == 9`
- `tests/test_user_memory.py` 的 `test_truncate_over_limit` 测试逻辑需要适配新上限（当前写 8 条，MAX_PERMANENT_ITEMS 改为 10 后 8 条不触发截断，需要改为写 11 条来测试截断）

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

- `_render_permanent_section` 中的尾部提示从"共N/5条"改为"共N/10条"，工具引用从"memory-server/user_memory_remember"改为"disk"，"memory-server/user_memory_forget"改为"disk"
- 删除 `_inject_dynamic_resources` 中 brain graph memory recall 块（含 import 行、`brain_memories_text = ""` 初始化、brain_memories_text 赋值）
- 删除 `_inject_dynamic_resources` 中 brain_memories_text 消费代码（`_strip_lightrag_error_lines(brain_memories_text)` + `if brain_memories_text: parts.append`）

#### 4. __init__.py 清理

文件：`mcp-servers/memory-server/src/niu_memory_server/__init__.py`

- 删除 `from niu_memory_server.storage import MemoryStorage` 和 `storage = MemoryStorage()`
- 删除 6 个工具的 TOOL_SCHEMAS 定义
- 删除 6 个工具的 handler 函数
- 删除 6 个工具的 Tool definition（list_tools）
- 删除 6 个工具的 call_tool 分支
- 删除 `get_tool_schemas()` 中对 6 个工具的返回
- 删除模块级函数别名（remember, recall, update_memory, get_memory_stats, cleanup_memories, link_memories）

#### 5. brain_region_api.py 合并 /api/brain/status

文件：`niu_api/brain_region_api.py`

在文件中添加 `/api/brain/status` 端点：

```python
@router.get("/status")
async def brain_status():
    """Check brain graph status and ensure Niu entity exists."""
    try:
        bg = get_brain_graph()
        bg.ensure_niu_entity()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
```

需要添加 import：`from niu_api.internal.brain_graph import get_brain_graph`（如果已有则不重复）。

同时更新 `brain_region_api.py` 的文件头部注释，删除对已废弃的 `/api/brain/remember`、`/api/brain/recall`、`/api/brain/status` 的旧注释引用。

#### 6. vectors.db 文件清理

文件：`niu_api/__main__.py`

在 startup 阶段（`ensure_niu_entity` 调用之后）添加：

```python
# Clean up deprecated vectors.db
vectors_db_path = Path.home() / ".niu" / "work" / "vectors.db"
if vectors_db_path.exists():
    vectors_db_path.unlink()
    logger.info("Removed deprecated vectors.db: %s", vectors_db_path)
```

### 不动的部分

- `user_memory_remember/forget/list` 3 个工具
- `runner.py` 的 `_load_memory_for_prompt` / `_render_permanent_section`（只微调常量）
- 内容提取 Agent（自动提取知识到知识图谱）
- `brain_graph.py` 的 `ensure_niu_entity`、`format_memories_for_prompt` 和其他非记忆方法
- `format_memories_for_prompt` 当前无运行时调用者（唯一调用点在 runner.py 已删除），保留供测试和未来复用
- `compat.py` 的已有 vectors.db 兼容代码（已是 skip 状态，后续可随整体清理）
- `lightrag_sync.py` 的 `_sync_vectors_db()` 已硬编码 skip，保留不动（低优先级，后续清理）
- `_strip_lightrag_error_lines` 函数变为死代码，保留不动（后续清理）
- `tests/test_proactive_fifo.py` 中 `memory-server/remember` 字符串只是测试数据，无需修改

## 影响分析

所有删除目标均经 GitNexus 确认为 LOW 风险：

| 符号 | 上游调用方 | 风险 |
|------|-----------|------|
| `do_save_memory` | 0（死代码） | LOW |
| `_calculate_importance` | do_save_memory | LOW（两者同时删除） |
| `BrainGraph.store_memory` | do_save_memory + remember_memory | LOW（两者同时删除） |
| `BrainGraph.recall_memories` | runner.py `_inject_dynamic_resources` | LOW（调用块一并删除） |
| `remember_memory` (brain_api.py) | 0 | LOW |
| `recall_memories` (brain_api.py) | 0 | LOW |
| `MemoryStorage` (storage.py) | 仅 __init__.py 导入 | LOW |

handler.py 的 `dispatch()` 使用 `hasattr(self, method_name)` 检查，删除 `do_save_memory` 后不会报错。前端无 `/api/brain/remember` 或 `/api/brain/recall` 调用。

## 删除顺序

按依赖链从上游到下游，避免中间状态编译失败或运行时错误：

1. **handler.py** — 删除 `do_save_memory` + `_calculate_importance`（上游调用者先删）
2. **runner.py** — 删除 `recall_memories` 调用块 + import 行 + brain_memories_text 消费代码
3. **__init__.py** — 删除 `from .storage import MemoryStorage` + `storage = MemoryStorage()` + 6 个工具的 handler/schema/别名（**必须先于 storage.py 文件删除**，否则 MCP Loader 启动崩溃）
4. **storage.py** — 删除文件（__init__.py 已清理完 import）
5. **brain_graph.py** — 删除 `store_memory`/`recall_memories` + 辅助函数/常量
6. **brain_api.py** — 删除整个文件
7. **__main__.py** — 删除 brain_api 路由注册，把 `/api/brain/status` 合并到 `brain_region_api.py`，添加 vectors.db 清理逻辑
8. **配置文件** — disk yaml、mcp-servers.yaml、config-manager、CLAUDE.md 示例代码
9. **脚本和测试** — reindex_vectors.py、test_memory_server.py、test_agent_evolution.py、lightrag_query_test.py 测试数据、test_brain_graph.py 4 个测试类、test_user_memory.py 断言和测试逻辑
10. **提示词** — config/agents/niu.md

## 端到端验证清单

1. Memory Server 启动：`python -m niu_memory_server` 无 ImportError
2. 3 个工具正常工作：ToolRegistry 调用 `user_memory_remember`/`forget`/`list` 返回正确结果
3. 6 个旧工具不存在：`registry.get("memory-server/remember")` 返回 None
4. 主 Agent 提示词渲染：permanent 数组正确渲染，尾部提示为"共N/10条"，工具引用为 disk
5. `_inject_dynamic_resources` 不报 NameError，日志中无 brain graph recall 输出
6. GET `/api/brain/status` 返回 200（已合并到 brain_region_api.py）
7. GET `/api/brain/remember` 和 `/api/brain/recall` 返回 404（路由已移除）
8. `~/.niu/work/vectors.db` 不存在（startup 时已清理）
9. pytest 全部通过（test_brain_graph.py 保留的测试类、test_user_memory.py 更新后的断言）
