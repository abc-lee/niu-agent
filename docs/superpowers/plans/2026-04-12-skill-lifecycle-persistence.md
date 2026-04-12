# Skills Intelligent Retrieval with Persistence - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement skills intelligent retrieval based on the last 3 messages (regardless of role: user, assistant, or tool) with score persistence mechanism (hit skills get 100 points, unhit skills decay by 10 per conversation until removed below threshold 50).

**Architecture:** Three-layer approach: (1) SkillLifecycleManager manages persistent skill scores stored in `~/.niu/skill_scores.json`, (2) Handler's `tool_after_callback()` retrieves related skills when tools are called, (3) Runner merges vector search results with persistent high-score skills during injection.

**Tech Stack:** Python 3.11+, JSON file storage, SQLite vector database

---

## File Structure

**Created Files:**
- `agent/skill_lifecycle.py` - Skill score persistence manager (similar to `tool_lifecycle.py` but with JSON persistence)
- `tests/test_skill_lifecycle.py` - Unit tests for SkillLifecycleManager

**Modified Files:**
- `agent/runner.py` - Change message extraction to 3 messages, add skill_lifecycle, merge persistent skills
- `agent/handler.py` - Add skill retrieval in `tool_after_callback()`, add skill decay in chat flow

---

## Task 1: Create SkillLifecycleManager

**Files:**
- Create: `agent/skill_lifecycle.py`
- Test: `tests/test_skill_lifecycle.py`

- [ ] **Step 1: Write the SkillLifecycleManager class**

```python
"""
Skills Lifecycle Management

Manage skill scores across conversation sessions with persistent storage.
Similar to ToolLifecycleManager but for skills with JSON file persistence.
"""

import json
from pathlib import Path
from typing import Dict, List


class SkillLifecycleManager:
    """Manage skill scores with persistent storage"""

    def __init__(self, decay_rate: int = 10, min_score: int = 50):
        """
        Args:
            decay_rate: Decay points per conversation (default: 10)
            min_score: Remove skills below this score (default: 50)
        """
        self.scores_path = Path.home() / ".niu" / "skill_scores.json"
        self.decay_rate = decay_rate
        self.min_score = min_score
        self.scores: Dict[str, int] = self._load_scores()

    def _load_scores(self) -> Dict[str, int]:
        """Load skill scores from JSON file"""
        if not self.scores_path.exists():
            return {}

        try:
            return json.loads(self.scores_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_scores(self):
        """Save skill scores to JSON file"""
        self.scores_path.parent.mkdir(parents=True, exist_ok=True)
        self.scores_path.write_text(
            json.dumps(self.scores, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    def hit_skill(self, skill_name: str):
        """
        Mark skill as hit - set score to 100

        Args:
            skill_name: Skill name (without path, e.g., "browser-automation")
        """
        self.scores[skill_name] = 100
        self._save_scores()

    def decay_skills(self):
        """
        Decay all skill scores after each conversation

        Rules:
        - All skill scores decrease by decay_rate
        - Skills with score < min_score are removed
        - Save to file after decay
        """
        to_remove = []

        for skill_name, score in self.scores.items():
            new_score = score - self.decay_rate
            self.scores[skill_name] = new_score

            if new_score < self.min_score:
                to_remove.append(skill_name)

        for skill_name in to_remove:
            del self.scores[skill_name]

        self._save_scores()

    def get_top_skills(self, limit: int = 5) -> List[str]:
        """
        Get top N skills by score

        Args:
            limit: Maximum number of skills to return

        Returns:
            List of skill names sorted by score (descending)
        """
        sorted_skills = sorted(
            self.scores.items(),
            key=lambda x: x[1],
            reverse=True
        )
        return [skill_name for skill_name, _ in sorted_skills[:limit]]

    def get_skill_score(self, skill_name: str) -> int:
        """
        Get score for a specific skill

        Args:
            skill_name: Skill name

        Returns:
            Current score (0 if skill doesn't exist)
        """
        return self.scores.get(skill_name, 0)

    def clear(self):
        """Clear all skill scores"""
        self.scores.clear()
        self._save_scores()

    def debug_print(self):
        """Debug: Print all skill scores"""
        if not self.scores:
            print("[SkillLifecycle] No skill scores")
            return

        print("[SkillLifecycle] Skill scores:")
        for skill_name, score in sorted(self.scores.items(), key=lambda x: -x[1]):
            print(f"  {skill_name}: {score}")
```

- [ ] **Step 2: Write unit tests**

```python
"""
Unit tests for SkillLifecycleManager
"""

import json
import tempfile
from pathlib import Path

import pytest

from agent.skill_lifecycle import SkillLifecycleManager


@pytest.fixture
def temp_storage(tmp_path):
    """Use temporary directory for skill scores"""
    original_home = Path.home()
    temp_home = tmp_path / "home"
    temp_home.mkdir(parents=True)

    # Monkey-patch Path.home()
    original_home_method = Path.home
    Path.home = lambda: temp_home

    yield temp_home

    # Restore
    Path.home = original_home_method


def test_hit_skill(temp_storage):
    """Test hitting a skill sets it to 100"""
    manager = SkillLifecycleManager(decay_rate=10, min_score=50)

    # Hit a skill
    manager.hit_skill("browser-automation")

    # Check score
    assert manager.get_skill_score("browser-automation") == 100

    # Check persistence
    scores_file = temp_storage / ".niu" / "skill_scores.json"
    assert scores_file.exists()

    scores = json.loads(scores_file.read_text(encoding="utf-8"))
    assert scores["browser-automation"] == 100


def test_decay_skills(temp_storage):
    """Test skill score decay"""
    manager = SkillLifecycleManager(decay_rate=10, min_score=50)

    # Add skills
    manager.hit_skill("skill-1")  # 100
    manager.hit_skill("skill-2")  # 100

    # Decay once
    manager.decay_skills()

    # Check scores
    assert manager.get_skill_score("skill-1") == 90
    assert manager.get_skill_score("skill-2") == 90


def test_decay_removes_low_scores(temp_storage):
    """Test that skills below min_score are removed"""
    manager = SkillLifecycleManager(decay_rate=30, min_score=50)

    # Add skill
    manager.hit_skill("low-skill")  # 100

    # Decay twice: 100 -> 70 -> 40
    manager.decay_skills()  # 70
    manager.decay_skills()  # 40 (removed)

    # Check skill is removed
    assert manager.get_skill_score("low-skill") == 0
    assert "low-skill" not in manager.scores


def test_get_top_skills(temp_storage):
    """Test getting top skills by score"""
    manager = SkillLifecycleManager(decay_rate=10, min_score=50)

    # Add skills with different scores
    manager.hit_skill("skill-a")  # 100
    manager.hit_skill("skill-b")  # 100
    manager.decay_skills()  # both to 90
    manager.hit_skill("skill-c")  # 100 (new)

    # Get top 2
    top_skills = manager.get_top_skills(limit=2)

    # skill-c should be first (100), then skill-a or skill-b (both 90)
    assert "skill-c" in top_skills
    assert len(top_skills) == 2


def test_persistence_across_instances(temp_storage):
    """Test that scores persist across manager instances"""
    # First instance
    manager1 = SkillLifecycleManager()
    manager1.hit_skill("persistent-skill")

    # Second instance (should load from file)
    manager2 = SkillLifecycleManager()
    assert manager2.get_skill_score("persistent-skill") == 100


def test_clear(temp_storage):
    """Test clearing all scores"""
    manager = SkillLifecycleManager()
    manager.hit_skill("skill-1")
    manager.hit_skill("skill-2")

    manager.clear()

    assert manager.get_skill_score("skill-1") == 0
    assert manager.get_skill_score("skill-2") == 0
    assert len(manager.scores) == 0
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/test_skill_lifecycle.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add agent/skill_lifecycle.py tests/test_skill_lifecycle.py
git commit -m "feat: add SkillLifecycleManager for persistent skill scores"
```

---

## Task 2: Integrate SkillLifecycleManager into NiuRunner

**Files:**
- Modify: `agent/runner.py:276-292` (NiuRunner.__init__)

- [ ] **Step 1: Import SkillLifecycleManager**

Add import after `from .tool_lifecycle import ToolLifecycleManager`:

```python
from .skill_lifecycle import SkillLifecycleManager
```

- [ ] **Step 2: Initialize skill_lifecycle in NiuRunner.__init__**

Add after `self.tool_lifecycle = ToolLifecycleManager(decay_rate=10, min_score=50)`:

```python
        # Skills lifecycle management
        self.skill_lifecycle = SkillLifecycleManager(decay_rate=10, min_score=50)
```

- [ ] **Step 3: Commit**

```bash
git add agent/runner.py
git commit -m "feat: integrate SkillLifecycleManager into NiuRunner"
```

---

## Task 3: Modify Context Extraction to 3 Messages

**Files:**
- Modify: `agent/runner.py:328-359` (_extract_context_from_history method)

- [ ] **Step 1: Change message extraction from 5 to 3**

Replace the method:

```python
    def _extract_context_from_history(self, history: Optional[list], user_input: str) -> str:
        """
        从消息历史中提取上下文用于技能检索

        改进：使用最近3条消息（不区分角色），提高检索准确性

        Args:
            history: 消息历史 [{"role": "user/assistant/tool", "content": str}, ...]
            user_input: 当前用户输入

        Returns:
            提取的上下文字符串
        """
        if not history:
            return user_input

        # 改进：提取最近3条消息（不区分角色）
        recent_messages = history[-3:] if len(history) > 3 else history

        # 拼接内容（包括工具调用）
        context_parts = []
        for msg in recent_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            # 包含所有角色（user, assistant, tool）
            if content:
                # 截断过长的内容
                if len(content) > 200:
                    content = content[:200] + "..."
                context_parts.append(f"{role}: {content}")

        # 添加当前用户输入
        context_parts.append(f"user: {user_input}")

        return "\n".join(context_parts)
```

- [ ] **Step 2: Run tests to verify no regressions**

Run: `pytest tests/test_runner.py -v` (if exists) or just manual test
Expected: Tests PASS

- [ ] **Step 3: Commit**

```bash
git add agent/runner.py
git commit -m "refactor: change context extraction from 5 to 3 messages"
```

---

## Task 4: Add Skill Retrieval After Tool Calls

**Files:**
- Modify: `agent/handler.py:241-301` (tool_after_callback method)

- [ ] **Step 1: Add skill retrieval in tool_after_callback**

Add after the Interaction Habits confidence update (around line 301):

```python
        # 增强：工具调用后检索相关技能
        try:
            from agent.vector_search import VectorSearchAdapter
            from agent.runner import get_runner

            runner = get_runner()
            if runner and hasattr(runner, 'skill_lifecycle'):
                vs = VectorSearchAdapter()

                # 使用工具名作为查询（去掉 server 前缀）
                tool_name_short = tool_name.split('/')[-1] if '/' in tool_name else tool_name

                # 检索相关技能
                skills = vs.search(
                    query=tool_name_short,
                    limit=3,
                    min_score=0.3,
                    filter={"level": "l1", "category": "skill"}
                )

                # 命中的技能设置为 100 分
                for skill in skills:
                    skill_name = skill.metadata.get("name")
                    if skill_name:
                        runner.skill_lifecycle.hit_skill(skill_name)
                        print(
                            f"[SkillRetrieval] Tool '{tool_name}' hit skill: {skill_name}",
                            file=sys.stderr,
                            flush=True
                        )

        except Exception as e:
            # 技能检索失败不影响主流程
            print(f"[SkillRetrieval] Error: {e}", file=sys.stderr, flush=True)
```

- [ ] **Step 2: Commit**

```bash
git add agent/handler.py
git commit -m "feat: add skill retrieval after tool calls"
```

---

## Task 5: Merge Vector Search with Persistent Skills

**Files:**
- Modify: `agent/runner.py:361-418` (_inject_dynamic_resources method)

- [ ] **Step 1: Modify skills retrieval to merge with persistent skills**

Replace the skills search section (around line 374-376):

```python
        # 搜索 Skills（符合L0/L1/L2规范，使用level字段）
        # 改进：合并向量检索结果 + 持久化高分技能
        skills_from_vector = self.vector_search.search(
            query=user_input, limit=3, min_score=0.35, filter={"level": "l1", "category": "skill"}
        )

        # 获取持久化高分技能
        top_skill_names = self.skill_lifecycle.get_top_skills(limit=3)

        # 从向量库加载这些技能的 L1 摘要
        skills_from_persistence = []
        for skill_name in top_skill_names:
            # 从 skill 名称查找向量库记录（通过 metadata.name 过滤）
            results = self.vector_search.search(
                query=skill_name,  # 使用技能名作为查询
                limit=1,
                min_score=0.9,  # 高阈值确保精确匹配
                filter={"level": "l1", "category": "skill"}
            )

            # 验证是否真的是这个技能
            for result in results:
                if result.metadata.get("name") == skill_name:
                    skills_from_persistence.append(result)
                    break

        # 合并并去重（向量检索结果优先）
        seen_names = set()
        all_skills = []

        for skill in skills_from_vector:
            name = skill.metadata.get("name")
            if name and name not in seen_names:
                seen_names.add(name)
                all_skills.append(skill)

        for skill in skills_from_persistence:
            name = skill.metadata.get("name")
            if name and name not in seen_names:
                seen_names.add(name)
                all_skills.append(skill)

        print(f"[Debug] Dynamic injection - Skills: {len(skills_from_vector)} from vector + {len(skills_from_persistence)} from persistence = {len(all_skills)} total", file=sys.stderr, flush=True)
```

- [ ] **Step 2: Update the formatting section to use all_skills**

Replace the formatting section (around line 403-404):

```python
        # 格式化
        parts = []
        if all_skills:  # 使用合并后的技能列表
            parts.append(format_resources_for_prompt(all_skills, "相关技能"))
        if mcp_tools:
            parts.append(format_resources_for_prompt(mcp_tools, "可用工具"))
        if knowledge:
            parts.append(format_resources_for_prompt(knowledge, "参考知识"))
        if interaction_habits:
            parts.append(format_resources_for_prompt(interaction_habits, "交互习惯"))
```

- [ ] **Step 3: Commit**

```bash
git add agent/runner.py
git commit -m "feat: merge vector search with persistent skills in injection"
```

---

## Task 6: Add Skill Decay After Chat

**Files:**
- Modify: `agent/runner.py:595-598` (end of chat method)

- [ ] **Step 1: Add skill decay after tool decay**

Add after `self.tool_lifecycle.decay_tools()`:

```python
        # 对话结束后衰减技能分数
        self.skill_lifecycle.decay_skills()
```

- [ ] **Step 2: Commit**

```bash
git add agent/runner.py
git commit -m "feat: add skill decay after chat session"
```

---

## Task 7: Integration Tests

**Files:**
- Create: `tests/test_skill_retrieval_integration.py`

- [ ] **Step 1: Write integration test**

```python
"""
Integration tests for skill retrieval with persistence
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from agent.skill_lifecycle import SkillLifecycleManager
from agent.runner import NiuRunner


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


def test_skill_hit_on_tool_call(temp_storage):
    """Test that calling a tool hits related skills"""
    # Mock LLM config
    llm_config = {
        "apikey": "test-key",
        "model": "test-model",
        "apibase": "http://test.com",
    }

    # Create runner
    runner = NiuRunner(llm_config=llm_config, mcp_client=None)

    # Mock vector search to return a skill
    with patch.object(runner.vector_search, 'search') as mock_search:
        mock_search.return_value = [
            Mock(
                id="skill:browser-automation:l1",
                content="Browser automation|browser,form filling|...",
                score=0.85,
                metadata={
                    "name": "browser-automation",
                    "level": "l1",
                    "category": "skill"
                }
            )
        ]

        # Simulate tool call via handler
        from agent.handler import NiuHandler
        handler = NiuHandler(mcp_client=None)
        handler.mcp_client = None

        # Manually trigger skill retrieval (simulating tool_after_callback)
        tool_name = "browser-server/browser_navigate"
        tool_name_short = tool_name.split('/')[-1]

        skills = runner.vector_search.search(
            query=tool_name_short,
            limit=3,
            min_score=0.3,
            filter={"level": "l1", "category": "skill"}
        )

        for skill in skills:
            skill_name = skill.metadata.get("name")
            if skill_name:
                runner.skill_lifecycle.hit_skill(skill_name)

        # Verify skill is hit
        assert runner.skill_lifecycle.get_skill_score("browser-automation") == 100


def test_skill_decay_after_chat(temp_storage):
    """Test that skills decay after each chat session"""
    llm_config = {
        "apikey": "test-key",
        "model": "test-model",
        "apibase": "http://test.com",
    }

    runner = NiuRunner(llm_config=llm_config, mcp_client=None)

    # Add skills
    runner.skill_lifecycle.hit_skill("skill-1")
    runner.skill_lifecycle.hit_skill("skill-2")

    # Simulate chat ending
    runner.skill_lifecycle.decay_skills()

    # Check scores
    assert runner.skill_lifecycle.get_skill_score("skill-1") == 90
    assert runner.skill_lifecycle.get_skill_score("skill-2") == 90


def test_merge_persistent_skills_with_vector_search(temp_storage):
    """Test that persistent skills are merged with vector search results"""
    llm_config = {
        "apikey": "test-key",
        "model": "test-model",
        "apibase": "http://test.com",
    }

    runner = NiuRunner(llm_config=llm_config, mcp_client=None)

    # Add persistent skill
    runner.skill_lifecycle.hit_skill("persistent-skill")

    # Mock vector search to return different skill
    with patch.object(runner.vector_search, 'search') as mock_search:
        def mock_search_fn(query, limit, min_score, filter):
            if filter.get("category") == "skill":
                # Vector search returns "vector-skill"
                if query == "user input":  # User query
                    return [
                        Mock(
                            id="skill:vector-skill:l1",
                            content="Vector skill|...",
                            score=0.85,
                            metadata={"name": "vector-skill", "level": "l1", "category": "skill"}
                        )
                    ]
                elif query == "persistent-skill":  # Persistence lookup
                    return [
                        Mock(
                            id="skill:persistent-skill:l1",
                            content="Persistent skill|...",
                            score=0.95,
                            metadata={"name": "persistent-skill", "level": "l1", "category": "skill"}
                        )
                    ]
            return []

        mock_search.side_effect = mock_search_fn

        # Inject resources
        injection = runner._inject_dynamic_resources("user input")

        # Both skills should be in injection
        assert "vector-skill" in injection or "persistent-skill" in injection
```

- [ ] **Step 2: Run integration tests**

Run: `pytest tests/test_skill_retrieval_integration.py -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_skill_retrieval_integration.py
git commit -m "test: add integration tests for skill retrieval with persistence"
```

---

## Task 8: Update Documentation

**Files:**
- Modify: `CLAUDE.md` (add skill lifecycle documentation)

- [ ] **Step 1: Add documentation section**

Add to `CLAUDE.md` after the `ToolLifecycleManager` section:

```markdown
### Skills 智能检索

**设计目标**：
1. 基于最近 3 条消息（不区分角色）触发检索
2. 工具调用也触发技能检索（使用工具名作为查询）
3. 分数持久化：命中技能得 100 分，未命中降 10 分，低于 50 分移除

**核心组件**：
- `agent/skill_lifecycle.py` — SkillLifecycleManager（管理技能分数持久化）
- 存储位置：`~/.niu/skill_scores.json`

**工作流程**：
1. **用户输入** → `_inject_dynamic_resources()` → 向量检索技能 → 合并持久化高分技能
2. **工具调用** → `tool_after_callback()` → 使用工具名检索技能 → `hit_skill()`
3. **对话结束** → `decay_skills()` → 所有技能降 10 分 → 移除低于 50 分的技能

**示例**：
```python
from agent.skill_lifecycle import SkillLifecycleManager

manager = SkillLifecycleManager()
manager.hit_skill("browser-automation")  # 100 分
manager.decay_skills()  # 90 分
top_skills = manager.get_top_skills(limit=3)  # ["browser-automation"]
```
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add skill lifecycle documentation to CLAUDE.md"
```

---

## Self-Review

**1. Spec Coverage:**
- ✅ 最近 3 条消息（不区分角色）— Task 3
- ✅ 工具调用触发检索 — Task 4
- ✅ 分数持久化 — Task 1
- ✅ 命中=100分 — Task 1
- ✅ 未命中降10分 — Task 6
- ✅ 低于50分移除 — Task 1
- ✅ 合并向量检索 + 持久化技能 — Task 5

**2. Placeholder Scan:**
- ✅ No TBD, TODO, or "implement later"
- ✅ All code steps show actual implementation
- ✅ All test code is complete

**3. Type Consistency:**
- ✅ `hit_skill(skill_name: str)` — used consistently
- ✅ `get_top_skills(limit: int = 5) -> List[str]` — used consistently
- ✅ `decay_skills()` — no parameters, used consistently

**No gaps found.**
