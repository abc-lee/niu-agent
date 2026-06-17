# Memory Server 重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 vectors.db 体系（6 个旧工具 + storage.py）和知识图谱记忆读写路径（handler.do_save_memory + brain_graph.store_memory/recall_memories + brain_api.py），保留 permanent 数组 3 个工具并扩容至 10 条（1 task + 9 memory），修复主 Agent 提示词矛盾。

**Architecture:** 按依赖链从上游到下游删除，避免中间状态编译失败。先删调用者（handler.py、runner.py），再删被调用者（__init__.py、storage.py、brain_graph.py），最后删文件（brain_api.py 的 import 先于文件删除），清理配置和测试。

**Tech Stack:** Python 3.11+, FastAPI, MCP SDK, pytest

---

## Task 1: 删除 handler.py 死代码（do_save_memory + _calculate_importance）

**Files:**
- Modify: `agent/handler.py:901-971`

- [ ] **Step 1: 删除 do_save_memory 和 _calculate_importance 方法**

在 `agent/handler.py` 中，删除第 899-971 行（从 `# ========== 记忆管理 ==========` 注释到 `_calculate_importance` 方法结束）：

```python
# 删除以下整段代码（第 899-971 行）：
    # ========== 记忆管理 ==========

    def do_save_memory(self, args: dict, response) -> StepOutcome:
        """..."""
        ...

    def _calculate_importance(self, memory_type: str) -> float:
        """..."""
        ...
```

保留第 973 行 `# ========== MCP 工具（动态） ==========` 及其后续内容。

- [ ] **Step 2: 验证语法**

Run: `python -c "import ast; ast.parse(open('agent/handler.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add agent/handler.py
git commit -m "refactor(memory): remove dead code do_save_memory and _calculate_importance from handler.py"
```

---

## Task 2: 删除 runner.py 中 brain graph memory recall 调用块和消费代码

**Files:**
- Modify: `agent/runner.py:1489-1498` (recall 调用块)
- Modify: `agent/runner.py:1561-1564` (brain_memories_text 消费)

- [ ] **Step 1: 删除 brain graph memory recall 调用块**

在 `agent/runner.py` 的 `_inject_dynamic_resources` 方法中，删除第 1489-1498 行：

```python
# 删除以下整段代码：
        # 4. Brain graph memory recall
        brain_memories_text = ""
        try:
            from niu_api.internal.brain_graph import get_brain_graph, format_memories_for_prompt
            bg = get_brain_graph()
            brain_memories = bg.recall_memories(context, top_k=10, min_weight=0.3, keywords=keywords)
            if brain_memories:
                brain_memories_text = format_memories_for_prompt(brain_memories)
        except Exception as e:
            logger.debug(f"Brain graph recall failed (non-blocking): {e}")
```

- [ ] **Step 2: 删除 brain_memories_text 消费代码**

在同一方法中，删除第 1561-1564 行：

```python
# 删除以下整段代码：
        # Brain memories
        brain_memories_text = _strip_lightrag_error_lines(brain_memories_text)
        if brain_memories_text:
            parts.append(brain_memories_text)
```

注意：`brain_memories_text` 变量已随 Step 1 一起删除，此处消费代码也必须一并删除，否则 NameError。

- [ ] **Step 3: 验证语法**

Run: `python -c "import ast; ast.parse(open('agent/runner.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 4: 提交**

```bash
git add agent/runner.py
git commit -m "refactor(memory): remove brain graph memory recall from runner.py dynamic injection"
```

---

## Task 3: 清理 memory-server __init__.py（删除 6 个旧工具 + storage import + 扩容常量）

**Files:**
- Modify: `mcp-servers/memory-server/src/niu_memory_server/__init__.py`

这是最大的单文件改动。按以下顺序操作：

- [ ] **Step 1: 删除 storage import 和实例化**

删除第 14 行和第 20 行：
```python
from .storage import MemoryStorage
storage = MemoryStorage()
```

- [ ] **Step 2: 删除 6 个旧 TOOL_SCHEMAS**

从 TOOL_SCHEMAS 字典中删除以下 6 个 key 及其值（第 26-132 行）：
- `remember`
- `recall`
- `update_memory`
- `get_memory_stats`
- `cleanup_memories`
- `link_memories`

保留 `user_memory_remember`、`user_memory_forget`、`user_memory_list` 三个。

- [ ] **Step 3: 更新常量**

将第 453-455 行的常量改为：
```python
MAX_PERMANENT_ITEMS = 10
MAX_TASK_ITEMS = 1
MAX_MEMORY_ITEMS = 9  # MAX_TASK_ITEMS + MAX_MEMORY_ITEMS = MAX_PERMANENT_ITEMS
```

- [ ] **Step 4: 更新 TOOL_SCHEMAS 中的描述文本**

在 `user_memory_remember` 的 TOOL_SCHEMAS（约第 133-152 行）中：
- 第 135 行 description: `"最多4条"` → `"最多9条"`，完整替换为：
  ```
  "description": "添加用户长期记忆或工作便签。type='task'为当前工作便签(最多1条,新任务自动覆盖旧任务,用于保存复杂任务的进度/关键参数/下一步); type='memory'为用户长期记忆(最多9条,仅在用户明确要求记住时添加)。记忆永久驻留系统提示词,异常退出后下次继续。",
  ```
- 第 146 行 type description: `"4条"` → `"9条"`，替换为：
  ```
  "description": "task=当前工作便签(1条,自动覆盖), memory=用户长期记忆(9条,需手动删)",
  ```

在 `user_memory_forget` 的 TOOL_SCHEMAS（约第 153-169 行）中：
- 第 161 行 index description: `"1-5"` → `"1-10"`，替换为：
  ```
  "description": "记忆序号（1-10），优先于 keyword",
  ```

- [ ] **Step 5: 删除 6 个旧 handler 函数**

删除第 344-442 行的 6 个函数：
- `remember_handler`
- `recall_handler`
- `update_memory_handler`
- `get_memory_stats_handler`
- `cleanup_memories_handler`
- `link_memories_handler`

- [ ] **Step 6: 删除 get_tool_definitions 中 6 个旧 Tool 定义**

在 `get_tool_definitions()` 函数中，删除 6 个旧 Tool 对象（第 194-299 行）：
- `Tool(name="remember", ...)`
- `Tool(name="recall", ...)`
- `Tool(name="update_memory", ...)`
- `Tool(name="get_memory_stats", ...)`
- `Tool(name="cleanup_memories", ...)`
- `Tool(name="link_memories", ...)`

保留 `user_memory_remember`、`user_memory_forget`、`user_memory_list` 三个 Tool 定义。

同时更新保留的 Tool 定义中的描述：
- `user_memory_remember` Tool 的 description: `"最多4条"` → `"最多9条"`，完整替换为：
  ```
  description="添加用户长期记忆或工作便签。type='task'为当前工作便签(最多1条,新任务自动覆盖旧任务); type='memory'为用户长期记忆(最多9条)。记忆将永久驻留在系统提示词中，异常退出后下次继续。",
  ```
- `user_memory_remember` Tool 的 type description: `"4条"` → `"9条"`，替换为：
  ```
  "description": "task=当前工作便签(1条), memory=用户长期记忆(9条)",
  ```
- `user_memory_forget` Tool 的 index description: `"1-5"` → `"1-10"`，替换为：
  ```
  "description": "记忆序号（1-10），优先于 keyword",
  ```

- [ ] **Step 7: 删除 call_tool 中 6 个旧 elif 分支**

在 `call_tool()` 函数中，删除 6 个旧 elif 分支（第 768-800 行）：
- `if name == "remember":`
- `elif name == "recall":`
- `elif name == "update_memory":`
- `elif name == "get_memory_stats":`
- `elif name == "cleanup_memories":`
- `elif name == "link_memories":`

保留 `user_memory_remember`、`user_memory_forget`、`user_memory_list` 三个分支。删除后，将第一个保留分支 `elif name == "user_memory_remember":` 的 `elif` 改为 `if`（因为它变成了 call_tool 中的第一个分支）。

- [ ] **Step 8: 删除 6 个旧模块级函数别名**

删除第 734-750 行的 6 个旧别名函数：
```python
def remember(content: str, memory_type: str, **kwargs):
    return remember_handler(content=content, memory_type=memory_type, **kwargs)

def recall(query: str, **kwargs):
    return recall_handler(query=query, **kwargs)

def update_memory(memory_id: str, content: str, **kwargs):
    return update_memory_handler(memory_id=memory_id, content=content, **kwargs)

def get_memory_stats(**kwargs):
    return get_memory_stats_handler(**kwargs)

def cleanup_memories(**kwargs):
    return cleanup_memories_handler(**kwargs)

def link_memories(memory_id_1: str, memory_id_2: str, relation: str, **kwargs):
    return link_memories_handler(memory_id_1=memory_id_1, memory_id_2=memory_id_2, relation=relation, **kwargs)
```

保留 `user_memory_remember`、`user_memory_forget`、`user_memory_list` 三个别名。

- [ ] **Step 9: 更新 user_memory_remember_handler 中的描述**

在第 562-567 行的 `user_memory_remember_handler` docstring 中：
```python
def user_memory_remember_handler(content: str, type: str = "memory") -> dict:
    """添加用户长期记忆到 memory.json permanent 数组

    type="task": 当前工作便签（最多1条，新任务覆盖旧的）
    type="memory": 用户长期记忆（最多9条）
    """
```

- [ ] **Step 10: 验证语法**

Run: `cd mcp-servers/memory-server/src && python -c "import niu_memory_server; print('OK')"`
Expected: OK（不应有 ImportError，因为 storage.py 还存在但不再被 import）

- [ ] **Step 11: 提交**

```bash
git add mcp-servers/memory-server/src/niu_memory_server/__init__.py
git commit -m "refactor(memory): remove 6 legacy vector tools from memory-server, expand permanent to 10 slots"
```

---

## Task 4: 删除 storage.py 文件

**Files:**
- Delete: `mcp-servers/memory-server/src/niu_memory_server/storage.py`

- [ ] **Step 1: 确认 __init__.py 已不再 import storage**

Run: `grep -n "storage" mcp-servers/memory-server/src/niu_memory_server/__init__.py`
Expected: 无输出（Task 3 已删除 import 和实例化）

- [ ] **Step 2: 删除文件**

```bash
rm mcp-servers/memory-server/src/niu_memory_server/storage.py
```

- [ ] **Step 3: 提交**

```bash
git add -A mcp-servers/memory-server/src/niu_memory_server/
git commit -m "refactor(memory): delete storage.py (vectors.db system removed)"
```

---

## Task 5: 删除 brain_graph.py 中记忆相关方法和常量

**Files:**
- Modify: `niu_api/internal/brain_graph.py`

- [ ] **Step 1: 删除记忆相关常量**

删除第 28-40 行的 3 个常量：
```python
MEMORY_TYPE_TO_RELATION: Dict[str, str] = {
    "environment": "located_at",
    "preferences": "prefers",
    "skills": "skilled_in",
    "experiences": "remembers",
    "facts": "remembers",
}

# Default weight and relation type when memory_type is not specified
DEFAULT_WEIGHT = 0.7
DEFAULT_RELATION_TYPE = "remembers"

DEFAULT_MIN_WEIGHT = 0.3
```

保留 `ENTITY_TYPES`、`MAX_NAME_LENGTH`。

- [ ] **Step 2: 删除 store_memory 方法**

删除第 141-234 行（从 `# ============== Memory Storage ==============` 到 store_memory 方法结束）。

- [ ] **Step 3: 删除 recall_memories 方法**

删除第 236-287 行（从 `# ============== Memory Recall ==============` 到 recall_memories 方法结束）。

- [ ] **Step 4: 删除 4 个辅助方法**

删除第 289-372 行：
- `_infer_entity_type` (291-302)
- `_extract_entity_label` (304-314)
- `_extract_brain_memories_from_structured` (316-344)
- `_extract_brain_memories_from_text` (346-371)

保留 `format_memories_for_prompt` 函数（第 375 行起）和 `get_brain_graph` 单例。

- [ ] **Step 5: 更新文件头部 docstring**

将文件头部 docstring 中的记忆相关描述删除，改为：

```python
"""
Brain Graph — Knowledge graph operations on LightRAG.

Core concepts:
- Niu — the "self" entity, all memory relations start from it
- Entity names use natural language (e.g., "Python", "任飞"), not colon-prefix format
- format_memories_for_prompt: format brain graph memories for system prompt injection
"""
```

- [ ] **Step 6: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/internal/brain_graph.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 7: 提交**

```bash
git add niu_api/internal/brain_graph.py
git commit -m "refactor(memory): remove store_memory/recall_memories and helpers from brain_graph.py"
```

---

## Task 6: 删除 __main__.py 中 brain_api 路由 + 删除 brain_api.py + 合并 /api/brain/status + 添加 vectors.db 清理

**Files:**
- Modify: `niu_api/__main__.py`
- Modify: `niu_api/brain_region_api.py`
- Delete: `niu_api/brain_api.py`

**重要**：必须先删 `__main__.py` 中的 import 和路由注册，再删 `brain_api.py` 文件。否则中间状态启动会 `ModuleNotFoundError`。

- [ ] **Step 1: 删除 __main__.py 中 brain_api import 和路由注册**

在 `niu_api/__main__.py` 中：
- 删除第 28 行：`from niu_api.brain_api import router as brain_router`
- 删除第 387 行：`app.include_router(brain_router)  # Brain Graph API`

- [ ] **Step 2: 删除 brain_api.py 文件**

```bash
rm niu_api/brain_api.py
```

- [ ] **Step 3: 在 brain_region_api.py 添加 /api/brain/status 端点**

在 `niu_api/brain_region_api.py` 中：

1. 添加 import（如果不存在）：
```python
from niu_api.internal.brain_graph import get_brain_graph
```

2. 在文件末尾（最后一个端点之后）添加：
```python
@router.get("/status")
async def brain_status():
    """Check brain graph status and ensure Niu entity exists."""
    try:
        bg = get_brain_graph()
        bg.ensure_niu_entity()
        return {"status": "ok", "message": "Brain graph is active. Niu entity ensured."}
    except Exception as e:
        return {"status": "error", "message": str(e)}
```

注意：`message` 字段保留以保持与旧 `brain_api.py` 端点的响应格式一致。

3. 更新文件头部 docstring，删除对已废弃端点的引用：
```python
"""
Brain Region API endpoints — Region management and consolidation.

Provides REST API for querying brain region states, triggering
community detection, and inspecting region membership.

Routes:
    GET  /api/brain/regions             — list all regions with activation states
    POST /api/brain/regions/consolidate — trigger community detection
    GET  /api/brain/regions/{name}/members — get region members
    GET  /api/brain/status              — check brain graph status

Integration: Mount this router in niu_api/__main__.py:
    from niu_api.brain_region_api import router as brain_region_router
    app.include_router(brain_region_router)
"""
```

- [ ] **Step 4: 在 __main__.py startup 添加 vectors.db 清理**

在 `niu_api/__main__.py` 的 startup 函数中，在 `ensure_niu_entity` 调用之后（约第 205 行后）添加：

```python
    # Clean up deprecated vectors.db
    try:
        vectors_db_path = Path.home() / ".niu" / "work" / "vectors.db"
        if vectors_db_path.exists():
            vectors_db_path.unlink()
            logger.info("Removed deprecated vectors.db: %s", vectors_db_path)
    except Exception as e:
        logger.debug(f"vectors.db cleanup failed (non-blocking): {e}")
```

- [ ] **Step 5: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/__main__.py').read()); print('OK')"` 和 `python -c "import ast; ast.parse(open('niu_api/brain_region_api.py').read()); print('OK')"`
Expected: 两个都 OK

- [ ] **Step 6: 提交**

```bash
git add -A niu_api/ && git add niu_api/__main__.py niu_api/brain_region_api.py
git commit -m "refactor(memory): delete brain_api.py, merge /api/brain/status into brain_region_api, add vectors.db cleanup"
```

---

## Task 7: 更新配置文件（disk yaml + mcp-servers.yaml + config-manager + CLAUDE.md + tool_registry.py）

**Files:**
- Modify: `config/disk/memory-server.yaml`
- Modify: `config/mcp-servers.yaml`
- Modify: `mcp-servers/config-manager/src/niu_config_manager/__init__.py`
- Modify: `CLAUDE.md`
- Modify: `agent/tool_registry.py`
- Modify: `docs/SYSTEM_MANUAL.md`

- [ ] **Step 1: 更新 config/disk/memory-server.yaml**

删除 6 个旧工具映射（第 5-99 行），只保留 3 个并更新描述。完整替换 tools 部分为：

```yaml
tools:
  - name: user_memory_remember
    category: write
    short: "添加便签或长期记忆(task最多1条,memory最多9条)"
    long: "添加便签或长期记忆(task最多1条,memory最多9条)"
    parameters:
      - name: content
        position: 1
        type: string
        required: true
      - name: type
        type: string
        enum: [task, memory]
        default: memory

  - name: user_memory_forget
    category: admin
    short: "删除便签或记忆(1-10)"
    long: "按序号(1-10)或关键词删除"
    parameters:
      - name: index
        type: integer
      - name: keyword
        type: string

  - name: user_memory_list
    category: read
    short: "查看所有记忆"
    long: "查看当前所有用户长期记忆"
    parameters: []
```

- [ ] **Step 2: 更新 config/mcp-servers.yaml**

在 memory-server 段，删除 6 个旧 hidden 工具声明，只保留 3 个：

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

- [ ] **Step 3: 删除 config-manager 中 vectors.db 冗余引用**

在 `mcp-servers/config-manager/src/niu_config_manager/__init__.py` 第 721 行，删除：
```python
    (workspace_path / "vectors.db").parent.mkdir(exist_ok=True)
```
（parent 就是 workspace_path 本身，第 717 行已 mkdir，此行冗余）

- [ ] **Step 4: 更新 CLAUDE.md**

1. 第 189 行，将：
   ```python
   tool_fn = registry.get("memory-server/remember")
   ```
   改为：
   ```python
   tool_fn = registry.get("memory-server/user_memory_remember")
   ```

2. 第 192 行的调用示例改为：
   ```python
   result = tool_fn(content="用户喜欢 Python", type="memory")
   ```

3. 第 417 行，将 `python scripts/test_memory_server.py` 改为 `python -m niu_memory_server`（脚本已删除，改用模块启动验证）

- [ ] **Step 5: 更新 agent/tool_registry.py docstring**

在 `agent/tool_registry.py` 第 19 行，将 docstring 中的：
```python
tool_fn = registry.get("memory-server/remember")
```
改为：
```python
tool_fn = registry.get("memory-server/user_memory_remember")
```

同时将后续调用示例（约第 20-22 行）从：
```python
result = tool_fn(content="用户喜欢 Python", metadata={"type": "preference"})
```
改为：
```python
result = tool_fn(content="用户喜欢 Python", type="memory")
```

- [ ] **Step 6: 更新 docs/SYSTEM_MANUAL.md**

在 `docs/SYSTEM_MANUAL.md` 第 103 行，将：
```
运行时代码（`lightrag_insert_entity`、`store_memory`）不创建 niu→实体锚边
```
改为：
```
运行时代码（`lightrag_insert_entity`）不创建 niu→实体锚边
```
（`store_memory` 已删除）

- [ ] **Step 7: 提交**

```bash
git add config/disk/memory-server.yaml config/mcp-servers.yaml mcp-servers/config-manager/src/niu_config_manager/__init__.py CLAUDE.md agent/tool_registry.py docs/SYSTEM_MANUAL.md
git commit -m "refactor(memory): update config/docs — remove 6 legacy tools, expand to 10 slots, fix stale references"
```

---

## Task 8: 更新 runner.py 渲染提示（permanent 数组尾部提示）

**Files:**
- Modify: `agent/runner.py:150`

- [ ] **Step 1: 更新 _render_permanent_section 尾部提示**

在 `agent/runner.py` 第 150 行，将：
```python
    lines.append(f"（共{len(normalized)}/5条，使用 memory-server/user_memory_remember 添加，memory-server/user_memory_forget 删除）")
```
改为：
```python
    lines.append(f"（共{len(normalized)}/10条，使用 disk 添加/删除）")
```

- [ ] **Step 2: 验证语法**

Run: `python -c "import ast; ast.parse(open('agent/runner.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add agent/runner.py
git commit -m "refactor(memory): update permanent section footer — 5→10 slots, disk tool reference"
```

---

## Task 9: 删除脚本和更新测试

**Files:**
- Delete: `scripts/reindex_vectors.py`
- Delete: `scripts/test_memory_server.py`
- Delete: `scripts/test_agent_evolution.py`
- Modify: `scripts/lightrag_query_test.py:57,136`
- Modify: `scripts/README.md` (清理已删除脚本和 vectors.db 引用)
- Modify: `tests/test_brain_graph.py` (删除 5 个测试类)
- Modify: `tests/test_user_memory.py` (更新断言和测试逻辑)

- [ ] **Step 1: 删除 3 个脚本**

```bash
rm scripts/reindex_vectors.py scripts/test_memory_server.py scripts/test_agent_evolution.py
```

- [ ] **Step 2: 更新 lightrag_query_test.py 测试数据**

在第 57 行和第 136 行，将 `"file_path": "mcp_tool://memory-server/remember"` 改为 `"file_path": "mcp_tool://memory-server/user_memory_remember"`。

- [ ] **Step 3: 清理 scripts/README.md**

删除以下引用：
- 第 30 行和第 168 行：`python scripts/test_memory_server.py` 引用段落
- 第 50 行和第 250 行：`python scripts/test_agent_evolution.py` / `test_memory_server.py, test_agent_evolution.py` 引用
- 第 174 行：`rm ~/.niu/vectors.db` 命令
- 第 197-200 行：`sqlite3 ~/.niu/vectors.db` 查询段落
- 其他引用 `reindex_vectors.py`、`test_memory_server.py`、`test_agent_evolution.py`、`vectors.db` 的段落

- [ ] **Step 4: 删除 test_brain_graph.py 中 5 个测试类**

删除以下测试类（保留其余）：
- `TestBrainGraphStoreMemory` (第 106-189 行)
- `TestBrainGraphRecallMemories` (第 192-269 行)
- `TestMemoryTypeMapping` (第 289-315 行)
- `TestMetadataEmbedding` (第 381-451 行) — **依赖已删除的 `store_memory` 方法，必须一并删除**

**注意**：`_make_mock_brain_graph` 辅助函数（第 90-103 行）必须保留！`TestBrainGraphEnsureNiu`（保留的测试类）第 277 行调用了它。

保留：
- `TestNormalizeName`
- `TestMakeEntityName`
- `TestBrainGraphEnsureNiu`
- `TestFormatMemoriesForPrompt`
- `TestGetBrainGraphSingleton`

- [ ] **Step 5: 更新 test_user_memory.py**

1. 第 49 行：`"/5条"` → `"/10条"`，`"memory-server/user_memory_remember"` → `"disk"`，`"memory-server/user_memory_forget"` → `"disk"`：
   ```python
   lines.append(f"（共{len(normalized)}/10条，使用 disk 添加/删除）")
   ```

2. 第 190 行：`assert result["max_memory"] == 4` → `assert result["max_memory"] == 9`

3. `test_truncate_over_limit` 测试（第 198-215 行）：当前写 8 条，MAX_PERMANENT_ITEMS 改为 10 后 8 条不触发截断。需要改为写 11 条来测试截断：
   ```python
   def test_truncate_over_limit():
       """Truncate permanent array > 10 on load"""
       with tempfile.TemporaryDirectory() as tmp:
           memory_path = Path(tmp) / ".niu" / "memory.json"
           memory_path.parent.mkdir(parents=True, exist_ok=True)
           data = {"permanent": [_mem(f"记忆{i}") for i in range(11)]}
           memory_path.write_text(json.dumps(data), encoding="utf-8")

           _setup_module(memory_path)

           result = mod._read_memory_json()
           assert len(result["permanent"]) == mod.MAX_PERMANENT_ITEMS
           # Kept first 10
           assert result["permanent"][0] == _mem("记忆0")
           assert result["permanent"][9] == _mem("记忆9")

       mod._reset_memory_json_path()
       print("PASS: test_truncate_over_limit")
   ```

4. `test_truncated_rejects_remember` 测试（第 374-395 行）：当前写 8 条测试超限（max 5），扩容到 10 后 8 条不触发截断。改为写 12 条：
   ```python
   async def test_truncated_rejects_remember():
       """When over limit, remember is rejected (no silent data loss)"""
       with tempfile.TemporaryDirectory() as tmp:
           memory_path = Path(tmp) / ".niu" / "memory.json"
           memory_path.parent.mkdir(parents=True, exist_ok=True)
           # Write 12 items (over max 10)
           data = {"permanent": [_mem(f"记忆{i}") for i in range(12)]}
           memory_path.write_text(json.dumps(data), encoding="utf-8")

           _setup_module(memory_path)

           # Remember should be rejected
           result = await mod.user_memory_remember_handler(content="新记忆")
           assert result["status"] == "error"
           assert "超过" in result["message"] or "限制" in result["message"]

           # File should NOT be modified (no silent data loss)
           saved = json.loads(memory_path.read_text(encoding="utf-8"))
           assert len(saved["permanent"]) == 12

       mod._reset_memory_json_path()
       print("PASS: test_truncated_rejects_remember")
   ```

5. `test_truncated_allows_forget` 测试（第 398-413 行）：当前写 8 条，改为写 12 条：
   ```python
   async def test_truncated_allows_forget():
       """When over limit, forget is still allowed (to fix the over-limit)"""
       with tempfile.TemporaryDirectory() as tmp:
           memory_path = Path(tmp) / ".niu" / "memory.json"
           memory_path.parent.mkdir(parents=True, exist_ok=True)
           data = {"permanent": [_mem(f"记忆{i}") for i in range(12)]}
           memory_path.write_text(json.dumps(data), encoding="utf-8")

           _setup_module(memory_path)

           # Forget should work even when truncated
           result = await mod.user_memory_forget_handler(index=1)
           assert result["status"] == "success"

       mod._reset_memory_json_path()
       print("PASS: test_truncated_allows_forget")
   ```

6. 更新 test_user_memory.py 末尾的 `asyncio.run()` 调用列表（第 550-551 行），确保 `test_truncated_rejects_remember` 和 `test_truncated_allows_forget` 仍在调用列表中。

- [ ] **Step 6: 提交**

```bash
git add -A scripts/ tests/
git commit -m "refactor(memory): delete vectors.db scripts, update tests for 10-slot permanent array"
```

---

## Task 10: 更新主 Agent 提示词

**Files:**
- Modify: `config/agents/niu.md:199-203`

- [ ] **Step 1: 替换用户长期记忆段落**

将 `config/agents/niu.md` 第 199-203 行：
```
# 用户长期记忆

使用 memory-server 工具管理用户长期记忆和工作便签。记忆驻留在系统提示词中，始终生效。

修改 identity/workspace/user 字段时，用 `read` + `edit` 工具读写 `~/.niu/memory.json`。
```

替换为：
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

- [ ] **Step 2: 提交**

```bash
git add config/agents/niu.md
git commit -m "refactor(memory): fix main agent prompt — disk tool references, task type description, 9-slot memory"
```

---

## Task 11: 端到端验证

- [ ] **Step 1: Memory Server 启动无 ImportError**

Run: `cd mcp-servers/memory-server/src && python -c "import niu_memory_server; print('OK')"`
Expected: OK

- [ ] **Step 2: 3 个工具正常工作**

Run: `cd mcp-servers/memory-server/src && python -c "
import niu_memory_server as mod
# list
result = mod.user_memory_list()
print('list:', result['status'])
# remember
result = mod.user_memory_remember(content='测试记忆', type='memory')
print('remember:', result['status'])
# forget
result = mod.user_memory_forget(keyword='测试')
print('forget:', result['status'])
print('ALL OK')
"`
Expected: ALL OK

- [ ] **Step 3: 6 个旧工具不存在**

Run: `cd mcp-servers/memory-server/src && python -c "
import niu_memory_server as mod
schemas = mod.get_tool_schemas()
names = [s['name'] for s in schemas]
assert 'remember' not in names, 'remember should be removed'
assert 'recall' not in names, 'recall should be removed'
assert 'update_memory' not in names, 'update_memory should be removed'
assert 'get_memory_stats' not in names, 'get_memory_stats should be removed'
assert 'cleanup_memories' not in names, 'cleanup_memories should be removed'
assert 'link_memories' not in names, 'link_memories should be removed'
assert 'user_memory_remember' in names
assert 'user_memory_forget' in names
assert 'user_memory_list' in names
print('OK: 6 old tools removed, 3 kept tools present')
"`
Expected: OK

- [ ] **Step 4: brain_graph.py 无记忆方法**

Run: `python -c "
from niu_api.internal.brain_graph import BrainGraph
bg = BrainGraph.__new__(BrainGraph)
assert not hasattr(bg, 'store_memory'), 'store_memory should be removed'
assert not hasattr(bg, 'recall_memories'), 'recall_memories should be removed'
assert not hasattr(bg, '_infer_entity_type'), '_infer_entity_type should be removed'
assert hasattr(bg, 'ensure_niu_entity'), 'ensure_niu_entity should be kept'
print('OK: brain_graph memory methods removed, ensure_niu_entity kept')
"`

- [ ] **Step 5: brain_api.py 不存在**

Run: `test -f niu_api/brain_api.py && echo "FAIL: brain_api.py still exists" || echo "OK: brain_api.py deleted"`

- [ ] **Step 6: /api/brain/status 在 brain_region_api.py**

Run: `grep -n "brain_status" niu_api/brain_region_api.py && echo "OK: status endpoint found" || echo "FAIL: status endpoint missing"`

- [ ] **Step 7: runner.py 无 brain_memories_text 引用**

Run: `grep -n "brain_memories_text" agent/runner.py && echo "FAIL: brain_memories_text still referenced" || echo "OK: brain_memories_text removed"`

- [ ] **Step 8: handler.py 无 do_save_memory**

Run: `grep -n "do_save_memory" agent/handler.py && echo "FAIL: do_save_memory still exists" || echo "OK: do_save_memory removed"`

- [ ] **Step 9: pytest 通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_brain_graph.py tests/test_user_memory.py -v 2>&1 | tail -20`
Expected: 所有保留的测试通过

- [ ] **Step 10: 最终提交（如有验证修复）**

如果验证过程中发现需要修复的问题，修复后提交：
```bash
git add -A
git commit -m "fix(memory): verification fixes for memory server refactor"
```
