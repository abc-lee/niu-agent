# 修复工具命中记录 + Skills 指引强化

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 MCP 工具命中记录（在工具真正执行时记录，而非向量检索时），添加分数持久化机制，并在 niu.md 中强化 Skills 使用指引。

**Architecture:**
1. 将命中记录从 `_inject_dynamic_resources()` 移到 `handler.dispatch()`
2. ToolLifecycleManager 添加 JSON 文件持久化（`~/.niu/tool_scores.json`）
3. 在 niu.md 中添加自然的 Skills 使用建议

**Tech Stack:** Python 3.11+, JSON 文件存储

---

## File Structure

**Modified Files:**
- `agent/tool_lifecycle.py` - 添加持久化存储
- `agent/handler.py` - 在 dispatch() 中记录工具命中
- `agent/runner.py` - 移除错误的命中记录，调用持久化
- `config/agents/niu.md` - 添加 Skills 使用指引

---

## Task 1: ToolLifecycleManager 添加持久化

**Files:**
- Modify: `agent/tool_lifecycle.py`

- [ ] **Step 1: Add JSON persistence to ToolLifecycleManager**

```python
"""
工具生命周期管理

管理工具在对话单元中的生命周期，实现分数衰减机制。
支持持久化存储，程序重启后保留工具分数。
"""

import json
from pathlib import Path
from typing import Dict, List


class ToolLifecycleManager:
    """管理工具在对话单元中的生命周期（带持久化）"""

    def __init__(self, decay_rate: int = 10, min_score: int = 50):
        """
        Args:
            decay_rate: 每轮衰减分数（默认10分/轮）
            min_score: 低于此分数移除工具（默认50分）
        """
        self.scores_path = Path.home() / ".niu" / "tool_scores.json"
        self.decay_rate = decay_rate
        self.min_score = min_score
        self.active_tools: Dict[str, int] = self._load_scores()

    def _load_scores(self) -> Dict[str, int]:
        """从 JSON 文件加载工具分数"""
        if not self.scores_path.exists():
            return {}

        try:
            return json.loads(self.scores_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_scores(self):
        """保存工具分数到 JSON 文件"""
        self.scores_path.parent.mkdir(parents=True, exist_ok=True)
        self.scores_path.write_text(
            json.dumps(self.active_tools, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    def hit_tool(self, tool_name: str):
        """
        工具被命中，重置为100分

        Args:
            tool_name: 工具名，格式为 "server-name/tool-name"
        """
        self.active_tools[tool_name] = 100
        self._save_scores()

    def decay_tools(self):
        """
        每轮对话后衰减所有工具分数

        规则：
        - 所有工具分数 -decay_rate
        - 分数 < min_score 的工具被移除
        - 保存到文件
        """
        to_remove = []
        for tool_name, score in self.active_tools.items():
            new_score = score - self.decay_rate
            self.active_tools[tool_name] = new_score

            if new_score < self.min_score:
                to_remove.append(tool_name)

        for tool_name in to_remove:
            del self.active_tools[tool_name]

        self._save_scores()

    def get_active_tools(self) -> List[str]:
        """
        获取当前应该注入的工具列表

        Returns:
            活跃工具名列表
        """
        return list(self.active_tools.keys())

    def clear(self):
        """清空所有活跃工具"""
        self.active_tools.clear()
        self._save_scores()

    def get_tool_score(self, tool_name: str) -> int:
        """
        获取指定工具的当前分数

        Args:
            tool_name: 工具名

        Returns:
            当前分数，如果工具不存在返回0
        """
        return self.active_tools.get(tool_name, 0)

    def debug_print(self):
        """调试：打印所有活跃工具及其分数"""
        if not self.active_tools:
            print("[ToolLifecycle] No active tools")
            return

        print("[ToolLifecycle] Active tools:")
        for tool_name, score in sorted(self.active_tools.items(), key=lambda x: -x[1]):
            print(f"  {tool_name}: {score}")
```

- [ ] **Step 2: Write unit test for persistence**

```python
# tests/test_tool_lifecycle_persistence.py
import json
import tempfile
from pathlib import Path

import pytest

from agent.tool_lifecycle import ToolLifecycleManager


@pytest.fixture
def temp_storage(tmp_path):
    """Use temporary directory for tool scores"""
    original_home = Path.home()
    temp_home = tmp_path / "home"
    temp_home.mkdir(parents=True)

    original_home_method = Path.home
    Path.home = lambda: temp_home

    yield temp_home

    Path.home = original_home_method


def test_tool_score_persistence(temp_storage):
    """Test that tool scores persist to JSON file"""
    manager = ToolLifecycleManager()
    manager.hit_tool("browser-server/browser_navigate")

    # Check file exists
    scores_file = temp_storage / ".niu" / "tool_scores.json"
    assert scores_file.exists()

    # Check content
    scores = json.loads(scores_file.read_text(encoding="utf-8"))
    assert scores["browser-server/browser_navigate"] == 100


def test_persistence_across_instances(temp_storage):
    """Test that scores persist across manager instances"""
    # First instance
    manager1 = ToolLifecycleManager()
    manager1.hit_tool("test-server/test-tool")

    # Second instance (should load from file)
    manager2 = ToolLifecycleManager()
    assert manager2.get_tool_score("test-server/test-tool") == 100


def test_decay_saves_to_file(temp_storage):
    """Test that decay updates the file"""
    manager = ToolLifecycleManager()
    manager.hit_tool("test-server/test-tool")

    manager.decay_tools()

    # Check file updated
    scores_file = temp_storage / ".niu" / "tool_scores.json"
    scores = json.loads(scores_file.read_text(encoding="utf-8"))
    assert scores["test-server/test-tool"] == 90
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_tool_lifecycle_persistence.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add agent/tool_lifecycle.py tests/test_tool_lifecycle_persistence.py
git commit -m "feat: add JSON persistence to ToolLifecycleManager

- Store tool scores in ~/.niu/tool_scores.json
- Load scores on initialization
- Save after hit_tool() and decay_tools()
- Survive program restarts"
```

---

## Task 2: 修复工具命中记录位置

**Files:**
- Modify: `agent/handler.py::dispatch()`
- Modify: `agent/runner.py::_inject_dynamic_resources()`

- [ ] **Step 1: Add hit recording in handler.dispatch()**

在 `agent/handler.py` 中，找到 `dispatch()` 方法（约第 1030 行），在 MCP 工具执行前记录命中：

```python
def dispatch(self, tool_name, args, response, index=0):
    """分发工具调用（支持 MCP 工具）- 必须是生成器"""
    # 先检查内置工具（工具名中的 - 转换为 _）
    method_name = f"do_{tool_name.replace('-', '_')}"
    if hasattr(self, method_name):
        # 直接调用方法，不委托给 super（因为 super 会用原始 tool_name 查找）
        args["_index"] = index
        prer = yield from try_call_generator(
            self.tool_before_callback, tool_name, args, response
        )
        ret = yield from try_call_generator(getattr(self, method_name), args, response)
        _ = yield from try_call_generator(
            self.tool_after_callback, tool_name, args, response, ret
        )
        return ret

    # 检查 MCP 工具（工具名格式：server/tool）
    if "/" in tool_name:
        try:
            from agent.tool_registry import get_registry

            # 从 ToolRegistry 获取工具函数
            func = get_registry().get(tool_name)

            if func is None:
                yield f"[MCP Error] Tool not found: {tool_name}\n"
                return StepOutcome(
                    {"status": "error", "error_code": "TOOL_NOT_FOUND", "msg": f"Tool {tool_name} not found in registry"},
                    next_prompt=self._get_anchor_prompt()
                )

            # 【新增】记录工具命中（在真正执行前）
            try:
                from agent.runner import get_runner
                runner = get_runner()
                if runner and hasattr(runner, 'tool_lifecycle'):
                    runner.tool_lifecycle.hit_tool(tool_name)
                    print(f"[ToolHit] {tool_name} executed (score: 100)", file=sys.stderr, flush=True)
            except Exception as e:
                # 命中记录失败不影响主流程
                print(f"[ToolHit] Failed to record hit: {e}", file=sys.stderr, flush=True)

            # 直接调用工具函数
            result = func(**args)

            yield f"[MCP] {tool_name} executed\n"

            # 判断任务是否完成：
            # - 成功后让LLM向用户汇报结果
            if isinstance(result, dict) and result.get("status") == "success":
                # 成功执行，提示LLM向用户汇报
                result_summary = json.dumps(result, ensure_ascii=False)[:500]
                return StepOutcome(result, next_prompt=f"工具调用成功。请向用户简洁汇报结果：{result_summary}")
            else:
                # 需要进一步处理，返回anchor prompt
                return StepOutcome(result, next_prompt=self._get_anchor_prompt())
        except Exception as e:
            yield f"[MCP Error] {tool_name}: {e}\n"
            return StepOutcome(
                {"status": "error", "msg": str(e)}, next_prompt=self._get_anchor_prompt()
            )

    # 未知工具
    yield f"Unknown tool: {tool_name}\n"
    return StepOutcome(None, next_prompt=f"Unknown tool: {tool_name}")
```

- [ ] **Step 2: Remove incorrect hit recording in runner.py**

在 `agent/runner.py` 中，找到 `_inject_dynamic_resources()` 方法（约第 459-472 行），删除错误的命中记录：

```python
# 【删除这段代码】
# 1. 向量检索工具（使用上下文，而不是单纯的user_input）
matched_tools = self.vector_search.search(
    query=context,
    limit=3,
    min_score=0.5,
    filter={'category': 'mcp_tool'}
)

# 2. 更新工具生命周期（命中工具设置为100分）
for result in matched_tools:
    tool_name = result.metadata.get('name')
    server = result.metadata.get('server')
    full_name = f"{server}/{tool_name}"
    self.tool_lifecycle.hit_tool(full_name)

# 3. 获取所有活跃工具（包括命中的 + 之前未衰减完的）
active_tool_names = self.tool_lifecycle.get_active_tools()
```

改为：

```python
# 1. 获取所有活跃工具（之前未衰减完的）
active_tool_names = self.tool_lifecycle.get_active_tools()
```

- [ ] **Step 3: Commit**

```bash
git add agent/handler.py agent/runner.py
git commit -m "fix: move tool hit recording from vector search to actual execution

- Record hit in handler.dispatch() when tool is actually called
- Remove incorrect hit recording in _inject_dynamic_resources()
- Only vector search results that lead to execution are counted as hits"
```

---

## Task 3: 在 niu.md 中添加 Skills 使用指引

**Files:**
- Modify: `config/agents/niu.md`

- [ ] **Step 1: Add Skills usage guidance**

在 `config/agents/niu.md` 中添加自然的 Skills 使用建议：

```markdown
## Skills 使用建议

系统会根据对话内容自动注入相关的 Skills 摘要。当你看到"相关技能"部分时：

1. **先看摘要判断价值**：摘要包含标题、关键词、简介和文件路径
2. **有用就读完整文件**：调用 `file_read` 读取摘要中的文件路径
3. **按文件指引操作**：不要自己猜测，按照 Skill 文件中的具体步骤执行

**示例**：
```
系统提示词：
### [相关技能]
1. **browser-automation** (分数: 85)
   Browser automation|browser,form filling|Use Playwright for web automation
   文件路径: memory/skills/browser-automation.md

你应该调用：
file_read(path="memory/skills/browser-automation.md")
```

**注意**：
- Skills 是经验总结，包含了最佳实践和避坑指南
- 读完文件后，内容会保留在上下文中，后续不需要重复读取
- 如果摘要看起来不相关，可以忽略，不必强制读取
```

- [ ] **Step 2: Commit**

```bash
git add config/agents/niu.md
git commit -m "docs: add natural Skills usage guidance in niu.md

- Encourage reading Skill files when relevant
- Emphasize following Skill instructions
- Not mandatory, let LLM judge relevance"
```

---

## Task 4: 集成测试

**Files:**
- Create: `tests/test_tool_hit_integration.py`

- [ ] **Step 1: Write integration test**

```python
"""
Integration test for tool hit recording
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from agent.runner import NiuRunner
from agent.handler import NiuHandler


@pytest.fixture
def temp_storage(tmp_path):
    """Use temporary directory for test storage"""
    original_home = Path.home()
    temp_home = tmp_path / "home"
    temp_home.mkdir(parents=True)

    original_home_method = Path.home
    Path.home = lambda: temp_home

    yield temp_home

    Path.home = original_home_method


def test_tool_hit_on_execution(temp_storage):
    """Test that tool is hit when actually executed, not on vector search"""
    llm_config = {
        "apikey": "test-key",
        "model": "test-model",
        "apibase": "http://test.com",
    }

    runner = NiuRunner(llm_config=llm_config, mcp_client=None)
    handler = NiuHandler(mcp_client=None)

    # Mock tool registry to return a mock function
    from agent import tool_registry
    mock_func = Mock(return_value={"status": "success", "data": "test"})
    tool_registry._registry["test-server/test-tool"] = mock_func

    # Execute tool via dispatch
    import json
    from agent.generic.agent_loop import StepOutcome

    gen = handler.dispatch("test-server/test-tool", {"arg": "value"}, Mock())
    result = list(gen)

    # Verify tool was hit
    assert runner.tool_lifecycle.get_tool_score("test-server/test-tool") == 100

    # Verify persistence
    scores_file = temp_storage / ".niu" / "tool_scores.json"
    import json
    scores = json.loads(scores_file.read_text(encoding="utf-8"))
    assert scores["test-server/test-tool"] == 100


def test_no_hit_on_vector_search(temp_storage):
    """Test that vector search does NOT trigger hit recording"""
    llm_config = {
        "apikey": "test-key",
        "model": "test-model",
        "apibase": "http://test.com",
    }

    runner = NiuRunner(llm_config=llm_config, mcp_client=None)

    # Mock vector search to return a tool
    with patch.object(runner.vector_search, 'search') as mock_search:
        mock_search.return_value = [
            Mock(
                metadata={"name": "test-tool", "server": "test-server", "category": "mcp_tool"},
                score=0.85
            )
        ]

        # Call _inject_dynamic_resources
        runner._inject_dynamic_resources("test query")

    # Verify tool was NOT hit (because it wasn't executed)
    assert runner.tool_lifecycle.get_tool_score("test-server/test-tool") == 0
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_tool_hit_integration.py -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_tool_hit_integration.py
git commit -m "test: add integration tests for tool hit recording

- Test hit on actual execution
- Test no hit on vector search only
- Verify persistence works correctly"
```

---

## Self-Review

**1. Spec Coverage:**
- ✅ MCP 工具命中位置修复 — Task 2
- ✅ 分数持久化 — Task 1
- ✅ 命中=100分 — Task 1
- ✅ 每次减10分 — Task 1
- ✅ Skills 使用指引 — Task 3

**2. Placeholder Scan:**
- ✅ No TBD, TODO, or "implement later"
- ✅ All code steps show actual implementation
- ✅ All test code is complete

**3. Type Consistency:**
- ✅ `hit_tool(tool_name: str)` — used consistently
- ✅ `decay_tools()` — no parameters, used consistently
- ✅ File paths use consistent format

**No gaps found.**

---

## Summary

这个方案只做必要的修改：

1. **修复核心bug**：工具命中记录从向量检索移到真正执行时
2. **添加持久化**：工具分数保存到 `~/.niu/tool_scores.json`
3. **强化指引**：告诉 LLM 看到技能摘要后主动读取文件

不需要 SkillLifecycleManager，因为 Skills 文件内容会通过工具调用自动保留在上下文中。
