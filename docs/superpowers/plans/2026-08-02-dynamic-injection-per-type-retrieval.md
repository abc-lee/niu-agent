# 动态注入按类型独立检索 — 修复 Skill 被 Knowledge 淹没问题

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将动态注入的向量检索从「全量 top-k 再分桶」改为「按类型各自独立检索」，确保 skill/interactionhabit 不被 knowledge 实体淹没。

**Tech Stack:** Python 3.11+, pytest, niu_api/internal/lightrag_adapter.py, agent/runner.py, agent/decay_pool.py
**Architecture:** 当前 `_inject_dynamic_resources` 调用一次 `search_multi_lightrag(top_k=10)` 取所有类型实体再分桶，导致 skill 排不进 top 10。改为：skill 用 `filter_lambda` 按 `file_path` 预过滤后独立 top-k 检索；knowledge/interactionhabit 仍走 `search_multi_lightrag` 全量检索（从结果中去除已由 skill 检索获取的实体去重）。同时修复衰减池 category 粘性问题和 description 截断不一致问题。
---

## 背景

### 问题根因

`_inject_dynamic_resources`（runner.py L2114）调用 `search_multi_lightrag(top_k=10)` 一次检索所有类型实体，然后 `_categorize_results` 按 `entity_type` 分桶。当 knowledge 实体数量远多于 skill 且语义距离更近时，top 10 全被 knowledge 占据，skill 桶为空。

### filter_lambda 限制

LightRAG 的 `filter_lambda` 在 nano_vectordb 中是**预过滤**（先过滤候选矩阵再 top-k），但 `entity_type` 不在 vdb 的 `meta_fields` 中（只有 `entity_name/source_id/content/file_path`），无法直接按 entity_type 预过滤。

**可行方案**：`file_path` 在 meta_fields 中，SkillSync 注入的 skill 有 `file_path='skill_sync'`。用 `filter_lambda` 按 `file_path` 包含 `skill_sync` 预过滤，实现 skill 专属检索。

### 已有但未用的方法

`search_skills`（lightrag_adapter.py L545-564）和 `search_interaction_habits`（L611-636）已定义但从未被调用，且它们用 `filter_by_entity_type` 做**后过滤**（先 top-k 再过滤），即使被调用也无法解决问题。

---

## 文件结构

| 文件 | 责任 | 操作 |
|------|------|------|
| `niu_api/internal/lightrag_adapter.py` | LightRAG 适配器，检索方法 | 修改 |
| `agent/runner.py` | `_inject_dynamic_resources` 注入逻辑 | 修改 |
| `agent/decay_pool.py` | 衰减池 | 修改 |
| `tests/test_dynamic_injection.py` | 新增测试 | 新建 |

---

### Task 1: 新增 `search_by_file_path` 方法（预过滤检索）

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py`（在 `search_skills` 方法之后，约 L565 位置）
- Test: `tests/test_dynamic_injection.py`

- [ ] **Step 1: 创建测试文件，写失败测试**

```python
"""Tests for dynamic injection — per-type retrieval with filter_lambda."""

from unittest.mock import MagicMock, patch
from niu_api.internal.lightrag_adapter import LightRAGAdapter


class TestSearchByFilePath:
    """Test search_by_file_path — pre-filter by file_path then top-k."""

    @patch.object(LightRAGAdapter, 'query_data')
    def test_filter_lambda_passed_to_query_data(self, mock_query):
        """filter_lambda must be passed to query_data for pre-filtering."""
        adapter = LightRAGAdapter.__new__(LightRAGAdapter)
        mock_query.return_value = {"data": {"entities": [], "relationships": [], "chunks": []}}

        adapter.search_by_file_path("test query", file_path_contains="skill_sync", top_k=10)

        call_kwargs = mock_query.call_args.kwargs
        assert "filter_lambda" in call_kwargs
        filter_fn = call_kwargs["filter_lambda"]
        assert callable(filter_fn)
        # Verify the filter function checks file_path
        assert filter_fn({"file_path": "skill_sync"}) is True
        assert filter_fn({"file_path": "some_doc.md"}) is False
        assert filter_fn({"file_path": None}) is False

    @patch.object(LightRAGAdapter, 'query_data')
    def test_returns_list_of_entities(self, mock_query):
        """Should return list of entity dicts, not categorized dict."""
        adapter = LightRAGAdapter.__new__(LightRAGAdapter)
        mock_query.return_value = {
            "data": {
                "entities": [
                    {"entity_name": "report-skill", "entity_type": "Skill", "file_path": "skill_sync"},
                    {"entity_name": "note-management", "entity_type": "Skill", "file_path": "skill_sync"},
                ],
                "relationships": [],
                "chunks": [],
            }
        }

        result = adapter.search_by_file_path("日志", file_path_contains="skill_sync", top_k=10)

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["entity_name"] == "report-skill"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python/bin/python -m pytest tests/test_dynamic_injection.py::TestSearchByFilePath -v`
Expected: FAIL with `AttributeError: 'LightRAGAdapter' object has no attribute 'search_by_file_path'`

- [ ] **Step 3: 实现 `search_by_file_path`**

在 `niu_api/internal/lightrag_adapter.py` 的 `search_skills` 方法之后（约 L565）插入：

```python
    def search_by_file_path(
        self,
        query: str,
        file_path_contains: str,
        top_k: int = 10,
        keywords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search entities with pre-filter on file_path via filter_lambda.

        Unlike search_skills (which post-filters), this method uses
        filter_lambda to filter at the vector search stage — achieving
        true 'filter-then-top-k' semantics.

        Used for skill retrieval where file_path contains 'skill_sync'
        (SkillSync-injected skills), ensuring skills are not drowned out
        by knowledge entities in global top-k.

        Args:
            query: Search query string.
            file_path_contains: Substring to match in entity's file_path field.
            top_k: Number of top results to retrieve (after filtering).
            keywords: Pre-provided keywords to skip LLM extraction.

        Returns:
            List of entity dicts matching the file_path filter.
        """
        def filter_fn(data: dict) -> bool:
            fp = data.get("file_path", "")
            return bool(fp) and file_path_contains in fp

        result = self.query_data(
            query, mode="local", top_k=top_k,
            keywords=keywords, filter_lambda=filter_fn,
        )
        if not result:
            return []

        data = result.get("data", {})
        if not data:
            data = result
        return data.get("entities", [])
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python/bin/python -m pytest tests/test_dynamic_injection.py::TestSearchByFilePath -v`
Expected: PASS (2 tests)

- [ ] **Step 5: 提交**

```bash
git add niu_api/internal/lightrag_adapter.py tests/test_dynamic_injection.py
git commit -m "feat: add search_by_file_path — pre-filter retrieval via filter_lambda"
```

---

### Task 2: 修改 `_inject_dynamic_resources` — skill 独立检索

**Files:**
- Modify: `agent/runner.py`（L2105-2160 区域）
- Test: `tests/test_dynamic_injection.py`

- [ ] **Step 1: 写失败测试 — skill 独立检索**

在 `tests/test_dynamic_injection.py` 中追加：

```python
class TestInjectDynamicResourcesSkillRetrieval:
    """Test that _inject_dynamic_resources retrieves skills independently."""

    def test_skill_retrieval_uses_search_by_file_path(self):
        """Skill retrieval must use search_by_file_path, not search_multi_lightrag."""
        from agent.runner import NiuRunner

        runner = NiuRunner.__new__(NiuRunner)
        runner._decay_pool = MagicMock()
        runner._decay_pool.decay = MagicMock()
        runner._decay_pool.inject = MagicMock()
        runner._decay_pool.get_top_by_category = MagicMock(return_value=[])
        runner._decay_pool.get_top_by_source = MagicMock(return_value=[])
        runner._brain_adapter = MagicMock()
        runner._brain_adapter.activate_for_query = MagicMock()
        runner._brain_adapter.format_region_map_only = MagicMock(return_value="")
        runner._format_running_subagents_section = MagicMock(return_value="")
        runner._get_brain_injector = MagicMock(return_value=None)
        runner._format_lightrag_entities_for_prompt = MagicMock(return_value=("", set()))
        runner._INJECT_ENTITY_TYPE_BLACKLIST = set()
        runner._INJECT_ENTITY_NAME_BLACKLIST = set()

        call_log = []

        def mock_search_multi(query, mode="local", top_k=20, keywords=None):
            call_log.append(("search_multi_lightrag", query, top_k))
            return {"skill": [], "knowledge": [], "interactionhabit": [], "other": []}

        def mock_search_by_fp(query, file_path_contains, top_k=10, keywords=None):
            call_log.append(("search_by_file_path", query, file_path_contains, top_k))
            return [{"entity_name": "report-skill", "entity_type": "Skill", "file_path": "skill_sync", "description": "test", "distance": 0.55}]

        runner._brain_adapter.search_multi_lightrag = mock_search_multi
        runner._brain_adapter.search_by_file_path = mock_search_by_fp
        runner._brain_adapter.search_interaction_habits = MagicMock(return_value=[])

        runner._inject_dynamic_resources("test context")

        # Verify search_by_file_path was called for skills
        skill_calls = [c for c in call_log if c[0] == "search_by_file_path"]
        assert len(skill_calls) == 1
        assert "skill_sync" in skill_calls[0][2]

        # Verify search_multi_lightrag was NOT called with the old all-in-one approach
        # (it should only be used for knowledge, not skills)
        multi_calls = [c for c in call_log if c[0] == "search_multi_lightrag"]
        # knowledge still uses search_multi_lightrag
        assert len(multi_calls) >= 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python/bin/python -m pytest tests/test_dynamic_injection.py::TestInjectDynamicResourcesSkillRetrieval -v`
Expected: FAIL

- [ ] **Step 3: 修改 `_inject_dynamic_resources` 的检索部分**

在 `agent/runner.py` 中，找到 `_inject_dynamic_resources` 的检索部分（约 L2105-2118）：

```python
        # 1. LightRAG 全局检索
        lightrag_results: dict[str, list[dict]] = {}
        adapter = None
        try:
            if self._brain_adapter is not None:
                adapter = self._brain_adapter
            else:
                from niu_api.internal.lightrag_adapter import LightRAGAdapter
                adapter = LightRAGAdapter()
            lightrag_results = adapter.search_multi_lightrag(
                context, mode="local", top_k=10, keywords=[context],
            )
        except Exception as e:
            logger.warning(f"LightRAG retrieval failed: {e}")
```

替换为：

```python
        # 1. LightRAG 检索 — 按类型独立检索，避免 skill 被 knowledge 淹没
        lightrag_results: dict[str, list[dict]] = {
            "skill": [], "knowledge": [], "interactionhabit": [], "other": [],
        }
        adapter = None
        if self._brain_adapter is not None:
            adapter = self._brain_adapter
        else:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            adapter = LightRAGAdapter()
        # 1a. Skill 专属检索：用 filter_lambda 按 file_path 预过滤，确保 skill 不被 knowledge 淹没
        #     独立 try 块：skill 检索失败不影响 knowledge 检索
        try:
            skill_results = adapter.search_by_file_path(
                context, file_path_contains="skill_sync", top_k=10, keywords=[context],
            )
            lightrag_results["skill"] = skill_results
        except Exception as e:
            logger.warning(f"LightRAG skill retrieval failed: {e}")
        # 1b. Knowledge 全量检索（interactionhabit 也从这里分桶）
        try:
            knowledge_results = adapter.search_multi_lightrag(
                context, mode="local", top_k=10, keywords=[context],
            )
            # 从 knowledge 结果中移除已由 skill 检索获取的实体（按 entity_name 去重）
            skill_names = {e.get("entity_name", "") for e in lightrag_results["skill"]}
            for cat, entities in knowledge_results.items():
                if cat == "skill":
                    continue  # skill 已由 search_by_file_path 独立检索，不用 search_multi_lightrag 的结果覆盖
                lightrag_results[cat] = [e for e in entities if e.get("entity_name", "") not in skill_names]
        except Exception as e:
            logger.warning(f"LightRAG knowledge retrieval failed: {e}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python/bin/python -m pytest tests/test_dynamic_injection.py::TestInjectDynamicResourcesSkillRetrieval -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent/runner.py tests/test_dynamic_injection.py
git commit -m "feat: skill retrieval uses independent search_by_file_path with filter_lambda"
```

---

### Task 3: 修改衰减池 `inject` — 修复 category 粘性问题

**Files:**
- Modify: `agent/decay_pool.py`（`inject` 方法）
- Test: `tests/test_dynamic_injection.py`

- [ ] **Step 1: 写失败测试 — category 纠正**

在 `tests/test_dynamic_injection.py` 中追加：

```python
class TestDecayPoolCategoryCorrection:
    """Test that inject() updates category when entity is re-injected with correct category."""

    def test_category_updated_when_lower_score(self):
        """Even with lower score, category must be updated if different."""
        from agent.decay_pool import DecayPool

        pool = DecayPool()
        # First inject as "knowledge" (wrong category)
        pool.inject(
            entity_name="report-skill",
            entity_dict={"entity_name": "report-skill", "entity_type": "Skill", "description": "test"},
            category="knowledge",
            source="vector",
            vector_score=0.8,
        )
        # Re-inject with correct category "skill" but lower score
        pool.inject(
            entity_name="report-skill",
            entity_dict={"entity_name": "report-skill", "entity_type": "Skill", "description": "test"},
            category="skill",
            source="vector",
            vector_score=0.5,
        )
        # Category should be "skill" now
        entry = pool._entries["report-skill"]
        assert entry.category == "skill"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python/bin/python -m pytest tests/test_dynamic_injection.py::TestDecayPoolCategoryCorrection -v`
Expected: FAIL — category stays "knowledge"

- [ ] **Step 3: 修改 `inject` 方法**

在 `agent/decay_pool.py` 中，找到 `inject` 方法。当前逻辑在 `existing.score > new_score` 时只更新 `entity_dict` 不更新 `category`/`source`。修改为：始终更新 `category` 和 `source`（即使分数更低）。

找到类似以下代码（`inject` 方法中保留高分的逻辑）：

```python
            if existing is not None and vector_score < existing.score:
                # 保留高分，只更新 entity_dict
                existing.entity_dict = entity_dict
                return
```

替换为：

```python
            if existing is not None and vector_score < existing.score:
                # 保留高分，但更新 entity_dict 和 category/source（纠正分类错误）
                existing.entity_dict = entity_dict
                existing.category = category
                existing.source = source
                return
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python/bin/python -m pytest tests/test_dynamic_injection.py::TestDecayPoolCategoryCorrection -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent/decay_pool.py tests/test_dynamic_injection.py
git commit -m "fix: decay_pool inject() always updates category/source to correct misclassification"
```

---

### Task 4: 修改图遍历起点 — 合并 skill hits

**Files:**
- Modify: `agent/runner.py`（L2123-2160 区域，`all_hits` 构造）
- Test: `tests/test_dynamic_injection.py`

- [ ] **Step 1: 写失败测试 — skill hits 进入图遍历起点**

在 `tests/test_dynamic_injection.py` 中追加：

```python
class TestGraphTraversalIncludesSkillHits:
    """Test that graph traversal starts from skill entities too, not just knowledge."""

    def test_skill_entity_names_in_all_hits(self):
        """Skill retrieval results must be included in all_hits for graph traversal."""
        from agent.runner import NiuRunner

        runner = NiuRunner.__new__(NiuRunner)
        runner._decay_pool = MagicMock()
        runner._decay_pool.decay = MagicMock()
        runner._decay_pool.inject = MagicMock()
        runner._decay_pool.get_top_by_category = MagicMock(return_value=[])
        runner._decay_pool.get_top_by_source = MagicMock(return_value=[])
        runner._brain_adapter = MagicMock()
        runner._brain_adapter.activate_for_query = MagicMock()
        runner._brain_adapter.format_region_map_only = MagicMock(return_value="")
        runner._format_running_subagents_section = MagicMock(return_value="")
        runner._get_brain_injector = MagicMock(return_value=None)
        runner._format_lightrag_entities_for_prompt = MagicMock(return_value=("", set()))
        runner._INJECT_ENTITY_TYPE_BLACKLIST = set()
        runner._INJECT_ENTITY_NAME_BLACKLIST = set()

        # Track what gets injected into decay_pool as "vector" source (all_hits)
        injected_names = []
        def track_inject(entity_name, entity_dict, category, source, vector_score):
            if source == "vector":
                injected_names.append(entity_name)
            runner._decay_pool.inject.return_value = None

        runner._decay_pool.inject = MagicMock(side_effect=track_inject)

        runner._brain_adapter.search_by_file_path = MagicMock(return_value=[
            {"entity_name": "report-skill", "entity_type": "Skill", "file_path": "skill_sync", "description": "test", "distance": 0.55},
        ])
        runner._brain_adapter.search_multi_lightrag = MagicMock(return_value={
            "skill": [],
            "knowledge": [{"entity_name": "work-log", "entity_type": "document", "description": "test", "distance": 0.7}],
            "interactionhabit": [],
            "other": [],
        })

        # Mock graph traversal to return empty
        runner._brain_adapter.get_graph_snapshot = MagicMock(return_value={"nodes": {}, "edges": []})

        runner._inject_dynamic_resources("test context")

        # report-skill should be in all_hits (injected as vector source)
        assert "report-skill" in injected_names
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python/bin/python -m pytest tests/test_dynamic_injection.py::TestGraphTraversalIncludesSkillHits -v`
Expected: FAIL

- [ ] **Step 3: 修改 `all_hits` 构造**

在 `agent/runner.py` 的 `_inject_dynamic_resources` 中，找到衰减池注入循环（约 L2123-2160）。当前只遍历 `lightrag_results` 的所有 category。由于 Task 2 已将 skill 结果放入 `lightrag_results["skill"]`，skill 实体会自然进入 `all_hits`。

确认 L2125 的循环：

```python
        for category, entities in lightrag_results.items():
```

这个循环已经会遍历 `lightrag_results["skill"]`，所以 skill 命中会自动进入 `all_hits`。Task 2 去重循环已跳过 `cat == "skill"`（不会覆盖 `search_by_file_path` 结果），因此此测试在 Task 2 修复后应该自动通过。

- [ ] **Step 4: 运行测试确认通过**

Run: `python/bin/python -m pytest tests/test_dynamic_injection.py::TestGraphTraversalIncludesSkillHits -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_dynamic_injection.py
git commit -m "test: verify skill hits enter graph traversal via all_hits"
```

---

### Task 5: 修复 description 截断不一致

**Files:**
- Modify: `agent/runner.py`（`_format_lightrag_entities_for_prompt` 方法 L1969 附近 + 活跃脑区知识 L2282）

- [ ] **Step 1: 找到截断位置**

Run: `grep -n "description\[:500\]\|desc\[:200\]" agent/runner.py`
确认两个截断位置：
- L1969 附近：`_format_lightrag_entities_for_prompt` 中 `description[:500]`
- L2282 附近：活跃脑区知识中 `desc[:200]`

- [ ] **Step 2: 统一截断到 500 字符**

在 `agent/runner.py` 中，找到活跃脑区知识的截断行（约 L2282）：

```python
                desc_line = f"   {desc[:200]}" if desc else ""
```

替换为：

```python
                desc_line = f"   {desc[:500]}" if desc else ""
```

- [ ] **Step 3: 运行全部测试确认无回归**

Run: `python/bin/python -m pytest tests/test_dynamic_injection.py tests/test_protect_range.py tests/test_sep_cleanup.py tests/test_dream_split.py -v`
Expected: PASS (all)

- [ ] **Step 4: 提交**

```bash
git add agent/runner.py
git commit -m "fix: unify description truncation to 500 chars across all injection sections"
```

---

### Task 6: 全量测试 + 验证

- [ ] **Step 1: 运行全部测试**

Run: `python/bin/python -m pytest tests/ -v`
Expected: PASS (all)

- [ ] **Step 2: ruff 检查**

Run: `cd agent && python/bin/ruff check . && cd ..`
Run: `python/bin/ruff check niu_api/internal/lightrag_adapter.py`
Expected: No errors

- [ ] **Step 3: 语法检查**

Run: `python/bin/python -c "import ast; ast.parse(open('agent/runner.py').read()); ast.parse(open('niu_api/internal/lightrag_adapter.py').read()); ast.parse(open('agent/decay_pool.py').read()); print('syntax ok')"`

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "test: all tests pass for dynamic injection per-type retrieval"
```

---

## Review Checklist

- [ ] skill 检索使用 `search_by_file_path` + `filter_lambda`（预过滤 `file_path` 包含 `skill_sync`）
- [ ] knowledge 检索仍用 `search_multi_lightrag`（全量 top-k）
- [ ] skill/knowledge 结果去重（按 entity_name）
- [ ] skill hits 进入 `all_hits` → 图遍历从 skill 实体出发
- [ ] 衰减池 `inject()` 始终更新 category/source（修复粘性）
- [ ] description 截断统一为 500 字符
- [ ] 所有测试通过
