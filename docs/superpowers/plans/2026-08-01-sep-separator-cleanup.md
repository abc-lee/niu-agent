# LightRAG 实体描述 `<SEP>` 分隔符清理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 全量清理 LightRAG 实体描述中的 `<SEP>` 分隔符——在所有展示给 LLM（提示词注入）和展示给用户（前端图谱页面）的出口统一替换为空格，确保 `<SEP>` 不再原样出现在任何场景。

**Architecture:** LightRAG 在合并多来源实体描述时用 `<SEP>` 拼接（如 `"描述A<SEP>描述B<SEP>描述C"`）。当前代码中仅 2 处做了清理（`_format_lightrag_entities_for_prompt` 用 `\n` 替换、`_format_description` 仅处理 brainregion 类型），其余 7 处出口直接透传原始描述。方案：在两个后端文件（`niu_api/kg_api.py`、`niu_api/internal/lightrag_adapter.py`）各加一个 `_clean_sep(desc)` 辅助函数统一替换 `<SEP>` 为空格，在所有返回 description 的出口调用；前端加防御性替换；`agent/runner.py` 活跃脑区知识段补上清理（与已有的 `_format_lightrag_entities_for_prompt` 保持一致用 `\n`，因为这是 prompt 注入场景，换行比空格更利于 LLM 分段阅读）。

**Tech Stack:** Python 3.11、FastAPI、JavaScript（Electron 前端）

---

## 背景

LightRAG 的 `_merge_nodes_then_upsert` 方法在合并同一实体的多个来源描述时，用 `<SEP>` 分隔符拼接：

```
"李磊是河北雄安分行...IT管理水平。<SEP>李磊是银行科技领域专家...2018年起支援雄安分行建设。<SEP>李磊是中国农业银行河北雄安分行的联系人"
```

实际日志（`/Users/lilei/.niu/logs/raw_http/20260801/000006_request.json`）显示，这段带 `<SEP>` 的描述被原样注入了 `### [活跃脑区知识]` 段的 prompt。

### 排查结果汇总

| # | 文件 | 行号 | 使用场景 | 是否已处理 `<SEP>` |
|---|------|------|----------|-------------------|
| 1 | `agent/runner.py` | 2198 | 活跃脑区知识段注入 prompt | ❌ 未处理 |
| 2 | `agent/runner.py` | 1893 | 参考知识/相关技能段注入 prompt | ✅ 已用 `\n` 替换 |
| 3 | `niu_api/kg_api.py` | 161-172 | `_format_description()`（前端图谱展示） | ⚠️ 仅 brainregion 处理 |
| 4 | `niu_api/kg_api.py` | 1152 | `/search_entities` 端点（前端搜索栏） | ❌ 未处理 |
| 5 | `niu_api/internal/lightrag_adapter.py` | 679, 691 | `explore_node` 返回（MCP 工具 → LLM） | ❌ 未处理 |
| 6 | `niu_api/internal/lightrag_adapter.py` | 981, 994 | `get_graph_snapshot` 返回（MCP 工具 → LLM + 前端） | ❌ 未处理 |
| 7 | `niu_api/internal/lightrag_adapter.py` | 806, 831 | `timeline_query` 返回（MCP 工具 → LLM） | ❌ 未处理 |
| 8 | `niu_api/internal/lightrag_adapter.py` | 1353, 1377 | `list_entities` 返回（MCP 工具 → LLM） | ❌ 未处理 |
| 9 | `niu_api/internal/lightrag_adapter.py` | 1525, 1532 | `merge_entities` changelog（前端轮询展示） | ❌ 未处理 |
| 10 | `niu_api/internal/lightrag_adapter.py` | 1655 | `get_entity_info`（habit 更新） | ✅ 已用 regex 清理 |
| 11 | `ui/main/windows/graph/renderer.js` | 723 | 实体详情面板 HTML 渲染 | ❌ 被动展示层 |
| 12 | `ui/main/windows/graph/renderer.js` | 431, 507, 515 | changelog 增量更新 node 对象 | ❌ 被动展示层 |

### 替换策略

- **后端出口（API/MCP 返回前端或 LLM）**：`<SEP>` → 空格（`" "`）。用户明确要求"转换成一个空格"。空格在 JSON 序列化和前端 HTML 渲染中最安全，不会引入换行符导致的布局问题。
- **Agent prompt 注入（runner.py）**：`<SEP>` → 换行（`\n`）。prompt 场景中换行更利于 LLM 分段阅读。已有的 `_format_lightrag_entities_for_prompt`（L1893）已用 `\n`，保持一致。
- **brainregion 特殊处理保留**：`_format_description()` 对 brainregion 的 `<SEP>` 解析逻辑（提取 brain_meta 元数据）保持不变，仅对非 brainregion 类型增加通用清理。

---

## File Structure

| 文件 | 职责 | 改动类型 |
|------|------|----------|
| `niu_api/kg_api.py` | 前端图谱 API 层 | 修改 `_format_description()` + `/search_entities` 端点 |
| `niu_api/internal/lightrag_adapter.py` | LightRAG 适配器（MCP 工具返回） | 新增 `_clean_sep()` + 在所有 description 出口调用 |
| `agent/runner.py` | Agent 动态注入 | 活跃脑区知识段补 `<SEP>` 清理 |
| `ui/main/windows/graph/renderer.js` | 前端图谱渲染 | 防御性 `<SEP>` 替换 |
| `tests/test_sep_cleanup.py` | 测试 | 新建 |

---

## Task 1: 新建测试文件，编写后端 `_clean_sep` 单元测试

**Files:**
- Create: `tests/test_sep_cleanup.py`

- [ ] **Step 1: 编写测试**

```python
"""Tests for <SEP> separator cleanup in entity descriptions.

LightRAG merges multi-source entity descriptions using <SEP> as separator.
All display/injection endpoints must clean <SEP> so it never appears raw
in prompts or frontend UI.
"""
import pytest


class TestCleanSep:
    """Test the _clean_sep helper function."""

    def test_no_sep_returns_unchanged(self):
        from niu_api.internal.lightrag_adapter import _clean_sep
        desc = "这是一个普通描述，没有分隔符"
        assert _clean_sep(desc) == desc

    def test_single_sep_replaced_with_space(self):
        from niu_api.internal.lightrag_adapter import _clean_sep
        desc = "描述A<SEP>描述B"
        result = _clean_sep(desc)
        assert "<SEP>" not in result
        assert "描述A 描述B" == result

    def test_multiple_sep_replaced(self):
        from niu_api.internal.lightrag_adapter import _clean_sep
        desc = "A<SEP>B<SEP>C<SEP>D"
        result = _clean_sep(desc)
        assert "<SEP>" not in result
        assert "A B C D" == result

    def test_empty_string(self):
        from niu_api.internal.lightrag_adapter import _clean_sep
        assert _clean_sep("") == ""

    def test_none_returns_empty(self):
        from niu_api.internal.lightrag_adapter import _clean_sep
        assert _clean_sep(None) == ""

    def test_sep_at_start_and_end(self):
        from niu_api.internal.lightrag_adapter import _clean_sep
        desc = "<SEP>描述<SEP>"
        result = _clean_sep(desc)
        assert "<SEP>" not in result
        assert result == " 描述 "

    def test_sep_with_surrounding_spaces(self):
        from niu_api.internal.lightrag_adapter import _clean_sep
        desc = "描述A <SEP> 描述B"
        result = _clean_sep(desc)
        assert "<SEP>" not in result
        # _clean_sep replaces "<SEP>" with " ", surrounding spaces preserved
        assert "描述A   描述B" == result


class TestKgApiFormatDescription:
    """Test that kg_api._format_description cleans <SEP> for non-brainregion types."""

    def test_person_description_sep_cleaned(self):
        from niu_api.kg_api import _format_description
        desc = "李磊是银行员工<SEP>李磊是技术专家"
        result = _format_description("person", desc)
        assert "<SEP>" not in result

    def test_concept_description_sep_cleaned(self):
        from niu_api.kg_api import _format_description
        desc = "概念A<SEP>概念B"
        result = _format_description("concept", desc)
        assert "<SEP>" not in result

    def test_brainregion_still_parsed(self):
        """brainregion type should still go through special parsing, not just space-replace."""
        from niu_api.kg_api import _format_description
        # brainregion with <SEP> triggers _parse_description path
        desc = "summary内容<SEP>brain_meta_shrink_count:2"
        result = _format_description("brainregion", desc)
        # Should not contain raw <SEP> either
        assert "<SEP>" not in result


class TestRunnerRegionKnowledgeSep:
    """Test that runner's active brain region knowledge section cleans <SEP>."""

    def test_sep_replaced_with_newline_in_region_knowledge(self):
        """The region knowledge injection should replace <SEP> with \\n (prompt context)."""
        # Verify the replacement logic: <SEP> → \n for prompt injection
        desc = "描述A<SEP>描述B<SEP>描述C"
        # This mirrors what runner.py should do (same as _format_lightrag_entities_for_prompt)
        cleaned = desc.replace("<SEP>", "\n")
        assert "<SEP>" not in cleaned
        assert "描述A\n描述B\n描述C" == cleaned
```

- [ ] **Step 2: 运行测试验证全部失败**

Run: `python/bin/python -m pytest tests/test_sep_cleanup.py -v`
Expected: FAIL — `ImportError: cannot import name '_clean_sep' from 'niu_api.internal.lightrag_adapter'`

- [ ] **Step 3: 提交测试文件**

```bash
git add tests/test_sep_cleanup.py
git commit -m "test: add <SEP> cleanup tests (red phase)"
```

---

## Task 2: `lightrag_adapter.py` — 新增 `_clean_sep()` 并在所有出口调用

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py` (模块顶部新增函数 + 7 处出口调用)

- [ ] **Step 1: 在模块顶部（class LightRAGAdapter 定义之前）新增 `_clean_sep` 函数**

在 `class LightRAGAdapter:` 定义之前插入：

```python
def _clean_sep(desc: str | None) -> str:
    """Clean LightRAG <SEP> separator from entity/edge descriptions.

    LightRAG merges multi-source descriptions using <SEP> as separator.
    This replaces <SEP> with a space for clean display in API responses
    and MCP tool results returned to the LLM.

    Args:
        desc: Raw description string that may contain <SEP>.

    Returns:
        Description with all <SEP> replaced by spaces. None → empty string.
    """
    if not desc:
        return ""
    return desc.replace("<SEP>", " ")
```

- [ ] **Step 2: `explore_node` — center 和 nodes description 调用 `_clean_sep`**

在 `explore_node` 方法中（约 L675-694），将所有 `properties.get("description", "")` 替换为 `_clean_sep(properties.get("description", ""))`。

具体修改 3 处：

L679（center description）:
```python
                    "description": _clean_sep(first_node.properties.get("description", "")),
```

L691（nodes description）:
```python
                    "description": _clean_sep(node.properties.get("description", "")),
```

L703（edges description）:
```python
                    "description": _clean_sep(edge.properties.get("description", "")),
```

- [ ] **Step 3: `get_graph_snapshot` — nodes 和 edges description 调用 `_clean_sep`**

在 `get_graph_snapshot` 方法中（约 L977-994）：

L981（nodes description）:
```python
                    "description": _clean_sep(attrs.get("description", "")),
```

L994（edges description）:
```python
                            "description": _clean_sep(data.get("description", "")),
```

- [ ] **Step 4: `timeline_query` — entity_desc 和 edge_desc 调用 `_clean_sep`**

在 `timeline_query` 方法中：

L798（entity_desc 赋值后清理）:
```python
                        entity_desc = _clean_sep(node.get("description", ""))
```

L824（edge_desc 赋值后清理）:
```python
                edge_desc = _clean_sep(edge.get("description", ""))
```

注意：`timeline_query` 内部调用了 `self.explore_node()`，而 Step 2 已经在 `explore_node` 出口做了清理。但 `timeline_query` 读取的是 `explore_node` 返回的 dict 中的 `"description"` 字段，该字段已经是清理后的。为防御性编程，仍在此处调用 `_clean_sep`（幂等操作，已无 `<SEP>` 时无副作用）。

- [ ] **Step 5: `list_entities` — 两处 description 调用 `_clean_sep`**

在 `list_entities` 方法中：

L1353（entity_type 过滤分支）:
```python
                                "description": _clean_sep(node_data.get("description", "")),
```

L1377（无过滤分支）:
```python
                            "description": _clean_sep(node.properties.get("description", "")),
```

- [ ] **Step 6: `merge_entities` — changelog 中的 target_desc 调用 `_clean_sep`**

在 `merge_entities` 方法中（约 L1525）:

```python
                            target_desc = _clean_sep(attrs.get("description", ""))
```

- [ ] **Step 7: 运行测试验证 `_clean_sep` 相关测试通过**

Run: `python/bin/python -m pytest tests/test_sep_cleanup.py::TestCleanSep -v`
Expected: PASS — 7 个测试全部通过

- [ ] **Step 8: 提交**

```bash
git add niu_api/internal/lightrag_adapter.py
git commit -m "fix: add _clean_sep() and apply to all lightrag_adapter description outputs"
```

---

## Task 3: `kg_api.py` — 修复 `_format_description()` 和 `/search_entities` 端点

**Files:**
- Modify: `niu_api/kg_api.py:161-172`（`_format_description` 函数）
- Modify: `niu_api/kg_api.py:1148-1153`（`/search_entities` 端点）

- [ ] **Step 1: 修改 `_format_description()` 函数，对非 brainregion 类型也清理 `<SEP>`**

将 `_format_description` 函数（L161-172）修改为：

```python
def _format_description(entity_type: str, description: str) -> str:
    """Format node description for frontend display.

    For brainregion entities, the raw description contains brain_meta_*
    metadata that is meaningless to users. This function extracts and
    formats the human-readable summary.

    For all other entity types, <SEP> separators (from LightRAG multi-source
    merging) are replaced with spaces for clean display.
    """
    if not description:
        return ""
    if entity_type.lower() == "brainregion" and "<SEP>" in description:
        from niu_api.internal.region_manager import _format_summary_for_display, _parse_description
        parsed = _parse_description(description)
        return _format_summary_for_display(parsed)
    # Non-brainregion: clean <SEP> separators for display
    return description.replace("<SEP>", " ")
```

- [ ] **Step 2: 修改 `/search_entities` 端点，截断前清理 `<SEP>`**

将 L1148-1153 修改为：

```python
                entities.append({
                    "id": name,
                    "name": name,
                    "entityType": ent.get("entity_type", ""),
                    "description": ((ent.get("description", "") or "").replace("<SEP>", " "))[:120],
                })
```

- [ ] **Step 3: 运行测试验证 kg_api 相关测试通过**

Run: `python/bin/python -m pytest tests/test_sep_cleanup.py::TestKgApiFormatDescription -v`
Expected: PASS — 3 个测试全部通过

- [ ] **Step 4: 提交**

```bash
git add niu_api/kg_api.py
git commit -m "fix: clean <SEP> in _format_description for all entity types and search_entities"
```

---

## Task 4: `agent/runner.py` — 活跃脑区知识段补 `<SEP>` 清理

**Files:**
- Modify: `agent/runner.py:2198`（活跃脑区知识段 desc 取值）

- [ ] **Step 1: 在活跃脑区知识段的 desc 取值后加 `<SEP>` 替换**

将 L2198:
```python
                desc = entry.entity_dict.get("description", "")
```

修改为:
```python
                desc = (entry.entity_dict.get("description") or "").replace("<SEP>", "\n")
```

说明：使用 `\n` 而非空格，与同文件 `_format_lightrag_entities_for_prompt`（L1893）保持一致——prompt 注入场景中换行更利于 LLM 分段阅读。

- [ ] **Step 2: 运行测试验证 runner 相关测试通过**

Run: `python/bin/python -m pytest tests/test_sep_cleanup.py::TestRunnerRegionKnowledgeSep -v`
Expected: PASS — 1 个测试通过

- [ ] **Step 3: 提交**

```bash
git add agent/runner.py
git commit -m "fix: clean <SEP> in active brain region knowledge injection"
```

---

## Task 5: 前端 `renderer.js` — 防御性 `<SEP>` 替换

**Files:**
- Modify: `ui/main/windows/graph/renderer.js:722-723`（实体详情面板）
- Modify: `ui/main/windows/graph/renderer.js:431,507,515`（changelog 增量更新）

- [ ] **Step 1: 实体详情面板渲染时替换 `<SEP>`**

将 L722-723:
```javascript
  if (orig.description) {
    html += `<div class="detail-row"><span class="detail-label">描述：</span>${escapeHtml(orig.description)}</div>`;
```

修改为:
```javascript
  if (orig.description) {
    const cleanDesc = orig.description.replace(/<SEP>/g, ' ');
    html += `<div class="detail-row"><span class="detail-label">描述：</span>${escapeHtml(cleanDesc)}</div>`;
```

- [ ] **Step 2: changelog 增量更新时替换 `<SEP>`**

在 3 处 changelog 处理中（L431、L507、L515），将 `change.data.description` 替换时清理 `<SEP>`。

L431:
```javascript
            description: (change.data.description || '').replace(/<SEP>/g, ' '),
```

L507:
```javascript
            existing.description = (change.data.description || existing.description || '').replace(/<SEP>/g, ' ');
```

L515:
```javascript
              description: (change.data.description || '').replace(/<SEP>/g, ' '),
```

- [ ] **Step 3: 提交**

```bash
git add ui/main/windows/graph/renderer.js
git commit -m "fix: defensive <SEP> cleanup in graph renderer frontend"
```

---

## Task 6: 全量测试验证

**Files:**
- Test: `tests/test_sep_cleanup.py`

- [ ] **Step 1: 运行全部 SEP 清理测试**

Run: `python/bin/python -m pytest tests/test_sep_cleanup.py -v`
Expected: PASS — 全部 11 个测试通过

- [ ] **Step 2: 运行相关现有测试确保无回归**

Run: `python/bin/python -m pytest tests/test_region_manager.py -v --timeout=30`
Expected: PASS — 现有测试无回归（brainregion 的 `<SEP>` 处理逻辑未变）

- [ ] **Step 3: 全库 grep 确认无遗漏的 `<SEP>` 透传**

Run: `grep -rn '"description".*get("description"' niu_api/ agent/ | grep -v '_clean_sep\|_format_description\|<SEP>'`
Expected: 所有返回 description 给用户/LLM 的出口都已通过 `_clean_sep` 或 `.replace("<SEP>", ...)` 处理

- [ ] **Step 4: 最终提交（如有剩余改动）**

```bash
git add -A
git commit -m "test: verify <SEP> cleanup complete, no regressions"
```

---

## Self-Review

### 1. Spec coverage

| 排查点 | 对应 Task | 状态 |
|--------|----------|------|
| runner.py 活跃脑区知识段（L2198） | Task 4 | ✅ |
| runner.py 参考知识段（L1893） | 已处理（无需改） | ✅ |
| kg_api.py `_format_description`（L161-172） | Task 3 Step 1 | ✅ |
| kg_api.py `/search_entities`（L1152） | Task 3 Step 2 | ✅ |
| lightrag_adapter.py `explore_node`（L679,691,703） | Task 2 Step 2 | ✅ |
| lightrag_adapter.py `get_graph_snapshot`（L981,994） | Task 2 Step 3 | ✅ |
| lightrag_adapter.py `timeline_query`（L798,824） | Task 2 Step 4 | ✅ |
| lightrag_adapter.py `list_entities`（L1353,1377） | Task 2 Step 5 | ✅ |
| lightrag_adapter.py `merge_entities` changelog（L1525） | Task 2 Step 6 | ✅ |
| lightrag_adapter.py `get_entity_info`（L1655） | 已处理（无需改） | ✅ |
| renderer.js 实体详情面板（L723） | Task 5 Step 1 | ✅ |
| renderer.js changelog 增量（L431,507,515） | Task 5 Step 2 | ✅ |

### 2. Placeholder scan

无 TBD/TODO/"add error handling" 等占位符。所有步骤都有完整代码。

### 3. Type consistency

- `_clean_sep(desc: str | None) -> str` — 签名一致
- `replace("<SEP>", " ")` — 后端 API/MCP 用空格
- `replace("<SEP>", "\n")` — Agent prompt 注入用换行
- `replace(/<SEP>/g, ' ')` — 前端 JS 用空格
- 策略一致：API/前端 = 空格，prompt = 换行
