# 用户长期记忆驻留 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace periodic vector-search memory recall with permanent system prompt injection for user-stated memories, with trigger-based refresh.

**Architecture:** User memories stored in `memory.json` `permanent` array (max 5 items, ≤200 token each). Injected into system prompt as `### [用户长期记忆]` section at startup. On remember/forget tool calls, set dirty flag; next `_on_turn_end` refreshes the section in-place. Remove dead code: 5-turn recall, 10-turn global memory, `_should_remember`, `start_long_term_update`, `agent/memory/__init__.py`.

**Tech Stack:** Python, asyncio, FastAPI, MCP in-process architecture

---

### Task 1: Add remember/forget/list tools to memory-server

**Files:**
- Modify: `mcp-servers/memory-server/src/niu_memory_server/__init__.py`

- [ ] **Step 1: Add TOOL_SCHEMAS entries for user_memory_remember, user_memory_forget, user_memory_list**

Add to `TOOL_SCHEMAS` dict (after `link_memories` entry, around line 133):

```python
"user_memory_remember": {
    "name": "user_memory_remember",
    "description": "添加用户长期记忆（最多5条，每条≤200 token）。记忆将永久驻留在系统提示词中。若已满(5/5)，必须先调用 user_memory_forget 删除旧记忆。",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "记忆内容（≤200 token，约300中文字符）",
            },
        },
        "required": ["content"],
    },
},
"user_memory_forget": {
    "name": "user_memory_forget",
    "description": "删除用户长期记忆。按序号(index)或关键词(keyword)匹配删除。",
    "input_schema": {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "记忆序号（1-5），优先于 keyword",
            },
            "keyword": {
                "type": "string",
                "description": "不区分大小写的子串匹配",
            },
        },
    },
},
"user_memory_list": {
    "name": "user_memory_list",
    "description": "查看当前所有用户长期记忆",
    "input_schema": {
        "type": "object",
        "properties": {},
    },
},
```

- [ ] **Step 2: Add Tool definitions for the three new tools**

Add to `get_tool_definitions()` return list (after the `link_memories` Tool):

```python
Tool(
    name="user_memory_remember",
    description="添加用户长期记忆（最多5条，每条≤200 token）。记忆将永久驻留在系统提示词中。若已满(5/5)，必须先调用 user_memory_forget 删除旧记忆。",
    inputSchema={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "记忆内容（≤200 token，约300中文字符）"},
        },
        "required": ["content"],
    },
),
Tool(
    name="user_memory_forget",
    description="删除用户长期记忆。按序号(index)或关键词(keyword)匹配删除。",
    inputSchema={
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "记忆序号（1-5），优先于 keyword"},
            "keyword": {"type": "string", "description": "不区分大小写的子串匹配"},
        },
    },
),
Tool(
    name="user_memory_list",
    description="查看当前所有用户长期记忆",
    inputSchema={
        "type": "object",
        "properties": {},
    },
),
```

- [ ] **Step 3: Implement the three handler functions**

Add before the `# MCP handlers` section (around line 364):

```python
# ============================================================================
# User memory tools (memory.json permanent array)
# ============================================================================

MEMORY_JSON_PATH = None  # Set at first call

def _get_memory_json_path():
    """Get path to ~/.niu/memory.json"""
    global MEMORY_JSON_PATH
    if MEMORY_JSON_PATH is None:
        from pathlib import Path
        MEMORY_JSON_PATH = Path.home() / ".niu" / "memory.json"
    return MEMORY_JSON_PATH

MAX_PERMANENT_ITEMS = 5
MAX_TOKEN_PER_ITEM = 200  # ~300 Chinese chars

def _read_memory_json() -> dict:
    """Read memory.json, return dict with at least {permanent: []}"""
    import json
    path = _get_memory_json_path()
    if not path.exists():
        return {"permanent": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "permanent" not in data:
            data["permanent"] = []
        # Truncate if over limit (keep first 5, drop from end)
        if len(data["permanent"]) > MAX_PERMANENT_ITEMS:
            data["permanent"] = data["permanent"][:MAX_PERMANENT_ITEMS]
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
    except Exception:
        return {"permanent": []}

def _write_memory_json(data: dict):
    """Write memory.json"""
    import json
    path = _get_memory_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def user_memory_remember_handler(content: str) -> dict:
    """添加用户长期记忆到 memory.json permanent 数组"""
    data = _read_memory_json()
    permanent = data["permanent"]

    if len(permanent) >= MAX_PERMANENT_ITEMS:
        return {
            "status": "error",
            "message": f"记忆已满({len(permanent)}/{MAX_PERMANENT_ITEMS})，请先调用 user_memory_forget 删除旧记忆。",
            "current_memories": permanent,
        }

    # Rough token estimate: 1 token ≈ 1.5 Chinese chars
    estimated_tokens = len(content) / 1.5
    if estimated_tokens > MAX_TOKEN_PER_ITEM:
        return {
            "status": "error",
            "message": f"记忆内容过长（约{int(estimated_tokens)} token，上限{MAX_TOKEN_PER_ITEM}），请精简后重试。",
        }

    permanent.append(content)
    _write_memory_json(data)

    return {
        "status": "success",
        "message": f"✅ 已添加记忆({len(permanent)}/{MAX_PERMANENT_ITEMS})",
        "current_memories": permanent,
    }


async def user_memory_forget_handler(index: int = None, keyword: str = None) -> dict:
    """删除用户长期记忆"""
    data = _read_memory_json()
    permanent = data["permanent"]

    if not permanent:
        return {"status": "error", "message": "没有可删除的记忆"}

    if index is not None:
        # Index is 1-based
        if index < 1 or index > len(permanent):
            return {"status": "error", "message": f"序号超出范围(1-{len(permanent)})"}
        removed = permanent.pop(index - 1)
        _write_memory_json(data)
        return {
            "status": "success",
            "message": f"✅ 已删除第{index}条记忆: {removed}",
            "current_memories": permanent,
        }

    if keyword:
        keyword_lower = keyword.lower()
        for i, item in enumerate(permanent):
            if keyword_lower in item.lower():
                removed = permanent.pop(i)
                _write_memory_json(data)
                return {
                    "status": "success",
                    "message": f"✅ 已删除匹配'{keyword}'的记忆: {removed}",
                    "current_memories": permanent,
                }
        return {"status": "error", "message": f"未找到包含'{keyword}'的记忆", "current_memories": permanent}

    return {"status": "error", "message": "请提供 index 或 keyword 参数"}


async def user_memory_list_handler() -> dict:
    """查看当前所有用户长期记忆"""
    data = _read_memory_json()
    permanent = data["permanent"]

    return {
        "status": "success",
        "count": len(permanent),
        "max": MAX_PERMANENT_ITEMS,
        "memories": permanent,
    }
```

- [ ] **Step 4: Add dispatch in call_tool handler**

Add elif branches in `call_tool()` (before the `else: return [TextContent...]` line):

```python
elif name == "user_memory_remember":
    result = await user_memory_remember_handler(
        content=arguments["content"],
    )
elif name == "user_memory_forget":
    result = await user_memory_forget_handler(
        index=arguments.get("index"),
        keyword=arguments.get("keyword"),
    )
elif name == "user_memory_list":
    result = await user_memory_list_handler()
```

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/memory-server/src/niu_memory_server/__init__.py
git commit -m "feat: add user_memory_remember/forget/list tools to memory-server"
```

---

### Task 2: Inject `### [用户长期记忆]` into system prompt at startup

**Files:**
- Modify: `agent/runner.py:109-115`

- [ ] **Step 1: Update `_load_memory_for_prompt()` to use `### [用户长期记忆]` format**

Replace lines 109-115 in `agent/runner.py`:

Old:
```python
    # 永久记忆（用户特别强调的、工作原则性的内容，每轮对话都加载）
    permanent = memory.get("permanent", [])
    if permanent:
        perm_str = "## 永久记忆\n\n以下内容用户特别强调或为工作原则，必须始终遵守：\n"
        for item in permanent:
            perm_str += f"- {item}\n"
        parts.append(perm_str)
```

New:
```python
    # 用户长期记忆（驻留在 system prompt，最多5条，每条≤200 token）
    permanent = memory.get("permanent", [])
    if permanent:
        perm_str = "### [用户长期记忆]\n以下内容用户特别强调，必须始终遵守：\n"
        for i, item in enumerate(permanent, 1):
            perm_str += f"{i}. {item}\n"
        perm_str += f"\n（共{len(permanent)}/5条，使用 memory-server/user_memory_remember 添加，memory-server/user_memory_forget 删除）"
        parts.append(perm_str)
```

- [ ] **Step 2: Commit**

```bash
git add agent/runner.py
git commit -m "feat: inject user permanent memories as ### [用户长期记忆] section in system prompt"
```

---

### Task 3: Add dirty-flag refresh in `_on_turn_end()`

**Files:**
- Modify: `agent/runner.py:371-417` (_on_turn_end method)
- Modify: `agent/handler.py` (set dirty flag on tool call)

- [ ] **Step 1: Add `_memory_dirty` flag to NiuRunner.__init__**

Find `self.base_system_prompt` assignment in `NiuRunner.__init__` and add after it:

```python
self._memory_dirty = False
```

- [ ] **Step 2: Add `_refresh_user_memories()` method to NiuRunner**

Add as a method on `NiuRunner` class (after `_on_turn_end`):

```python
def _refresh_user_memories(self, messages: list):
    """Refresh the ### [用户长期记忆] section in system prompt if dirty"""
    if not self._memory_dirty:
        return
    self._memory_dirty = False

    if not messages or messages[0].get("role") != "system":
        return

    import re
    from pathlib import Path
    import json

    # Read current permanent memories
    memory_path = Path.home() / ".niu" / "memory.json"
    try:
        data = json.loads(memory_path.read_text(encoding="utf-8"))
        permanent = data.get("permanent", [])
    except Exception:
        return

    # Build new section
    if permanent:
        new_section = "### [用户长期记忆]\n以下内容用户特别强调，必须始终遵守：\n"
        for i, item in enumerate(permanent, 1):
            new_section += f"{i}. {item}\n"
        new_section += f"\n（共{len(permanent)}/5条，使用 memory-server/user_memory_remember 添加，memory-server/user_memory_forget 删除）"
    else:
        new_section = ""

    # Replace or append the section in system prompt
    content = messages[0]["content"]
    pattern = r'### \[用户长期记忆\]\n.*?(?=\n###|\Z)'

    if re.search(pattern, content, re.DOTALL):
        if new_section:
            messages[0]["content"] = re.sub(pattern, new_section.rstrip(), content, flags=re.DOTALL)
        else:
            messages[0]["content"] = re.sub(r'\n*### \[用户长期记忆\]\n.*?(?=\n###|\Z)', '', content, flags=re.DOTALL)
    elif new_section:
        messages[0]["content"] = content + "\n\n" + new_section
```

- [ ] **Step 3: Call `_refresh_user_memories` from `_on_turn_end`**

In `_on_turn_end()`, add after the `decay_tools()` call (line 384) and before the context extraction:

```python
        # 0. Refresh user memories if dirty
        self._refresh_user_memories(messages)
```

- [ ] **Step 4: Set `_memory_dirty` flag when user_memory tools are called**

In `agent/handler.py`, find `tool_after_callback` method. Add at the beginning of the method (after the docstring):

```python
        # Set memory dirty flag when user memory tools are called
        if tool_name in ("memory-server/user_memory_remember", "memory-server/user_memory_forget"):
            from agent.runner import get_runner
            runner = get_runner()
            if runner and hasattr(runner, '_memory_dirty'):
                runner._memory_dirty = True
```

- [ ] **Step 5: Commit**

```bash
git add agent/runner.py agent/handler.py
git commit -m "feat: trigger-based refresh of user memories in system prompt on remember/forget"
```

---

### Task 4: Remove dead memory code from handler.py

**Files:**
- Modify: `agent/handler.py`

- [ ] **Step 1: Remove the 5-turn recall block (lines 565-569)**

Delete:
```python
        # 增强：每 5 轮注入相关长期记忆
        if turn % 5 == 0 and turn > 0:
            memories = self._recall_relevant_memories(next_prompt)
            if memories:
                next_prompt += f"\n\n### [相关长期记忆]\n{memories}"
```

- [ ] **Step 2: Remove the suggest_remember block (lines 571-580)**

Delete:
```python
        # 增强：如果有建议记忆的标记，提示 LLM
        if self.working.get("suggest_remember"):
            reason = self.working.get("remember_reason", "")
            next_prompt += (
                f"\n\n[SYSTEM TIP] 检测到值得长期记忆的信息: {reason}。"
                "建议调用 start_long_term_update 提炼记忆。"
            )
            # 清除标记
            self.working.pop("suggest_remember", None)
            self.working.pop("remember_reason", None)
```

- [ ] **Step 3: Remove the 10-turn global memory block (lines 582-591)**

Delete:
```python
        # 每 10 轮注入全局记忆
        if turn % 10 == 0 and turn > 0:
            from .generic.handler import get_global_memory

            try:
                global_mem = get_global_memory()
                if global_mem:
                    next_prompt += f"\n\n### [GLOBAL MEMORY]\n{global_mem}"
            except Exception:
                pass
```

- [ ] **Step 4: Remove `_should_remember()` method (lines 462-484)**

Delete the entire `_should_remember` method.

- [ ] **Step 5: Remove `_get_remember_reason()` method (lines 486-...)**

Delete the entire `_get_remember_reason` method.

- [ ] **Step 6: Remove `do_start_long_term_update()` method (lines 937-...)**

Delete the entire `do_start_long_term_update` method and its helper methods `_infer_memory_type`, `_generate_memory_content`, `_generate_memory_title` (if they exist as separate methods).

- [ ] **Step 7: Remove `_recall_relevant_memories()` method (lines 595-...)**

Delete the entire `_recall_relevant_memories` method and its helper `_extract_keywords` (if it exists as a separate method).

- [ ] **Step 8: Remove `suggest_remember` flag setting in `tool_after_callback`**

Find and remove any lines that set `self.working["suggest_remember"]` or `self.working["remember_reason"]` in `tool_after_callback`.

- [ ] **Step 9: Remove `start_long_term_update` from tool dispatch**

Find where `start_long_term_update` is registered as a tool name in the handler's dispatch logic and remove it.

- [ ] **Step 10: Commit**

```bash
git add agent/handler.py
git commit -m "refactor: remove dead memory code — 5-turn recall, 10-turn global memory, suggest_remember, start_long_term_update"
```

---

### Task 5: Remove dead `agent/memory/__init__.py` module

**Files:**
- Delete: `agent/memory/__init__.py`

- [ ] **Step 1: Verify no imports of agent.memory exist**

Run: `grep -rn "from agent.memory\|import agent.memory" agent/ niu_api/ scripts/ config/`
Expected: No results (dead module)

- [ ] **Step 2: Delete the file**

```bash
rm agent/memory/__init__.py
```

Also remove the `agent/memory/` directory if it only contains `__init__.py`.

- [ ] **Step 3: Commit**

```bash
git add agent/memory/
git commit -m "chore: remove dead agent/memory module"
```

---

### Task 6: Update niu.md with new memory tool instructions

**Files:**
- Modify: `config/agents/niu.md`

- [ ] **Step 1: Replace old memory instructions with new tool guidance**

Find the memory-related section in `niu.md` and replace with:

```markdown
# 用户长期记忆

使用 memory-server 工具管理用户长期记忆。记忆驻留在系统提示词中，始终生效。

- **添加记忆**：`memory-server/user_memory_remember`，参数 content（≤200 token）
- **删除记忆**：`memory-server/user_memory_forget`，参数 index（序号1-5）或 keyword（子串匹配）
- **查看记忆**：`memory-server/user_memory_list`

**限制**：最多5条。已满时必须先删旧的再加新的。每条≤200 token，请精炼内容。
```

- [ ] **Step 2: Commit**

```bash
git add config/agents/niu.md
git commit -m "docs: update niu.md with new user memory tool instructions"
```

---

### Task 7: Write and run tests

**Files:**
- Create: `tests/test_user_memory.py`

- [ ] **Step 1: Write tests**

```python
#!/usr/bin/env python3
"""Test user memory tools (remember/forget/list)"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_memory_json(tmp_path, permanent=None):
    """Create a temporary memory.json"""
    data = {"permanent": permanent or []}
    path = tmp_path / ".niu" / "memory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_user_memory_remember():
    """Adding memories up to limit"""
    from mcp_servers_memory_server import user_memory_remember_handler, _read_memory_json, MAX_PERMANENT_ITEMS

    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        with patch("mcp_servers_memory_server.__init__._get_memory_json_path", return_value=memory_path):
            # Add first memory
            result = await user_memory_remember_handler("我喜欢Python")
            assert result["status"] == "success"
            assert result["current_memories"] == ["我喜欢Python"]

            # Add second
            result = await user_memory_remember_handler("密码是abc")
            assert result["status"] == "success"
            assert len(result["current_memories"]) == 2


def test_user_memory_remember_full():
    """Reject when memory is full"""
    from mcp_servers_memory_server import user_memory_remember_handler, MAX_PERMANENT_ITEMS

    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        with patch("mcp_servers_memory_server.__init__._get_memory_json_path", return_value=memory_path):
            # Fill to max
            for i in range(MAX_PERMANENT_ITEMS):
                result = await user_memory_remember_handler(f"记忆{i}")
                assert result["status"] == "success"

            # Try to add one more
            result = await user_memory_remember_handler("超限记忆")
            assert result["status"] == "error"
            assert "已满" in result["message"]


def test_user_memory_forget_by_index():
    """Delete by 1-based index"""
    from mcp_servers_memory_server import user_memory_forget_handler

    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        with patch("mcp_servers_memory_server.__init__._get_memory_json_path", return_value=memory_path):
            # Pre-populate
            data = {"permanent": ["A", "B", "C"]}
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            memory_path.write_text(json.dumps(data), encoding="utf-8")

            result = await user_memory_forget_handler(index=2)
            assert result["status"] == "success"
            assert "B" in result["message"]
            assert result["current_memories"] == ["A", "C"]


def test_user_memory_forget_by_keyword():
    """Delete by keyword substring match"""
    from mcp_servers_memory_server import user_memory_forget_handler

    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        with patch("mcp_servers_memory_server.__init__._get_memory_json_path", return_value=memory_path):
            data = {"permanent": ["我喜欢Python", "密码是abc", "每周五例会"]}
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            memory_path.write_text(json.dumps(data), encoding="utf-8")

            result = await user_memory_forget_handler(keyword="密码")
            assert result["status"] == "success"
            assert result["current_memories"] == ["我喜欢Python", "每周五例会"]


def test_user_memory_list():
    """List all memories"""
    from mcp_servers_memory_server import user_memory_list_handler

    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        with patch("mcp_servers_memory_server.__init__._get_memory_json_path", return_value=memory_path):
            data = {"permanent": ["A", "B"]}
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            memory_path.write_text(json.dumps(data), encoding="utf-8")

            result = await user_memory_list_handler()
            assert result["status"] == "success"
            assert result["count"] == 2
            assert result["max"] == 5
            assert result["memories"] == ["A", "B"]


def test_truncate_over_limit():
    """Truncate permanent array > 5 on load"""
    from mcp_servers_memory_server import _read_memory_json, MAX_PERMANENT_ITEMS

    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        data = {"permanent": [f"记忆{i}" for i in range(8)]}
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        with patch("mcp_servers_memory_server.__init__._get_memory_json_path", return_value=memory_path):
            result = _read_memory_json()
            assert len(result["permanent"]) == MAX_PERMANENT_ITEMS
            # Kept first 5
            assert result["permanent"][0] == "记忆0"
            assert result["permanent"][4] == "记忆4"


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_user_memory_remember())
    asyncio.run(test_user_memory_remember_full())
    asyncio.run(test_user_memory_forget_by_index())
    asyncio.run(test_user_memory_forget_by_keyword())
    asyncio.run(test_user_memory_list())
    test_truncate_over_limit()
    print("All tests passed!")
```

- [ ] **Step 2: Run tests and fix import paths as needed**

Run: `python -m pytest tests/test_user_memory.py -v`
Expected: All PASS (may need import path adjustments)

- [ ] **Step 3: Commit**

```bash
git add tests/test_user_memory.py
git commit -m "test: add user memory remember/forget/list tests"
```

---

### Task 8: Verify end-to-end and final commit

- [ ] **Step 1: Run all existing tests to ensure no regressions**

Run: `python -m pytest tests/ -v --timeout=30`
Expected: All PASS

- [ ] **Step 2: Manual smoke test — verify system prompt contains `### [用户长期记忆]`**

Start the API server and check that the system prompt includes the memory section. Verify that calling `memory-server/user_memory_remember` adds a memory and the next chat turn sees it in the system prompt.

- [ ] **Step 3: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: user memory resident — final integration fixes"
```
