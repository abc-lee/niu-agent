# Virtual Disk Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all MCP tool schema injection with a single `disk()` tool, so the main Agent only sees 5 tools (4 base + disk).

**Architecture:** DiskEngine already exists (merged from feature/virtual-disk). We simplify runner.py to stop injecting MCP schemas and stop dynamic tool lifecycle management. System prompt gets disk description + directory listing. handler.py already has disk dispatch. Cleanup removes tool_lifecycle.py and related code.

**Tech Stack:** Python 3.11+, pytest, LightRAG (for skills/knowledge retrieval only)

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `agent/runner.py` | Simplify schema injection, remove dynamic logic, add disk description to system prompt |
| Modify | `agent/handler.py` | Remove tool_lifecycle hit_tool call from MCP dispatch path |
| Modify | `config/mcp-servers.yaml` | Set all MCP tools to `visibility: hidden` |
| Create | `config/disk/lightrag-server.yaml` | Disk config for 12 lightrag-server tools |
| Delete | `agent/tool_lifecycle.py` | No longer needed (MCP tools managed by disk, not scores) |
| Modify | `tests/test_tool_hit_integration.py` | Update tests for disk-only schema |
| Create | `tests/test_disk_integration.py` | Integration tests for disk mode |

---

### Task 1: Set all MCP tools to visibility: hidden

**Files:**
- Modify: `config/mcp-servers.yaml`

This is the simplest change — flip all `visibility: static` and `visibility: dynamic` to `visibility: hidden`. The 4 base tools (code_run, file_read, file_patch, file_write) come from `tools_schema.json` and are not affected by this config.

- [ ] **Step 1: Write the failing test**

Create `tests/test_disk_integration.py`:

```python
"""Integration tests for virtual disk mode — all MCP tools hidden."""

import pytest
from agent.tool_registry import get_registry, reset_registry


@pytest.fixture(autouse=True)
def clean_registry():
    """Reset ToolRegistry between tests."""
    reset_registry()
    yield
    reset_registry()


def test_no_static_or_dynamic_tools():
    """After disk mode, no MCP tool should be static or dynamic."""
    registry = get_registry()
    static = registry.get_static_tools()
    dynamic = [name for name, vis in registry._tool_visibility.items() if vis == "dynamic"]
    assert len(static) == 0, f"Static tools remain: {static}"
    assert len(dynamic) == 0, f"Dynamic tools remain: {dynamic}"


def test_runner_schema_only_base_plus_disk():
    """NiuRunner should inject only base tools + disk schema."""
    from agent.runner import NiuRunner
    runner = NiuRunner(
        llm_config={"apikey": "test", "model": "test", "apibase": "http://test"},
        mcp_client=None,
    )
    # Simulate set_mcp_tools_schema with empty list (all hidden)
    runner.set_mcp_tools_schema([])
    # base_tools_schema has 4 base + 5 sub-agent tools = 9
    # _mcp_tools_schema should only have disk
    mcp_names = [t["function"]["name"] for t in runner._mcp_tools_schema]
    assert "disk" in mcp_names
    # No MCP tool schemas should be present
    mcp_only = [n for n in mcp_names if n != "disk"]
    assert len(mcp_only) == 0, f"MCP schemas leaked: {mcp_only}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_disk_integration.py::test_no_static_or_dynamic_tools -v`
Expected: FAIL (current config has static/dynamic tools)

- [ ] **Step 3: Modify mcp-servers.yaml**

Change all `visibility: static` and `visibility: dynamic` to `visibility: hidden`:

```yaml
# lightrag-server — all hidden (accessible via disk)
lightrag-server:
  tools:
    lightrag_query: {visibility: hidden}
    lightrag_query_data: {visibility: hidden}
    lightrag_search_entities: {visibility: hidden}
    lightrag_get_graph: {visibility: hidden}
    lightrag_insert: {visibility: hidden}
    lightrag_insert_custom_kg: {visibility: hidden}
    lightrag_insert_entity: {visibility: hidden}
    lightrag_insert_relation: {visibility: hidden}
    lightrag_delete_entity: {visibility: hidden}
    lightrag_document_status: {visibility: hidden}
    lightrag_list_entities: {visibility: hidden}
    lightrag_merge_entities: {visibility: hidden}

# memory-server — all hidden
memory-server:
  tools:
    remember: {visibility: hidden}
    recall: {visibility: hidden}
    update_memory: {visibility: hidden}
    get_memory_stats: {visibility: hidden}
    cleanup_memories: {visibility: hidden}
    link_memories: {visibility: hidden}
    user_memory_remember: {visibility: hidden}
    user_memory_forget: {visibility: hidden}
    user_memory_list: {visibility: hidden}

# scheduler-server — all hidden
scheduler-server:
  tools:
    list_scheduled_tasks: {visibility: hidden}
    schedule_task: {visibility: hidden}
    cancel_task: {visibility: hidden}
    update_task: {visibility: hidden}

# browser-server — all hidden
browser-server:
  tools:
    browser_navigate: {visibility: hidden}
    browser_switch_tab: {visibility: hidden}
    browser_close_tab: {visibility: hidden}

# photo-server — all hidden
photo-server:
  tools:
    name_person: {visibility: hidden}
    merge_persons: {visibility: hidden}
    search_persons: {visibility: hidden}
    get_unnamed_persons: {visibility: hidden}
    delete_person: {visibility: hidden}
    cleanup_deleted_photos: {visibility: hidden}
    get_person_photos: {visibility: hidden}

# file-parser — all hidden
file-parser:
  tools:
    parse_file: {visibility: hidden}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_disk_integration.py::test_no_static_or_dynamic_tools -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config/mcp-servers.yaml tests/test_disk_integration.py
git commit -m "feat: set all MCP tools to visibility:hidden for disk mode, add integration test"
```

---

### Task 2: Simplify runner.py — schema injection + remove dynamic logic

**Files:**
- Modify: `agent/runner.py`

This is the core change. Simplify `set_mcp_tools_schema()`, `chat()`, and `_on_turn_end()` to only inject base tools + disk.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_disk_integration.py`:

```python
def test_chat_schema_only_base_plus_disk():
    """chat() should assemble only base + disk tools, no MCP schemas."""
    from unittest.mock import patch, MagicMock
    from agent.runner import NiuRunner

    runner = NiuRunner(
        llm_config={"apikey": "test", "model": "test", "apibase": "http://test"},
        mcp_client=None,
    )
    runner.set_mcp_tools_schema([])

    # Mock agent_runner_loop to capture tools_schema
    captured_schema = {}
    def mock_loop(**kwargs):
        captured_schema["tools"] = kwargs.get("tools_schema", [])
        yield "test response"

    with patch("agent.runner.agent_runner_loop", side_effect=mock_loop):
        list(runner.chat("test-session", "hello"))

    tool_names = [t["function"]["name"] for t in captured_schema["tools"]]
    # Should have base tools + sub-agent tools + disk
    assert "code_run" in tool_names
    assert "file_read" in tool_names
    assert "disk" in tool_names
    # Should NOT have any MCP tool schemas
    mcp_tools = [n for n in tool_names if "/" in n]
    assert len(mcp_tools) == 0, f"MCP schemas leaked into chat: {mcp_tools}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_disk_integration.py::test_chat_schema_only_base_plus_disk -v`
Expected: FAIL (chat() still injects static/dynamic MCP schemas)

- [ ] **Step 3: Simplify set_mcp_tools_schema()**

In `agent/runner.py`, replace `set_mcp_tools_schema()` to only inject disk schema:

```python
def set_mcp_tools_schema(self, tools: list):
    """Set MCP tool schemas — in disk mode, only inject disk() schema.

    All MCP tools are visibility=hidden and accessed via disk().
    The tools list is stored but not injected into LLM prompt.
    """
    self._mcp_tools_schema = tools  # Store for _get_tool_schema_by_name lookups
    logger.info(f"Loaded {len(tools)} MCP tools (all hidden, accessed via disk)")

    # Inject disk schema
    disk_schema = self.disk_engine.get_schema()
    # _mcp_tools_schema for schema lookup includes all tools + disk
    self._mcp_tools_schema_with_disk = tools + [disk_schema]
```

- [ ] **Step 4: Simplify chat() tools_schema assembly**

Replace the dynamic schema assembly in `chat()` (lines ~866-883):

```python
    # Assemble tools_schema = base tools + sub-agent tools + disk
    tools_schema = self.base_tools_schema.copy()

    # Add disk tool
    disk_schema = self.disk_engine.get_schema()
    tools_schema.append(disk_schema)

    logger.debug(
        f"tools_schema: {len(self.base_tools_schema)} base + 1 disk = {len(tools_schema)} total"
    )
```

- [ ] **Step 5: Simplify _on_turn_end()**

Replace `_on_turn_end()` to remove dynamic schema refresh and tool_lifecycle:

```python
def _on_turn_end(self, messages: list, tools_schema: list, turn: int) -> list:
    """每轮循环结束后刷新动态注入（skills/knowledge only, no MCP schema refresh)."""
    # Refresh user memories if dirty
    self._refresh_user_memories(messages)

    # Extract context and re-inject skills/knowledge
    context = self._extract_context_from_messages(messages)
    injection, _ = self._inject_dynamic_resources(context)

    # Update system_prompt
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = self.base_system_prompt + injection

    # No schema refresh — tools_schema stays base + disk
    return tools_schema
```

- [ ] **Step 6: Remove tool_lifecycle from __init__**

In `__init__`, remove:
```python
self.tool_lifecycle = ToolLifecycleManager(decay_rate=10, remove_threshold=25)
```

Remove the `from agent.tool_lifecycle import ToolLifecycleManager` import.

- [ ] **Step 7: Simplify _inject_dynamic_resources()**

Remove MCP tool score building (step 5) and tool-signal skills (step 2):

```python
def _inject_dynamic_resources(self, context: str) -> tuple[str, dict[str, int]]:
    """动态注入相关资源（Skills、知识）— no MCP tool scores."""
    # 1. LightRAG main search (skills + knowledge only)
    effective_query = context
    lightrag_results: dict[str, list[dict]] = {}
    lightrag_skill_names: set[str] = set()
    lightrag_available = True
    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter
        adapter = LightRAGAdapter()
        lightrag_results = adapter.search_multi_lightrag(
            effective_query, mode="hybrid", top_k=20,
        )
        for entity in lightrag_results.get("skill", []):
            entity_name = entity.get("entity_name", "")
            if entity_name.startswith("skill:"):
                lightrag_skill_names.add(entity_name[6:])
    except Exception as e:
        logger.warning(f"LightRAG retrieval failed: {e}")
        lightrag_available = False

    # 2. interaction_habits (LightRAG)
    interaction_habits: list[dict] = []
    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter
        habit_adapter = LightRAGAdapter()
        interaction_habits = habit_adapter.search_interaction_habits(
            query=effective_query, top_k=3,
        )
    except Exception as e:
        logger.debug(f"Interaction habits search failed (non-blocking): {e}")

    # 3. Brain graph memory recall
    brain_memories_text = ""
    try:
        from niu_api.internal.brain_graph import get_brain_graph, format_memories_for_prompt
        bg = get_brain_graph()
        brain_memories = bg.recall_memories(context, top_k=10, min_weight=0.3)
        if brain_memories:
            brain_memories_text = format_memories_for_prompt(brain_memories)
    except Exception as e:
        logger.debug(f"Brain graph recall failed (non-blocking): {e}")

    # ============== Format & Inject ==============
    parts = []
    seen_names: set[str] = set()

    # Skills
    lightrag_skills = lightrag_results.get("skill", [])
    skills_text, seen_names = self._format_lightrag_entities_for_prompt(
        lightrag_skills, "相关技能", seen_names,
    )
    if skills_text:
        parts.append(skills_text)

    # Knowledge
    lightrag_knowledge = lightrag_results.get("knowledge", [])
    knowledge_text, seen_names = self._format_lightrag_entities_for_prompt(
        lightrag_knowledge, "参考知识", seen_names,
    )
    if knowledge_text:
        parts.append(knowledge_text)
        parts.append(
            "\n\n### [知识探索指引]\n"
            "优先参考上述注入的历史参考信息回答用户问题。"
            "若命中知识点涉及已知实体，可使用 disk(\"/lightrag/query <实体名>\") 查询知识图谱。"
        )

    # Interaction habits
    if interaction_habits:
        habits_text, seen_names = self._format_lightrag_entities_for_prompt(
            interaction_habits, "交互习惯", seen_names,
        )
        if habits_text:
            parts.append(habits_text)

    # Brain memories
    if brain_memories_text:
        parts.append(brain_memories_text)

    injection = "\n".join(parts)
    return injection, {}  # Empty mcp_tool_scores — no dynamic MCP injection
```

- [ ] **Step 8: Remove dead methods**

Delete from `runner.py`:
- `_build_dynamic_tools_schema()`
- `_build_tool_scores_from_lightrag()`
- `_search_tool_signal_skills_lightrag()`
- `_get_static_tools()`
- `_apply_query_patterns()` (if exists and only used by MCP tool search)

- [ ] **Step 9: Remove tool_lifecycle.reset_session() from chat()**

In `chat()`, remove `self.tool_lifecycle.reset_session()`.

- [ ] **Step 10: Run test to verify it passes**

Run: `python -m pytest tests/test_disk_integration.py -v`
Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add agent/runner.py tests/test_disk_integration.py
git commit -m "feat: simplify runner for disk mode — base tools + disk only, remove dynamic MCP injection"
```

---

### Task 3: Add disk description to system prompt

**Files:**
- Modify: `agent/runner.py`

Add disk tool description + dynamic directory listing to system prompt in `_inject_dynamic_resources()`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_disk_integration.py`:

```python
def test_system_prompt_contains_disk_description():
    """System prompt should contain disk tool description and directory listing."""
    from agent.runner import NiuRunner
    runner = NiuRunner(
        llm_config={"apikey": "test", "model": "test", "apibase": "http://test"},
        mcp_client=None,
    )
    injection, _ = runner._inject_dynamic_resources("test query")
    # Should contain disk description
    assert "disk(command)" in injection or "disk" in runner.base_system_prompt
    # Should contain directory listing
    assert "/memory" in injection or "/memory" in runner.base_system_prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_disk_integration.py::test_system_prompt_contains_disk_description -v`
Expected: FAIL (no disk description in system prompt yet)

- [ ] **Step 3: Add disk description to _inject_dynamic_resources()**

At the beginning of `_inject_dynamic_resources()`, before the LightRAG search, add disk description:

```python
    # 0. Disk tool description + directory listing
    disk_description = self._build_disk_description()
    if disk_description:
        parts.append(disk_description)
```

Add new method `_build_disk_description()`:

```python
def _build_disk_description(self) -> str:
    """Build disk tool description with dynamic directory listing for system prompt."""
    try:
        dirs = self.disk_engine.config.list_directories()
    except Exception:
        return ""

    dir_lines = []
    for d in dirs:
        dir_lines.append(f"  /{d.directory:<10} — {d.description}")

    return (
        "\n\n### [虚拟磁盘工具]\n"
        "你有一个虚拟磁盘工具 disk(command)，可以用 Unix 命令探索和调用所有 MCP 工具。\n\n"
        "命令: ls [path] 列出目录, cat <path> 查看工具说明, /<dir>/<tool> [args] 执行工具\n\n"
        "当前磁盘目录:\n"
        + "\n".join(dir_lines)
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_disk_integration.py::test_system_prompt_contains_disk_description -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/runner.py tests/test_disk_integration.py
git commit -m "feat: add disk tool description + directory listing to system prompt"
```

---

### Task 4: Remove tool_lifecycle from handler.py

**Files:**
- Modify: `agent/handler.py`

Remove the `hit_tool()` call and `tool_lifecycle` references from MCP dispatch path.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_disk_integration.py`:

```python
def test_handler_no_tool_lifecycle_hit():
    """handler.dispatch() should not call tool_lifecycle.hit_tool() in disk mode."""
    # This test verifies that the hit_tool code block has been removed
    # by checking that handler.dispatch works without tool_lifecycle
    from agent.handler import NiuHandler
    handler = NiuHandler(mcp_client=None, disk_engine=None)
    # handler should not reference tool_lifecycle at all
    assert not hasattr(handler, 'tool_lifecycle')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_disk_integration.py::test_handler_no_tool_lifecycle_hit -v`
Expected: FAIL (handler still has tool_lifecycle references)

- [ ] **Step 3: Remove hit_tool block from handler.py**

In `handler.py`, in the MCP dispatch path (after `if "/" in tool_name:`), remove the entire `hit_tool` block (lines ~910-931):

```python
# DELETE THIS ENTIRE BLOCK:
if not getattr(self, '_is_subagent', False):
    try:
        from agent.runner import get_runner
        runner = get_runner()
        if runner and hasattr(runner, 'tool_lifecycle'):
            runner.tool_lifecycle.hit_tool(tool_name)
            current_score = runner.tool_lifecycle.get_tool_score(tool_name)
            print(f"[ToolHit] {tool_name} executed (lifecycle score: {current_score})", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[ToolHit] Failed to record hit: {e}", file=sys.stderr, flush=True)

    # Reinforce brain region on tool use
    try:
        from agent.brain_tools import reinforce_on_tool_use
        reinforce_on_tool_use(tool_name)
    except Exception:
        pass
```

Keep the brain region reinforcement (it's not tool_lifecycle related):

```python
# Keep brain region reinforcement (not tool_lifecycle)
if not getattr(self, '_is_subagent', False):
    try:
        from agent.brain_tools import reinforce_on_tool_use
        reinforce_on_tool_use(tool_name)
    except Exception:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_disk_integration.py::test_handler_no_tool_lifecycle_hit -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/handler.py tests/test_disk_integration.py
git commit -m "feat: remove tool_lifecycle.hit_tool from handler dispatch"
```

---

### Task 5: Create lightrag-server.yaml

**Files:**
- Create: `config/disk/lightrag-server.yaml`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_disk_integration.py`:

```python
def test_lightrag_directory_in_disk():
    """disk('ls /lightrag') should return lightrag-server tools."""
    from niu_api.internal.disk_engine import DiskEngine
    import os
    disk_config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "disk")
    engine = DiskEngine(disk_config_dir, registry=None)
    result = engine.execute("ls /lightrag")
    assert result.action == "LIST"
    assert "query" in result.text
    assert "insert" in result.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_disk_integration.py::test_lightrag_directory_in_disk -v`
Expected: FAIL (lightrag-server.yaml doesn't exist yet)

- [ ] **Step 3: Create lightrag-server.yaml**

Create `config/disk/lightrag-server.yaml`:

```yaml
server: lightrag-server
directory: lightrag
description: "LightRAG 知识图谱 — 查询、插入与图谱管理"

tools:
  - name: lightrag_query
    category: query
    short: "查询知识图谱"
    long: "搜索知识库。模式: local(实体), global(概览), hybrid(平衡), mix(图+向量), naive(仅向量)"
    parameters:
      - name: query
        position: 1
        type: string
        required: true
      - name: mode
        flag: mode
        type: string
        default: mix
        enum: [naive, local, global, hybrid, mix, bypass]
      - name: only_need_context
        flag: context-only
        type: boolean
        default: true
      - name: top_k
        flag: top-k
        type: integer
        default: 5
      - name: response_type
        flag: response-type
        type: string
        default: "Multiple Paragraphs"

  - name: lightrag_query_data
    category: query
    short: "结构化查询"
    long: "查询知识库返回结构化数据(实体+关系)，而非文本"
    parameters:
      - name: query
        position: 1
        type: string
        required: true
      - name: mode
        flag: mode
        type: string
        default: local
        enum: [naive, local, global, hybrid, mix, bypass]
      - name: top_k
        flag: top-k
        type: integer
        default: 10

  - name: lightrag_search_entities
    category: query
    short: "搜索实体"
    long: "按类型搜索知识图谱中的实体"
    parameters:
      - name: query
        position: 1
        type: string
        required: true
      - name: entity_type
        flag: type
        type: string
      - name: top_k
        flag: top-k
        type: integer
        default: 10

  - name: lightrag_get_graph
    category: query
    short: "获取子图"
    long: "获取知识图谱子图。explore=实体邻居, snapshot=全图"
    parameters:
      - name: action
        position: 1
        type: string
        required: true
        enum: [explore, snapshot]
      - name: entity_name
        flag: entity
        type: string
      - name: depth
        flag: depth
        type: integer
        default: 2
      - name: limit
        flag: limit
        type: integer
        default: 200

  - name: lightrag_insert
    category: write
    short: "插入文档"
    long: "插入文档到知识库，LightRAG自动提取实体和关系"
    parameters:
      - name: content
        position: 1
        type: string
        required: true
      - name: doc_id
        flag: doc-id
        type: string
      - name: file_path
        flag: file-path
        type: string

  - name: lightrag_insert_custom_kg
    category: write
    short: "注入结构化知识"
    long: "直接注入结构化知识(实体+关系+块)，绕过LLM提取"
    parameters:
      - name: entities
        flag: entities
        type: array
        cli_format: json
      - name: relationships
        flag: relationships
        type: array
        cli_format: json
      - name: chunks
        flag: chunks
        type: array
        cli_format: json
      - name: source_id
        flag: source-id
        type: string
        default: custom_kg

  - name: lightrag_insert_entity
    category: write
    short: "插入实体"
    long: "插入单个实体到知识图谱"
    parameters:
      - name: name
        position: 1
        type: string
        required: true
      - name: entity_type
        flag: type
        type: string
        required: true
      - name: description
        type: string
      - name: source_id
        flag: source-id
        type: string
        default: custom_kg

  - name: lightrag_insert_relation
    category: write
    short: "插入关系"
    long: "在两个实体间插入关系"
    parameters:
      - name: src_id
        position: 1
        type: string
        required: true
      - name: tgt_id
        position: 2
        type: string
        required: true
      - name: relation
        position: 3
        type: string
        required: true
      - name: description
        type: string

  - name: lightrag_delete_entity
    category: admin
    short: "删除实体"
    long: "从知识图谱删除实体及其所有关系"
    parameters:
      - name: entity_name
        position: 1
        type: string
        required: true

  - name: lightrag_document_status
    category: read
    short: "文档状态"
    long: "获取文档处理状态统计"
    hidden: true
    parameters: []

  - name: lightrag_list_entities
    category: read
    short: "列出实体"
    long: "列出知识库中的实体、文档或标签"
    parameters:
      - name: list_type
        flag: type
        type: string
        default: entities
        enum: [entities, documents, labels]
      - name: entity_type
        flag: entity-type
        type: string
      - name: limit
        flag: limit
        type: integer
        default: 50

  - name: lightrag_merge_entities
    category: admin
    short: "合并实体"
    long: "合并多个实体为一个，整合所有关系"
    parameters:
      - name: source_entities
        flag: sources
        type: array
        cli_format: json
        required: true
      - name: target_entity
        flag: target
        type: string
        required: true
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_disk_integration.py::test_lightrag_directory_in_disk -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config/disk/lightrag-server.yaml tests/test_disk_integration.py
git commit -m "feat: add lightrag-server.yaml for disk discovery"
```

---

### Task 6: Delete tool_lifecycle.py and update references

**Files:**
- Delete: `agent/tool_lifecycle.py`
- Modify: `agent/runner.py` (remove import)
- Modify: `agent/handler.py` (remove import)

- [ ] **Step 1: Find all references to tool_lifecycle**

Run: `grep -r "tool_lifecycle" --include="*.py" agent/ niu_api/ tests/`

Expected references to clean up:
- `agent/runner.py` — import and `self.tool_lifecycle` (already removed in Task 2)
- `agent/handler.py` — `runner.tool_lifecycle.hit_tool()` (already removed in Task 4)
- `tests/test_tool_hit_integration.py` — tests that mock tool_lifecycle

- [ ] **Step 2: Update test_tool_hit_integration.py**

Rewrite the 3 tests to work without tool_lifecycle. They should verify that disk mode works correctly instead:

```python
"""Tests for tool hit recording in disk mode."""
import pytest
from unittest.mock import patch, MagicMock


def test_disk_execute_records_in_context():
    """When disk executes a tool, the result should be in LLM context."""
    # In disk mode, tool execution goes through disk_engine,
    # which calls tool_registry internally. No tool_lifecycle needed.
    from niu_api.internal.disk_engine import DiskEngine
    import os
    disk_config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "disk")
    engine = DiskEngine(disk_config_dir, registry=None)
    # Navigation commands return text
    result = engine.execute("ls /")
    assert result.action == "LIST"
    assert result.text  # Non-empty directory listing


def test_no_tool_lifecycle_needed():
    """Verify tool_lifecycle module is not imported by runner."""
    import agent.runner as runner_module
    # tool_lifecycle should not be in the module's namespace
    assert not hasattr(runner_module, 'ToolLifecycleManager')


def test_handler_dispatch_without_lifecycle():
    """handler.dispatch() should work without tool_lifecycle."""
    from agent.handler import NiuHandler
    handler = NiuHandler(mcp_client=None, disk_engine=None)
    # No tool_lifecycle attribute
    assert not hasattr(handler, 'tool_lifecycle')
```

- [ ] **Step 3: Delete tool_lifecycle.py**

```bash
git rm agent/tool_lifecycle.py
```

- [ ] **Step 4: Remove any remaining imports**

Search for `from agent.tool_lifecycle import` or `import tool_lifecycle` and remove them.

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -q --tb=short`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: delete tool_lifecycle.py, update tests for disk mode"
```

---

### Task 7: Remove MCP→LightRAG registration from injector

**Files:**
- Modify: `niu_api/injector.py` — remove register_mcp_tool and register_mcp_tools_batch endpoints (or simplify to no-op)
- Modify: `agent/injector/sync.py` — remove MCP tool sync to LightRAG
- Modify: `agent/mcp_loader.py` — remove _inject_tools_to_lightrag call

- [ ] **Step 1: Identify MCP→LightRAG registration code**

In `niu_api/injector.py`, the `register_mcp_tool()` and `register_mcp_tools_batch()` endpoints register MCP tools to LightRAG. In disk mode, this is unnecessary — disk YAML config serves as the discovery mechanism.

In `agent/mcp_loader.py`, `_inject_tools_to_lightrag()` is called after loading MCP tools.

- [ ] **Step 2: Simplify injector.py endpoints**

Make `register_mcp_tool()` and `register_mcp_tools_batch()` return success without writing to LightRAG:

```python
@router.post("/mcp-tool", response_model=RegisterMCPToolResponse)
async def register_mcp_tool(request: RegisterMCPToolRequest):
    """Register MCP tool — no-op in disk mode (discovery via YAML config)."""
    doc_id = f"mcp_tool:{request.server_name}:{request.tool_name}"
    return RegisterMCPToolResponse(status="success", resource_id=doc_id)


@router.post("/mcp-tools/batch")
async def register_mcp_tools_batch(tools: list[RegisterMCPToolRequest]):
    """Batch register MCP tools — no-op in disk mode."""
    if not tools:
        return {"results": []}
    results = []
    for tool in tools:
        results.append({
            "tool_name": tool.tool_name,
            "status": "success",
            "resource_id": f"mcp_tool:{tool.server_name}:{tool.tool_name}",
        })
    return {"results": results}
```

- [ ] **Step 3: Remove _inject_tools_to_lightrag from mcp_loader.py**

Find and remove the call to `_inject_tools_to_lightrag()` in `agent/mcp_loader.py`.

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest tests/ -q --tb=short`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add niu_api/injector.py agent/mcp_loader.py
git commit -m "feat: simplify MCP registration to no-op in disk mode, remove LightRAG injection"
```

---

### Task 8: Final verification and cleanup

**Files:**
- All modified files

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 2: Verify disk tests specifically**

Run: `python -m pytest tests/test_disk_integration.py tests/test_disk_*.py -v`
Expected: All pass

- [ ] **Step 3: Verify no stale references**

Run: `grep -r "tool_lifecycle\|ToolLifecycleManager\|_build_dynamic_tools_schema\|_build_tool_scores_from_lightrag\|_search_tool_signal_skills_lightrag" --include="*.py" agent/ niu_api/`
Expected: No matches (all cleaned up)

- [ ] **Step 4: Verify disk directory listing works**

Run a quick Python check:
```python
from niu_api.internal.disk_engine import DiskEngine
import os
engine = DiskEngine(os.path.join("config", "disk"), registry=None)
result = engine.execute("ls /")
print(result.text)
assert "lightrag" in result.text
assert "memory" in result.text
```

- [ ] **Step 5: Commit final state**

```bash
git add -A
git commit -m "feat: virtual disk integration complete — all MCP tools via disk(), no dynamic injection"
```
