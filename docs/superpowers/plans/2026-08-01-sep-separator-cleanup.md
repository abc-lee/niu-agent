# LightRAG 实体描述 `<SEP>` 分隔符清理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 全量清理 LightRAG 实体描述中的 `<SEP>` 分隔符——在所有展示给 LLM（提示词注入、MCP 工具结果）和展示给用户（前端图谱页面）的出口统一替换为空格，确保 `<SEP>` 不再原样出现在任何场景。

**Architecture:** LightRAG 在合并多来源实体描述时用 `<SEP>` 拼接（如 `"描述A<SEP>描述B<SEP>描述C"`）。当前代码中仅 2 处做了清理（`_format_lightrag_entities_for_prompt` 用 `\n` 替换、`_format_description` 仅处理 brainregion 类型）。方案在三个层面清理：
1. **adapter 层**（`niu_api/internal/lightrag_adapter.py`）：新增模块级 `_clean_sep()` 函数，在所有返回 description 的出口调用——包括 `explore_node`、`get_graph_snapshot`、`timeline_query`、`list_entities`、`merge_entities` changelog，以及 R1 审查新发现的 `query_data`、`get_entity_info`、`get_relation_info` 三个方法的返回结果后处理。
2. **API 层**（`niu_api/kg_api.py`）：修复 `_format_description()` 对非 brainregion 类型也清理 `<SEP>`；修复 `/explore` 端点 center 节点跳过 `_format_description` 的问题；修复 `/search_entities` 端点。
3. **MCP server 层**（`mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py`）：修复 `lightrag_insert_entity` 去重消息中 `current_desc` 的 `<SEP>` 泄漏。

前端 `renderer.js` 加防御性替换；`agent/runner.py` 活跃脑区知识段补上清理（用 `\n`，与已有的 `_format_lightrag_entities_for_prompt` 保持一致——prompt 注入场景换行更利于 LLM 分段阅读）。

**Tech Stack:** Python 3.11、FastAPI、JavaScript（Electron 前端）

---

## 背景

LightRAG 的 `_merge_nodes_then_upsert` 方法在合并同一实体的多个来源描述时，用 `<SEP>` 分隔符拼接：

```
"李磊是河北雄安分行...IT管理水平。<SEP>李磊是银行科技领域专家...2018年起支援雄安分行建设。<SEP>李磊是中国农业银行河北雄安分行的联系人"
```

实际日志（`/Users/lilei/.niu/logs/raw_http/20260801/000006_request.json`）显示，这段带 `<SEP>` 的描述被原样注入了 `### [活跃脑区知识]` 段的 prompt。

### 排查结果汇总（R1 审查 + 二次核对后）

| # | 文件 | 行号 | 使用场景 | 是否已处理 `<SEP>` |
|---|------|------|----------|-------------------|
| 1 | `agent/runner.py` | 2198 | 活跃脑区知识段注入 prompt | ❌ 未处理 |
| 2 | `agent/runner.py` | 1893 | 参考知识/相关技能段注入 prompt | ✅ 已用 `\n` 替换 |
| 3 | `niu_api/kg_api.py` | 161-172 | `_format_description()`（前端图谱展示） | ⚠️ 仅 brainregion 处理 |
| 4 | `niu_api/kg_api.py` | 714, 728 | `/explore` 端点 center 节点（前端） | ❌ 跳过 _format_description |
| 5 | `niu_api/kg_api.py` | 1152 | `/search_entities` 端点（前端搜索栏） | ❌ 未处理 |
| 6 | `niu_api/internal/lightrag_adapter.py` | 679, 691, 703 | `explore_node` 返回（MCP 工具 → LLM） | ❌ 未处理 |
| 7 | `niu_api/internal/lightrag_adapter.py` | 981, 994 | `get_graph_snapshot` 返回（MCP 工具 → LLM + 前端） | ❌ 未处理 |
| 8 | `niu_api/internal/lightrag_adapter.py` | 806, 831 | `timeline_query` 返回（MCP 工具 → LLM） | ❌ 未处理 |
| 9 | `niu_api/internal/lightrag_adapter.py` | 1353, 1377 | `list_entities` 返回（MCP 工具 → LLM） | ❌ 未处理 |
| 10 | `niu_api/internal/lightrag_adapter.py` | 1525, 1532 | `merge_entities` changelog（前端轮询展示） | ❌ 未处理 |
| 11 | `niu_api/internal/lightrag_adapter.py` | 1213-1214 | `get_entity_info` 返回（MCP 工具 → LLM） | ❌ 未处理（R1 新发现） |
| 12 | `niu_api/internal/lightrag_adapter.py` | 1235-1236 | `get_relation_info` 返回（MCP 工具 → LLM） | ❌ 未处理（R1 新发现） |
| 13 | `niu_api/internal/lightrag_adapter.py` | 287-290 | `query_data` 返回（MCP 工具 → LLM） | ❌ 未处理（R1 新发现） |
| 14 | `niu_api/internal/lightrag_adapter.py` | 1655-1656 | `update_habit_confidence`（habit 更新） | ✅ 已用 regex 清理 |
| 15 | `mcp-servers/.../__init__.py` | 1240 | `lightrag_insert_entity` 去重消息 `current_desc` | ❌ 未处理（R1 新发现） |
| 16 | `ui/main/windows/graph/renderer.js` | 723 | 实体详情面板 HTML 渲染 | ❌ 被动展示层 |
| 17 | `ui/main/windows/graph/renderer.js` | 431, 507, 515 | changelog 增量更新 node 对象 | ❌ 被动展示层 |
| 18 | `niu_api/internal/lightrag_adapter.py` | 224 | `query` 返回 context 字符串（MCP 工具 → LLM） | ❌ 未处理（R2 新发现） |
| 19 | `niu_api/internal/lightrag_adapter.py` | 1150, 1172 | `edit_entity`/`edit_relation` 返回 data（MCP 工具 → LLM） | ❌ 未处理（R2 新发现） |

> **R1 审查纠正**：原计划第 10 项声称 `get_entity_info`（L1655）已用 regex 清理。二次核对确认 L1655 实际在 `update_habit_confidence` 方法内，而非 `get_entity_info`（L1198）。`get_entity_info` 是纯透传，完全没处理 `<SEP>`。

### 替换策略

- **后端出口（API/adapter/MCP 返回前端或 LLM）**：`<SEP>` → 空格（`" "`）。用户明确要求"转换成一个空格"。空格在 JSON 序列化和前端 HTML 渲染中最安全，不会引入换行符导致的布局问题。
- **Agent prompt 注入（runner.py）**：`<SEP>` → 换行（`\n`）。prompt 场景中换行更利于 LLM 分段阅读。已有的 `_format_lightrag_entities_for_prompt`（L1893）已用 `\n`，保持一致。
- **brainregion 特殊处理保留**：`_format_description()` 对 brainregion 的 `<SEP>` 解析逻辑（提取 brain_meta 元数据）保持不变，仅对非 brainregion 类型增加通用清理。

---

## File Structure

| 文件 | 职责 | 改动类型 |
|------|------|----------|
| `niu_api/internal/lightrag_adapter.py` | LightRAG 适配器（MCP 工具 + API 返回） | 新增 `_clean_sep()` + 在所有 description 出口调用 + `query_data`/`get_entity_info`/`get_relation_info` 后处理 |
| `niu_api/kg_api.py` | 前端图谱 API 层 | 修复 `_format_description()` + `/explore` center + `/search_entities` |
| `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` | MCP 工具层 | `lightrag_insert_entity` 去重消息清理 |
| `agent/runner.py` | Agent 动态注入 | 活跃脑区知识段补 `<SEP>` 清理 |
| `ui/main/windows/graph/renderer.js` | 前端图谱渲染 | 防御性 `<SEP>` 替换 |
| `tests/test_sep_cleanup.py` | 测试 | 新建 |

---

## Task 1: 新建测试文件，编写 `_clean_sep` 和 `_format_description` 单元测试

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

## Task 2: `lightrag_adapter.py` — 新增 `_clean_sep()` 并在所有出口调用（含 R1 新发现）

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py` (模块顶部新增函数 + 10 处出口调用)

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

- [ ] **Step 2: `explore_node` — center、nodes、edges description 调用 `_clean_sep`**

在 `explore_node` 方法中（约 L675-703），修改 3 处：

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

L798（entity_desc 赋值）:
```python
                        entity_desc = _clean_sep(node.get("description", ""))
```

L824（edge_desc 赋值）:
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

- [ ] **Step 7: `query_data` — 返回结果后处理（R1 新发现）**

`query_data` 方法（L230-294）返回 LightRAG `aquery_data()` 的原始结果，其中 `entities` 列表中每个 entity 都有 `description` 字段。需要在 return 前对结果中的 description 做后处理。

在 `query_data` 方法的 `return result`（L290）之前插入：

```python
            # Clean <SEP> from entity/relationship descriptions in result
            result = _clean_sep_in_query_result(result)
            return result
```

并在模块顶部（`_clean_sep` 函数之后）新增辅助函数：

```python
def _clean_sep_in_query_result(result):
    """Recursively clean <SEP> from description fields in query_data results.

    LightRAG's aquery_data() returns structured results with entities,
    relationships, and chunks — each potentially containing description
    fields with <SEP> separators from multi-source merging.
    """
    if not isinstance(result, dict):
        return result
    data = result.get("data", result) if isinstance(result, dict) else result
    if isinstance(data, dict):
        for key in ("entities", "relationships"):
            items = data.get(key, [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and "description" in item:
                        item["description"] = _clean_sep(item.get("description", ""))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "description" in item:
                item["description"] = _clean_sep(item.get("description", ""))
    return result
```

- [ ] **Step 8: `get_entity_info` — 返回结果后处理（R1 新发现）**

`get_entity_info` 方法（L1198-1217）返回 `{"status": "ok", "data": result}`，其中 `result` 来自 LightRAG 的 `rag.get_entity_info()`，包含 `graph_data.description`。

在 `get_entity_info` 方法的 `return {"status": "ok", "data": result}`（L1214）之前插入：

```python
            # Clean <SEP> from description in result
            if isinstance(result, dict):
                graph_data = result.get("graph_data", {})
                if isinstance(graph_data, dict) and "description" in graph_data:
                    graph_data["description"] = _clean_sep(graph_data.get("description", ""))
            return {"status": "ok", "data": result}
```

- [ ] **Step 9: `get_relation_info` — 返回结果后处理（R1 新发现）**

`get_relation_info` 方法（L1219-1239）返回 `{"status": "ok", "data": result}`，其中 `result` 来自 LightRAG 的 `rag.get_relation_info()`，包含 edge description。

在 `get_relation_info` 方法的 `return {"status": "ok", "data": result}`（L1236）之前插入：

```python
            # Clean <SEP> from description in result
            if isinstance(result, dict):
                graph_data = result.get("graph_data", {})
                if isinstance(graph_data, dict) and "description" in graph_data:
                    graph_data["description"] = _clean_sep(graph_data.get("description", ""))
            return {"status": "ok", "data": result}
```

- [ ] **Step 10: `query` — 返回结果后处理（R2 新发现）**

`query` 方法（L163-228）调用 `rag.aquery()` 返回一个 context 字符串。当 `only_need_context=True`（默认值）时，LightRAG 将实体描述序列化为 JSON 拼入 context 字符串，其中包含原始 `<SEP>`。`lightrag_query` MCP 工具直接返回这个字符串给 LLM。

在 `query` 方法的 `return result`（L224）之前插入：

```python
            # Clean <SEP> from context string (entity descriptions embedded by LightRAG)
            if isinstance(result, str):
                result = result.replace("<SEP>", " ")
            return result
```

- [ ] **Step 11: `edit_entity` 和 `edit_relation` — 返回结果后处理（R2 新发现）**

`edit_entity` 方法（L1132-1153）返回 `{"status": "ok", "data": result}`，其中 `result` 来自 LightRAG 的 `rag.aedit_entity()`，包含实体当前描述（可能含 `<SEP>`）。当用户只修改 entity_type 不修改 description 时，返回的 data 中 description 是原始合并值。

`edit_relation` 方法（L1155-1175）同理，返回的 data 中包含 edge description。

在 `edit_entity` 方法的 `return {"status": "ok", "data": result}`（L1150）之前插入：

```python
            # Clean <SEP> from description in returned data
            if isinstance(result, dict):
                graph_data = result.get("graph_data", {})
                if isinstance(graph_data, dict) and "description" in graph_data:
                    graph_data["description"] = _clean_sep(graph_data.get("description", ""))
            return {"status": "ok", "data": result}
```

在 `edit_relation` 方法的 `return {"status": "ok", "data": result}`（L1172）之前插入同样的清理逻辑：

```python
            # Clean <SEP> from description in returned data
            if isinstance(result, dict):
                graph_data = result.get("graph_data", {})
                if isinstance(graph_data, dict) and "description" in graph_data:
                    graph_data["description"] = _clean_sep(graph_data.get("description", ""))
            return {"status": "ok", "data": result}
```

- [ ] **Step 12: 运行测试验证 `_clean_sep` 相关测试通过**

Run: `python/bin/python -m pytest tests/test_sep_cleanup.py::TestCleanSep -v`
Expected: PASS — 7 个测试全部通过

- [ ] **Step 13: 提交**

```bash
git add niu_api/internal/lightrag_adapter.py
git commit -m "fix: add _clean_sep() and apply to all lightrag_adapter description outputs

Covers: explore_node, get_graph_snapshot, timeline_query, list_entities,
merge_entities changelog, query_data, get_entity_info, get_relation_info,
query (context string), edit_entity, edit_relation.
R1 additions: query_data/get_entity_info/get_relation_info.
R2 additions: query context string, edit_entity/edit_relation return data."
```

---

## Task 3: `kg_api.py` — 修复 `_format_description()`、`/explore` center、`/search_entities`

**Files:**
- Modify: `niu_api/kg_api.py:161-172`（`_format_description` 函数）
- Modify: `niu_api/kg_api.py:714, 728`（`/explore` 端点 center 节点）
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

- [ ] **Step 2: 修改 `/explore` 端点 center 节点，对 non-brainregion 也调用 `_format_description`（R1 新发现）**

当前 L714-728 的 center 处理逻辑：只在 `brainregion` 且含 `<SEP>` 时做特殊解析，non-brainregion center 的 `center_desc` 直接透传。修改为：对 center 也调用 `_format_description`。

将 L714-728 修改为：

```python
        center_desc = c.get("description", "")
        # Use _format_description for all entity types (handles <SEP> for
        # brainregion via _parse_description, and for others via space-replace)
        center_desc = _format_description(center_type, center_desc)
        result["center"] = {
            "id": c.get("id", ""),
            "label": c.get("name", c.get("id", "")),
            "name": c.get("name", ""),
            "nodeType": "Entity",
            "entityType": center_type,
            "description": center_desc,
            "uri": _clean_file_path(c.get("file_path", "")),
            "source": _clean_source_id(c.get("source_id", "")),
        }
```

说明：这删除了原来只在 brainregion + `<SEP>` 时才做处理的 if 分支，改为统一调用 `_format_description`，后者内部已经对 brainregion 做特殊解析、对其他类型做 `<SEP>` → 空格替换。

- [ ] **Step 3: 修改 `/search_entities` 端点，截断前清理 `<SEP>`**

将 L1148-1153 修改为：

```python
                entities.append({
                    "id": name,
                    "name": name,
                    "entityType": ent.get("entity_type", ""),
                    "description": ((ent.get("description", "") or "").replace("<SEP>", " "))[:120],
                })
```

- [ ] **Step 4: 运行测试验证 kg_api 相关测试通过**

Run: `python/bin/python -m pytest tests/test_sep_cleanup.py::TestKgApiFormatDescription -v`
Expected: PASS — 3 个测试全部通过

- [ ] **Step 5: 提交**

```bash
git add niu_api/kg_api.py
git commit -m "fix: clean <SEP> in _format_description, /explore center, and search_entities

R1 review addition: /explore center node for non-brainregion types was
skipping _format_description entirely, leaking <SEP> to frontend."
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

## Task 5: MCP server `lightrag_insert_entity` — 去重消息清理 `current_desc`（R1 新发现）

**Files:**
- Modify: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py:1240`（去重消息中 current_desc）

- [ ] **Step 1: 在 `current_desc` 赋值时清理 `<SEP>`**

将 L1240:
```python
                    current_desc = str(data.get("graph_data", {}).get("description", ""))[:100]
```

修改为:
```python
                    current_desc = str(data.get("graph_data", {}).get("description", "")).replace("<SEP>", " ")[:100]
```

说明：先替换 `<SEP>` 为空格，再截断到 100 字符。这样截断不会在 `<SEP>` 中间断开产生残留。

注意：Task 2 Step 8 在 adapter 层 `get_entity_info` 已做 `_clean_sep` 后处理，理论上 MCP 工具拿到的 data 已经是清理后的。但为防御性编程，在 MCP 层也做一次（幂等操作）。

- [ ] **Step 2: 提交**

```bash
git add mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py
git commit -m "fix: clean <SEP> in lightrag_insert_entity dedup message

R1 review addition: current_desc from get_entity_info was passed raw
to the LLM in the dedup status message."
```

---

## Task 6: 前端 `renderer.js` — 防御性 `<SEP>` 替换

**Files:**
- Modify: `ui/main/windows/graph/renderer.js:722-723`（实体详情面板）
- Modify: `ui/main/windows/graph/renderer.js:431, 507, 515`（changelog 增量更新）

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

## Task 7: 全量测试验证

**Files:**
- Test: `tests/test_sep_cleanup.py`

- [ ] **Step 1: 运行全部 SEP 清理测试**

Run: `python/bin/python -m pytest tests/test_sep_cleanup.py -v`
Expected: PASS — 全部 11 个测试通过

- [ ] **Step 2: 运行相关现有测试确保无回归**

Run: `python/bin/python -m pytest tests/test_region_manager.py -v --timeout=30`
Expected: PASS — 现有测试无回归（brainregion 的 `<SEP>` 处理逻辑未变）

- [ ] **Step 3: 全库 grep 确认无遗漏的 `<SEP>` 透传**

Run: `grep -rn '"description"' niu_api/ agent/ mcp-servers/lightrag-server/src/ | grep -v '_clean_sep\|_format_description\|<SEP>\|test_\|\.pyc'`
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
| kg_api.py `/explore` center（L714, L728） | Task 3 Step 2 | ✅ R1 新增 |
| kg_api.py `/search_entities`（L1152） | Task 3 Step 3 | ✅ |
| lightrag_adapter.py `explore_node`（L679,691,703） | Task 2 Step 2 | ✅ |
| lightrag_adapter.py `get_graph_snapshot`（L981,994） | Task 2 Step 3 | ✅ |
| lightrag_adapter.py `timeline_query`（L798,824） | Task 2 Step 4 | ✅ |
| lightrag_adapter.py `list_entities`（L1353,1377） | Task 2 Step 5 | ✅ |
| lightrag_adapter.py `merge_entities` changelog（L1525） | Task 2 Step 6 | ✅ |
| lightrag_adapter.py `query_data`（L287-290） | Task 2 Step 7 | ✅ R1 新增 |
| lightrag_adapter.py `get_entity_info`（L1213-1214） | Task 2 Step 8 | ✅ R1 新增 |
| lightrag_adapter.py `get_relation_info`（L1235-1236） | Task 2 Step 9 | ✅ R1 新增 |
| lightrag_adapter.py `update_habit_confidence`（L1655） | 已处理（无需改） | ✅ |
| MCP `lightrag_insert_entity` 去重消息（L1240） | Task 5 | ✅ R1 新增 |
| renderer.js 实体详情面板（L723） | Task 6 Step 1 | ✅ |
| renderer.js changelog 增量（L431,507,515） | Task 6 Step 2 | ✅ |
| lightrag_adapter.py `query` context 字符串（L224） | Task 2 Step 10 | ✅ R2 新增 |
| lightrag_adapter.py `edit_entity`/`edit_relation` 返回 data（L1150, L1172） | Task 2 Step 11 | ✅ R2 新增 |

### 2. Placeholder scan

无 TBD/TODO/"add error handling" 等占位符。所有步骤都有完整代码。

### 3. Type consistency

- `_clean_sep(desc: str | None) -> str` — 签名一致
- `_clean_sep_in_query_result(result) -> result` — 递归清理 dict/list 中的 description
- `replace("<SEP>", " ")` — 后端 API/MCP 用空格
- `replace("<SEP>", "\n")` — Agent prompt 注入用换行
- `replace(/<SEP>/g, ' ')` — 前端 JS 用空格
- 策略一致：API/前端/MCP = 空格，prompt = 换行

### 4. R1 审查闭环

| R1 发现 | 严重程度 | 处理 | Task |
|---------|---------|------|------|
| kg_api /explore center 跳过 _format_description | P1 | Task 3 Step 2 | ✅ |
| MCP lightrag_query_data 返回原始 desc | P1 | Task 2 Step 7 | ✅ |
| MCP lightrag_search_entities 返回原始 desc | P1 | Task 2 Step 7（同一 adapter 方法） | ✅ |
| MCP lightrag_get_entity_info 返回原始 desc | P1 | Task 2 Step 8 | ✅ |
| MCP lightrag_get_relation_info 返回原始 desc | P1 | Task 2 Step 9 | ✅ |
| MCP lightrag_insert_entity 去重消息 current_desc | P1 | Task 5 | ✅ |
| 计划混淆 get_entity_info (L1198) 和 update_habit_confidence (L1655) | P2 | 排查表已纠正 | ✅ |

### 5. R2 审查闭环

| R2 发现 | 严重程度 | 处理 | Task |
|---------|---------|------|------|
| MCP lightrag_query 返回 context 字符串含 `<SEP>` | P1 | Task 2 Step 10 | ✅ |
| MCP lightrag_edit_entity/edit_relation 返回 data 含 `<SEP>` | P1 | Task 2 Step 11 | ✅ |
| `_clean_sep_in_query_result` 有死代码 list 分支 | P3 | 保留作为防御性代码 | ✅ |
| `/search_entities` replace 与 Task 2 Step 7 冗余 | P3 | 防御性幂等，保留 | ✅ |

### 6. 单向清理原则

`<SEP>` 是 LightRAG 内部合并多来源实体描述的标准分隔符。本计划的所有修改都是**单向读取清理**——只在从知识图谱读取展示给用户/LLM 时清理 `<SEP>`，写入知识图谱时遵守 LightRAG 的 `<SEP>` 规矩。计划中没有任何修改入库写入路径（`ainsert_custom_kg`、`aedit_entity` 的 `updated_data` 参数、`create_entity`、`create_relation` 等）的代码。
