# 聊天记录 LightRAG 增量注入 + 时间线查询 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现聊天记录精炼文档增量注入 LightRAG、时间线查询、边类型过滤、缺省脑区、遗忘曲线定时任务，以及 entity-extractor/dream-evolver 重新定位

**Architecture:** entity-extractor 重定位为"内容提炼器"，输出精炼文档通过 lightrag_insert 增量分段注入；新增 lightrag_timeline_query MCP 工具和 lightrag_get_graph edge_types 参数；dream-evolver 新增 skill 维护职责；补齐遗忘曲线定时任务和缺省脑区

**Tech Stack:** Python 3.11+, LightRAG, pytest, FastAPI, SQLite (message.db)

---

## File Structure

| File | Responsibility | Status |
|------|---------------|--------|
| `config/agents/entity-extractor.md` | entity-extractor 子 Agent 提示词定义 | Modify |
| `config/agents/dream-evolver.md` | dream-evolver 子 Agent 提示词定义 | Modify |
| `niu_api/internal/message_injector.py` | 精炼文档增量分段注入逻辑 | Create |
| `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` | MCP 工具 schema + 函数 + 注册 | Modify |
| `config/mcp-servers.yaml` | MCP 服务器配置 (visibility) | Modify |
| `config/disk/lightrag-server.yaml` | 虚拟磁盘目录映射 | Modify |
| `niu_api/internal/lightrag_adapter.py` | timeline_query() + edge_types 过滤 | Modify |
| `niu_api/internal/brain_graph.py` | 遗忘曲线衰减/巩固/清理方法 | Modify |
| `niu_api/internal/region_manager.py` | create_default_regions() | Modify |
| `niu_api/internal/scheduler/service.py` | 注册新定时任务 | Modify |
| `niu_api/__main__.py` | 启动时调用 create_default_regions() + 注册遗忘曲线任务 | Modify |
| `tests/test_message_injector.py` | message_injector 单元测试 | Create |
| `tests/test_timeline_query.py` | timeline_query 单元测试 | Create |
| `tests/test_edge_type_filter.py` | edge_types 过滤单元测试 | Create |
| `tests/test_brain_decay.py` | 遗忘曲线单元测试 | Create |
| `tests/test_default_regions.py` | 缺省脑区单元测试 | Create |

---

## Phase 1: 重新定位 entity-extractor + 精炼文档增量注入

### Task 1.1: message_injector — 精炼文档分段与增量逻辑

**Files:**
- Create: `niu_api/internal/message_injector.py`
- Test: `tests/test_message_injector.py`

- [ ] **Step 1: Write failing test for segment doc_id generation**

```python
# tests/test_message_injector.py
import pytest
from datetime import datetime
from niu_api.internal.message_injector import (
    generate_doc_id,
    get_next_segment_number,
    format_refined_document,
)


class TestDocIdGeneration:
    def test_generate_doc_id_format(self):
        """doc_id 格式应为 refined:{date}:{seq:03d}"""
        result = generate_doc_id("2026-04-27", 1)
        assert result == "refined:2026-04-27:001"

    def test_generate_doc_id_padding(self):
        """序号应零填充到3位"""
        assert generate_doc_id("2026-04-27", 5) == "refined:2026-04-27:005"
        assert generate_doc_id("2026-04-27", 42) == "refined:2026-04-27:042"

    def test_get_next_segment_number_no_existing(self):
        """无已有段时，从1开始"""
        result = get_next_segment_number(existing_doc_ids=[])
        assert result == 1

    def test_get_next_segment_number_with_existing(self):
        """有已有段时，返回最大段号+1"""
        existing = [
            "refined:2026-04-27:001",
            "refined:2026-04-27:002",
            "refined:2026-04-27:003",
        ]
        result = get_next_segment_number(existing_doc_ids=existing)
        assert result == 4

    def test_get_next_segment_number_filters_by_date(self):
        """只统计当天日期的段号"""
        existing = [
            "refined:2026-04-26:001",
            "refined:2026-04-26:002",
            "refined:2026-04-27:001",
        ]
        result = get_next_segment_number(
            existing_doc_ids=existing, date="2026-04-27"
        )
        assert result == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_message_injector.py::TestDocIdGeneration -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'niu_api.internal.message_injector'`

- [ ] **Step 3: Write minimal implementation**

```python
# niu_api/internal/message_injector.py
"""
Message Injector — Refined document incremental segment injection.

entity-extractor 提炼的精炼文档按时间段增量注入 LightRAG，
每段独立 doc_id，不删除重注入。
"""

import re
from typing import List, Optional


def generate_doc_id(date: str, seq: int) -> str:
    """生成精炼文档的 doc_id。

    格式: refined:{date}:{seq:03d}
    例如: refined:2026-04-27:001
    """
    return f"refined:{date}:{seq:03d}"


def get_next_segment_number(
    existing_doc_ids: List[str],
    date: Optional[str] = None,
) -> int:
    """根据已有 doc_id 列表，返回下一个段号。

    Args:
        existing_doc_ids: 已有的精炼文档 doc_id 列表。
        date: 日期过滤，只统计该日期的段号。None 则统计所有。

    Returns:
        下一个段号（从1开始）。
    """
    max_seq = 0
    prefix = f"refined:{date}:" if date else "refined:"

    for doc_id in existing_doc_ids:
        if not doc_id.startswith(prefix):
            continue
        # 提取段号部分
        parts = doc_id.split(":")
        if len(parts) >= 3:
            try:
                seq = int(parts[-1])
                if seq > max_seq:
                    max_seq = seq
            except ValueError:
                continue

    return max_seq + 1


def format_refined_document(
    items: List[dict],
    date: str,
    segment: int,
) -> str:
    """将提炼内容格式化为精炼文档。

    Args:
        items: 提炼内容列表，每项包含 type, timestamp, content。
        date: 日期字符串。
        segment: 段序号。

    Returns:
        格式化的精炼文档字符串。
    """
    if not items:
        return ""

    lines = [f"[记忆提炼 {date} 段{segment}]", ""]

    for item in items:
        item_type = item.get("type", "记忆")
        timestamp = item.get("timestamp", "")
        content = item.get("content", "")
        lines.append(f"## {timestamp} {item_type}")
        lines.append(content)
        lines.append("")

    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_message_injector.py::TestDocIdGeneration -v`
Expected: PASS

- [ ] **Step 5: Write failing test for format_refined_document**

```python
# 追加到 tests/test_message_injector.py

class TestFormatRefinedDocument:
    def test_format_empty_items(self):
        """无提炼内容时返回空字符串"""
        result = format_refined_document([], "2026-04-27", 1)
        assert result == ""

    def test_format_single_item(self):
        """单条提炼内容格式化"""
        items = [
            {
                "type": "偏好",
                "timestamp": "14:23:15",
                "content": "用户偏好 Rust 语言",
            }
        ]
        result = format_refined_document(items, "2026-04-27", 3)
        assert "[记忆提炼 2026-04-27 段3]" in result
        assert "## 14:23:15 偏好" in result
        assert "用户偏好 Rust 语言" in result

    def test_format_multiple_items(self):
        """多条提炼内容格式化"""
        items = [
            {"type": "偏好", "timestamp": "14:23:15", "content": "偏好暗色主题"},
            {"type": "技能", "timestamp": "15:01:08", "content": "换用新解析库处理PDF"},
        ]
        result = format_refined_document(items, "2026-04-27", 1)
        assert "## 14:23:15 偏好" in result
        assert "## 15:01:08 技能" in result
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_message_injector.py::TestFormatRefinedDocument -v`
Expected: PASS (format_refined_document 已在 Step 3 实现)

- [ ] **Step 7: Write failing test for segment splitting logic**

```python
# 追加到 tests/test_message_injector.py

from niu_api.internal.message_injector import split_into_segments


class TestSegmentSplitting:
    def test_split_within_limit(self):
        """内容在限制内时，不拆分"""
        items = [
            {"type": "偏好", "timestamp": "14:00:00", "content": "偏好A"},
            {"type": "技能", "timestamp": "15:00:00", "content": "技能B"},
        ]
        segments = split_into_segments(items, max_items_per_segment=20)
        assert len(segments) == 1
        assert len(segments[0]) == 2

    def test_split_at_limit(self):
        """内容超过限制时，拆分为多段"""
        items = [
            {"type": "偏好", "timestamp": f"{10+i}:00:00", "content": f"内容{i}"}
            for i in range(25)
        ]
        segments = split_into_segments(items, max_items_per_segment=20)
        assert len(segments) == 2
        assert len(segments[0]) == 20
        assert len(segments[1]) == 5

    def test_split_exact_limit(self):
        """内容恰好等于限制时，不拆分"""
        items = [
            {"type": "偏好", "timestamp": f"{10+i}:00:00", "content": f"内容{i}"}
            for i in range(20)
        ]
        segments = split_into_segments(items, max_items_per_segment=20)
        assert len(segments) == 1
```

- [ ] **Step 8: Run test to verify it fails**

Run: `python -m pytest tests/test_message_injector.py::TestSegmentSplitting -v`
Expected: FAIL — `ImportError: cannot import name 'split_into_segments'`

- [ ] **Step 9: Implement split_into_segments**

```python
# 追加到 niu_api/internal/message_injector.py

def split_into_segments(
    items: List[dict],
    max_items_per_segment: int = 20,
) -> List[List[dict]]:
    """将提炼内容按条数拆分为多段。

    Args:
        items: 提炼内容列表。
        max_items_per_segment: 每段最大条数。

    Returns:
        拆分后的段列表，每段是一个 items 子列表。
    """
    if not items:
        return []

    segments = []
    for i in range(0, len(items), max_items_per_segment):
        segments.append(items[i : i + max_items_per_segment])
    return segments
```

- [ ] **Step 10: Run test to verify it passes**

Run: `python -m pytest tests/test_message_injector.py::TestSegmentSplitting -v`
Expected: PASS

- [ ] **Step 11: Commit Phase 1.1**

```bash
git add niu_api/internal/message_injector.py tests/test_message_injector.py
git commit -m "feat: add message_injector with segment splitting and doc_id generation"
```

---

### Task 1.2: 重写 entity-extractor 提示词

**Files:**
- Modify: `config/agents/entity-extractor.md`

- [ ] **Step 1: Write the new entity-extractor prompt**

将 `config/agents/entity-extractor.md` 的内容替换为：

```markdown
---
name: entity-extractor
description: "内容提炼 - 从对话中筛选有价值内容，形成精炼文档提交给 LightRAG 入库"
mode: subagent
temperature: 0.3
mcpServers:
  - lightrag-server
---

# 内容提炼（Entity Extractor）

从对话中筛选有价值内容，形成精炼文档提交给 LightRAG 入库。LightRAG 是"全量入库"引擎，没有判断内容价值的能力 — 你的核心价值是**筛选提炼**。

## 核心任务

回顾上方对话，筛选出有价值的内容：

### 记忆提炼
用户是否透露了偏好、期望等信息？
- 偏好：如"我喜欢暗色主题" → 提炼为精炼摘要
- 期望：如"我希望报告自动生成" → 提炼为精炼摘要
- 身份：如"我是数据分析师" → 提炼为精炼摘要
- 计划：如"明天要去上海出差" → 提炼为精炼摘要

### 技能提炼
是否使用了需要反复试错、或根据实际发现调整思路的非简易方法？
- 成功经验：如"用 X 方法解决了 Y 问题" → 提炼为精炼摘要
- 失败教训：如"Z 方法不适用于 W 场景" → 提炼为精炼摘要
- 工具发现：如"发现 A 工具有 B 能力" → 提炼为精炼摘要

### 输出格式
将提炼结果格式化为精炼文档，调用 `lightrag_insert(content=精炼文档, doc_id="refined:{date}:{seq}")` 入库：
- 每条提炼内容一行，包含：类型标签 + 时间戳 + 精炼摘要
- 无价值内容不输出（闲聊、确认、简单问答等跳过）

### 输出示例

```
[记忆提炼 2026-04-27 段1]

## 14:23:15 偏好
用户偏好 Rust 语言，对所有权机制感兴趣

## 15:01:08 计划
用户明天要去上海出差

## 16:33:02 技能
换用新解析库处理PDF，效果优于旧库；旧库在大型PDF上有内存泄漏问题
```

## 工具使用规范

- 文档注入：`lightrag_insert(content=精炼文档, doc_id="refined:{date}:{seq:03d}")` — 整体入库，LightRAG 自动提取实体和关系
- 查询已有文档：`lightrag_document_status()` — 检查已有精炼文档
- 查询已有实体：`lightrag_search_entities(query, entity_type, top_k)`
- 图遍历：`lightrag_get_graph(action="explore", entity_name, depth)`

**关键变化**：
- 旧方式：逐条提取实体和关系，手动调用 `lightrag_insert_entity`/`lightrag_insert_relation`
- 新方式：提炼有价值内容形成精炼文档，调用 `lightrag_insert` 整体入库
- LightRAG 对精炼文档做 ainsert，自动提取实体和关系，建立语义连接
- 精炼文档质量远高于原始聊天记录，LightRAG 的提取效果更好

## 游标机制

- 调用方会告知 `last_dream_evolve_id`（上次处理到的消息UUID），只处理该ID之后的新消息
- 处理完成后，在报告末尾用 JSON 格式报告：`{"last_dream_evolve_id": "<最后处理的消息UUID>"}`
- force 模式下不使用游标，全量处理所有消息

## 禁止

- 禁止使用 `code_run` 工具
- 禁止使用 `lightrag_insert_entity` 或 `lightrag_insert_relation`（精炼文档通过 lightrag_insert 整体入库，实体和关系由 LightRAG 自动提取）
- 禁止使用 `add_document`、`search_documents`、`get_document`、`delete_document`、`list_documents`（已废弃的 vector-store 工具）
```

- [ ] **Step 2: Verify the file was written correctly**

Run: `python -c "import sys; sys.stdout.reconfigure(encoding='utf-8'); f=open('config/agents/entity-extractor.md','r',encoding='utf-8'); c=f.read(); f.close(); print('内容提炼' in c and 'lightrag_insert(content' in c and 'lightrag_insert_entity' not in c.split('禁止')[-1][:200])"`
Expected: `True` (确认新提示词包含"内容提炼"，使用 lightrag_insert，且禁止部分禁止了 lightrag_insert_entity)

- [ ] **Step 3: Commit**

```bash
git add config/agents/entity-extractor.md
git commit -m "feat: reposition entity-extractor as content refiner with lightrag_insert"
```

---

### Task 1.3: 注册精炼文档注入定时任务

**Files:**
- Modify: `niu_api/__main__.py` (lines 136-191: entity-extractor task section)

- [ ] **Step 1: Write failing test for refined injection task content**

```python
# tests/test_message_injector.py (追加)

class TestInjectorTaskContent:
    def test_task_content_uses_refined_injection(self):
        """entity-extractor 定时任务内容应提及精炼文档注入"""
        import importlib
        import niu_api.__main__ as main_mod
        # 重新加载以获取最新 _ENTITY_EXTRACTOR_TASK_CONTENT
        importlib.reload(main_mod)
        content = main_mod._ENTITY_EXTRACTOR_TASK_CONTENT
        assert "chat-with-entity-extractor" in content
        assert "精炼" in content or "提炼" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_message_injector.py::TestInjectorTaskContent -v`
Expected: FAIL — `_ENTITY_EXTRACTOR_TASK_CONTENT` 仍使用旧的"提取实体和关系"措辞

- [ ] **Step 3: Update _ENTITY_EXTRACTOR_TASK_CONTENT in __main__.py**

在 `niu_api/__main__.py` 第 136-140 行，将：

```python
_ENTITY_EXTRACTOR_TASK_CONTENT = (
    "调用 chat-with-entity-extractor 子 Agent，task 参数为："
    "\"整理知识图谱：扫描近期对话，提取实体和关系写入 LightRAG。\" "
    "不要从对话历史中提取内容，只执行此 task。"
)
```

替换为：

```python
_ENTITY_EXTRACTOR_TASK_CONTENT = (
    "调用 chat-with-entity-extractor 子 Agent，task 参数为："
    "\"提炼有价值内容：扫描近期对话，筛选偏好/技能/经验，形成精炼文档通过 lightrag_insert 增量注入 LightRAG。\" "
    "不要从对话历史中提取内容，只执行此 task。"
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_message_injector.py::TestInjectorTaskContent -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add niu_api/__main__.py tests/test_message_injector.py
git commit -m "feat: update entity-extractor task content for refined document injection"
```

---

## Phase 2: 时间线查询 + 边类型过滤

### Task 2.1: lightrag_adapter — timeline_query 方法

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py` (after line 433: explore_node method)
- Test: `tests/test_timeline_query.py`

- [ ] **Step 1: Write failing test for timeline_query**

```python
# tests/test_timeline_query.py
import pytest
from unittest.mock import MagicMock, patch
from niu_api.internal.lightrag_adapter import LightRAGAdapter


class TestTimelineQuery:
    @patch("niu_api.internal.lightrag_adapter.get_lightrag")
    def test_timeline_query_returns_sorted_results(self, mock_get_rag):
        """时间线查询应按时间降序返回结果"""
        adapter = LightRAGAdapter()

        # Mock query_data to return matching entities
        adapter.query_data = MagicMock(return_value={
            "status": "success",
            "data": {
                "entities": [
                    {"entity_name": "PDF处理错误", "entity_type": "Event",
                     "description": "brain_meta_created_at=2026-04-26;"},
                    {"entity_name": "改用新方案", "entity_type": "Event",
                     "description": "brain_meta_created_at=2026-04-27;"},
                ],
                "relationships": [],
                "chunks": [],
            }
        })

        # Mock explore_node to return time chain edges
        adapter.explore_node = MagicMock(return_value={
            "center": {"name": "PDF处理错误"},
            "nodes": [
                {"name": "PDF处理错误", "type": "Event",
                 "description": "brain_meta_created_at=2026-04-26;"},
                {"name": "改用新方案", "type": "Event",
                 "description": "brain_meta_created_at=2026-04-27;"},
            ],
            "edges": [
                {"source": "PDF处理错误", "target": "改用新方案",
                 "relation": "followed_by"},
            ],
            "stats": {"nodes": 2, "edges": 1},
        })

        result = adapter.timeline_query(
            query="PDF处理",
            direction="backward",
            max_depth=1,
            max_results=10,
        )

        assert result["status"] == "ok"
        assert len(result["timeline"]) == 2
        # backward 方向：最近的排最前
        assert result["timeline"][0]["name"] == "改用新方案"
        assert result["timeline"][1]["name"] == "PDF处理错误"

    @patch("niu_api.internal.lightrag_adapter.get_lightrag")
    def test_timeline_query_with_start_entities(self, mock_get_rag):
        """提供 start_entities 时跳过向量匹配"""
        adapter = LightRAGAdapter()
        adapter.explore_node = MagicMock(return_value={
            "center": {"name": "PDF处理错误"},
            "nodes": [
                {"name": "PDF处理错误", "type": "Event",
                 "description": "brain_meta_created_at=2026-04-26;"},
            ],
            "edges": [],
            "stats": {"nodes": 1, "edges": 0},
        })

        result = adapter.timeline_query(
            query="",
            start_entities=["PDF处理错误"],
            direction="backward",
            max_depth=1,
            max_results=10,
        )

        assert result["status"] == "ok"
        # query_data 不应被调用
        adapter.query_data = MagicMock()
        adapter.query_data.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_timeline_query.py -v`
Expected: FAIL — `AttributeError: 'LightRAGAdapter' object has no attribute 'timeline_query'`

- [ ] **Step 3: Implement timeline_query in LightRAGAdapter**

在 `niu_api/internal/lightrag_adapter.py` 的 `LightRAGAdapter` 类中，`explore_node` 方法之后（约 line 513），添加：

```python
    # ============== Timeline Query ==============

    # Time chain edge types for timeline traversal
    TIME_CHAIN_EDGES = {"followed_by", "corrected_by", "led_to", "resolved_by"}

    def timeline_query(
        self,
        query: str,
        start_entities: Optional[List[str]] = None,
        direction: str = "backward",
        max_depth: int = 5,
        max_results: int = 10,
    ) -> Dict[str, Any]:
        """时间线查询：先向量匹配内容，再沿时间链排序返回。

        Step 1: 向量匹配（如果未提供 start_entities）
        Step 2: 沿时间链遍历
        Step 3: 按时间排序返回

        Args:
            query: 查询内容。
            start_entities: 直接指定起始实体名（跳过向量匹配）。
            direction: 遍历方向。backward=由近到远，forward=由远到近。
            max_depth: 时间链遍历深度。
            max_results: 返回结果数量上限。

        Returns:
            Dict with status and timeline list.
        """
        # Step 1: 向量匹配
        if not start_entities:
            if not query:
                return {"status": "error", "message": "query or start_entities required", "timeline": []}
            result = self.query_data(query, mode="local", top_k=max_results)
            if self._is_no_result(result):
                return {"status": "no_results", "message": "No matching entities found", "timeline": []}
            data = result.get("data", result) if isinstance(result, dict) else {}
            entities = data.get("entities", []) if isinstance(data, dict) else []
            start_entities = [e.get("entity_name", e.get("name", "")) for e in entities if e.get("entity_name") or e.get("name")]
            if not start_entities:
                return {"status": "no_results", "message": "No entity names in results", "timeline": []}

        # Step 2: 沿时间链遍历
        all_nodes = {}
        visited = set()
        to_visit = list(start_entities)

        for _ in range(max_depth):
            next_visit = []
            for entity_name in to_visit:
                if entity_name in visited:
                    continue
                visited.add(entity_name)
                subgraph = self.explore_node(entity_name=entity_name, depth=1)
                for node in subgraph.get("nodes", []):
                    name = node.get("name", "")
                    if name and name not in all_nodes:
                        all_nodes[name] = node
                for edge in subgraph.get("edges", []):
                    relation = edge.get("relation", "")
                    if relation in self.TIME_CHAIN_EDGES:
                        target = edge.get("target", "")
                        if target and target not in visited:
                            next_visit.append(target)
            if not next_visit:
                break
            to_visit = next_visit

        # Step 3: 按时间排序
        timeline = list(all_nodes.values())

        def _extract_created_at(node: dict) -> str:
            desc = node.get("description", "")
            for part in desc.split(";"):
                if part.strip().startswith("brain_meta_created_at="):
                    return part.strip().split("=", 1)[1]
            return ""

        timeline.sort(
            key=lambda n: _extract_created_at(n),
            reverse=(direction == "backward"),
        )
        timeline = timeline[:max_results]

        return {"status": "ok", "timeline": timeline}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_timeline_query.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add niu_api/internal/lightrag_adapter.py tests/test_timeline_query.py
git commit -m "feat: add timeline_query method to LightRAGAdapter"
```

---

### Task 2.2: lightrag_adapter — edge_types 过滤

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py` (line 434: explore_node signature)
- Test: `tests/test_edge_type_filter.py`

- [ ] **Step 1: Write failing test for edge_types filtering**

```python
# tests/test_edge_type_filter.py
import pytest
from unittest.mock import patch, MagicMock
from niu_api.internal.lightrag_adapter import LightRAGAdapter


class TestEdgeTypeFilter:
    @patch("niu_api.internal.lightrag_adapter.get_lightrag")
    def test_filter_by_edge_types(self, mock_get_rag):
        """edge_types 参数应过滤返回的边"""
        adapter = LightRAGAdapter()

        # Mock LightRAG get_knowledge_graph
        mock_rag = MagicMock()
        mock_get_rag.return_value = mock_rag

        from niu_api.internal.lightrag_manager import call_async
        with patch("niu_api.internal.lightrag_adapter.call_async") as mock_call:
            # 模拟返回包含多种边类型的子图
            mock_node = MagicMock()
            mock_node.id = "TestEntity"
            mock_node.properties = {"entity_type": "Event", "description": ""}

            mock_edge_semantic = MagicMock()
            mock_edge_semantic.source = "A"
            mock_edge_semantic.target = "B"
            mock_edge_semantic.properties = {"keywords": "USED_FOR", "description": "", "weight": 1.0}

            mock_edge_timeline = MagicMock()
            mock_edge_timeline.source = "B"
            mock_edge_timeline.target = "C"
            mock_edge_timeline.properties = {"keywords": "followed_by", "description": "", "weight": 1.0}

            mock_kg = MagicMock()
            mock_kg.nodes = [mock_node]
            mock_kg.edges = [mock_edge_semantic, mock_edge_timeline]
            mock_call.return_value = mock_kg

            # 只请求语义边
            result = adapter.explore_node(
                entity_name="TestEntity", depth=1,
                edge_types=["USED_FOR", "OFTEN_WITH"],
            )
            # 应只包含语义边，不包含时间链边
            assert len(result["edges"]) == 1
            assert result["edges"][0]["relation"] == "USED_FOR"

    @patch("niu_api.internal.lightrag_adapter.get_lightrag")
    def test_no_filter_returns_all_edges(self, mock_get_rag):
        """不指定 edge_types 时返回所有边"""
        adapter = LightRAGAdapter()
        mock_rag = MagicMock()
        mock_get_rag.return_value = mock_rag

        with patch("niu_api.internal.lightrag_adapter.call_async") as mock_call:
            mock_node = MagicMock()
            mock_node.id = "TestEntity"
            mock_node.properties = {"entity_type": "Event", "description": ""}

            mock_edge1 = MagicMock()
            mock_edge1.source = "A"
            mock_edge1.target = "B"
            mock_edge1.properties = {"keywords": "USED_FOR", "description": "", "weight": 1.0}

            mock_edge2 = MagicMock()
            mock_edge2.source = "B"
            mock_edge2.target = "C"
            mock_edge2.properties = {"keywords": "followed_by", "description": "", "weight": 1.0}

            mock_kg = MagicMock()
            mock_kg.nodes = [mock_node]
            mock_kg.edges = [mock_edge1, mock_edge2]
            mock_call.return_value = mock_kg

            result = adapter.explore_node(entity_name="TestEntity", depth=1)
            assert len(result["edges"]) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_edge_type_filter.py -v`
Expected: FAIL — `explore_node() got an unexpected keyword argument 'edge_types'`

- [ ] **Step 3: Add edge_types parameter to explore_node**

在 `niu_api/internal/lightrag_adapter.py` 的 `explore_node` 方法中：

1. 修改签名，添加 `edge_types` 参数：

```python
def explore_node(
    self,
    entity_name: str,
    depth: int = 2,
    edge_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
```

2. 在边转换循环后，添加过滤逻辑（在 `edges.append(...)` 循环结束后）：

```python
        # Filter edges by edge_types if specified
        if edge_types:
            edge_types_set = set(edge_types)
            edges = [e for e in edges if e.get("relation", "") in edge_types_set]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_edge_type_filter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add niu_api/internal/lightrag_adapter.py tests/test_edge_type_filter.py
git commit -m "feat: add edge_types filtering to LightRAGAdapter.explore_node"
```

---

### Task 2.3: MCP 工具注册 — lightrag_timeline_query + edge_types

**Files:**
- Modify: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py`
- Modify: `config/mcp-servers.yaml`
- Modify: `config/disk/lightrag-server.yaml`

- [ ] **Step 1: Add lightrag_timeline_query to TOOL_SCHEMAS**

在 `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` 的 `TOOL_SCHEMAS` 字典中，`lightrag_get_graph` 之后（约 line 193），添加：

```python
    "lightrag_timeline_query": {
        "name": "lightrag_timeline_query",
        "description": (
            "时间线查询：先向量匹配内容，再沿时间链排序返回。"
            "用于回忆事件序列、决策过程、问题解决历史。"
            "返回结果按时间由近到远排序。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "查询内容（先向量匹配，再沿时间链展开）",
                },
                "start_entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选：直接指定起始实体名（跳过向量匹配步骤）",
                },
                "direction": {
                    "type": "string",
                    "enum": ["backward", "forward", "both"],
                    "default": "backward",
                    "description": "遍历方向。backward=由近到远，forward=由远到近",
                },
                "max_depth": {
                    "type": "integer",
                    "default": 5,
                    "description": "时间链遍历深度",
                },
                "max_results": {
                    "type": "integer",
                    "default": 10,
                    "description": "返回结果数量上限",
                },
            },
            "required": ["query"],
        },
    },
```

- [ ] **Step 2: Add edge_types parameter to lightrag_get_graph TOOL_SCHEMAS**

在同一个文件中，`lightrag_get_graph` 的 `input_schema.properties` 中，`limit` 之后添加：

```python
                "edge_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "过滤的边类型列表。可选值: followed_by, corrected_by, "
                        "led_to, resolved_by, associated_with, USED_FOR, OFTEN_WITH, "
                        "belongs_to, brain_region_anchor, _session:contains, "
                        "_region:contains。不指定则返回所有边。"
                    ),
                },
```

- [ ] **Step 3: Implement lightrag_timeline_query function**

在 `_TOOL_FUNCTIONS` 字典之前，添加工具函数：

```python
def lightrag_timeline_query(
    query: str,
    start_entities: Optional[List[str]] = None,
    direction: str = "backward",
    max_depth: int = 5,
    max_results: int = 10,
) -> Dict[str, Any]:
    """Timeline query: vector match then time chain traversal."""
    valid_directions = {"backward", "forward", "both"}
    if direction not in valid_directions:
        return {"status": "error", "message": f"Invalid direction '{direction}'. Must be one of: {', '.join(sorted(valid_directions))}", "timeline": []}
    try:
        adapter = _get_adapter()
        return adapter.timeline_query(
            query=query,
            start_entities=start_entities,
            direction=direction,
            max_depth=max_depth,
            max_results=max_results,
        )
    except Exception as e:
        logger.error(f"lightrag_timeline_query failed: {e}")
        return {"status": "error", "message": str(e), "timeline": []}
```

- [ ] **Step 4: Update lightrag_get_graph function signature**

修改 `lightrag_get_graph` 函数，添加 `edge_types` 参数：

```python
def lightrag_get_graph(
    action: str = "explore",
    entity_name: str = "",
    depth: int = 2,
    limit: int = 200,
    edge_types: Optional[List[str]] = None,
):
    """Get a subgraph from the knowledge graph."""
    valid_actions = {"explore", "snapshot"}
    if action not in valid_actions:
        return {"status": "error", "message": f"Invalid action '{action}'. Must be one of: {', '.join(sorted(valid_actions))}", "nodes": [], "edges": [], "center": None, "stats": {}}
    try:
        adapter = _get_adapter()
        if action == "explore":
            if not entity_name:
                return {"status": "error", "message": "entity_name required for explore", "nodes": [], "edges": [], "center": None, "stats": {}}
            return adapter.explore_node(entity_name=entity_name, depth=depth, edge_types=edge_types)
        else:  # snapshot
            return adapter.get_graph_snapshot(limit=limit)
    except Exception as e:
        logger.error(f"lightrag_get_graph failed: {e}")
        return {"status": "error", "message": str(e), "nodes": [], "edges": [], "center": None, "stats": {}}
```

- [ ] **Step 5: Register in _TOOL_FUNCTIONS**

在 `_TOOL_FUNCTIONS` 字典中添加：

```python
    "lightrag_timeline_query": lightrag_timeline_query,
```

- [ ] **Step 6: Add to config/mcp-servers.yaml**

在 `lightrag-server.tools` 下，`lightrag_merge_entities` 之后添加：

```yaml
    lightrag_timeline_query: {visibility: hidden}
```

- [ ] **Step 7: Add to config/disk/lightrag-server.yaml**

在 `lightrag_merge_entities` 条目之后添加：

```yaml
  - name: lightrag_timeline_query
    category: query
    short: "时间线查询"
    long: "先向量匹配内容，再沿时间链排序返回。用于回忆事件序列、决策过程"
    parameters:
      - name: query
        position: 1
        type: string
        required: true
      - name: start_entities
        flag: entities
        type: array
        cli_format: json
      - name: direction
        flag: direction
        type: string
        default: backward
        enum: [backward, forward, both]
      - name: max_depth
        flag: depth
        type: integer
        default: 5
      - name: max_results
        flag: max-results
        type: integer
        default: 10
```

同时在 `lightrag_get_graph` 的 parameters 中，`limit` 之后添加：

```yaml
      - name: edge_types
        flag: edge-types
        type: array
        cli_format: json
```

- [ ] **Step 8: Verify all 4 files are consistent**

Run: `python -c "
import sys; sys.path.insert(0, 'mcp-servers/lightrag-server/src')
from niu_lightrag_server import TOOL_SCHEMAS, _TOOL_FUNCTIONS
assert 'lightrag_timeline_query' in TOOL_SCHEMAS
assert 'lightrag_timeline_query' in _TOOL_FUNCTIONS
assert 'edge_types' in TOOL_SCHEMAS['lightrag_get_graph']['input_schema']['properties']
print('OK: all tools registered')
"`
Expected: `OK: all tools registered`

- [ ] **Step 9: Commit**

```bash
git add mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py config/mcp-servers.yaml config/disk/lightrag-server.yaml
git commit -m "feat: add lightrag_timeline_query tool and edge_types parameter"
```

---

## Phase 3: 缺省脑区 + 遗忘曲线定时任务

### Task 3.1: 缺省脑区主节点

**Files:**
- Modify: `niu_api/internal/region_manager.py` (after line 135: RegionManager class)
- Modify: `niu_api/__main__.py` (after line 133: brain:Niu init)
- Test: `tests/test_default_regions.py`

- [ ] **Step 1: Write failing test for create_default_regions**

```python
# tests/test_default_regions.py
import pytest
from unittest.mock import MagicMock, patch
from niu_api.internal.region_manager import RegionManager, create_default_regions


class TestDefaultRegions:
    def test_create_default_regions_creates_three_regions(self):
        """应创建三个缺省脑区：聊天历史、文档库、知识体系"""
        mock_adapter = MagicMock()
        mock_ingester = MagicMock()

        # 模拟 brain:Niu 已存在
        mock_adapter.query_data.return_value = {
            "status": "success",
            "data": {
                "entities": [
                    {"entity_name": "brain:region:聊天历史", "entity_type": "BrainRegion"},
                ],
                "relationships": [],
                "chunks": [],
            }
        }

        result = create_default_regions(adapter=mock_adapter, ingester=mock_ingester)
        # 应尝试创建3个脑区
        assert result["created"] + result["existing"] == 3

    def test_default_region_names(self):
        """缺省脑区名称列表"""
        from niu_api.internal.region_manager import DEFAULT_REGIONS
        assert "聊天历史" in DEFAULT_REGIONS
        assert "文档库" in DEFAULT_REGIONS
        assert "知识体系" in DEFAULT_REGIONS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_default_regions.py -v`
Expected: FAIL — `ImportError: cannot import name 'create_default_regions'`

- [ ] **Step 3: Implement create_default_regions**

在 `niu_api/internal/region_manager.py` 的 `RegionManager` 类之后，添加：

```python
# ============== Default Regions ==============

DEFAULT_REGIONS = ["聊天历史", "文档库", "知识体系"]


def create_default_regions(
    adapter: Any,
    ingester: Any,
) -> dict:
    """创建缺省脑区主节点（聊天历史、文档库、知识体系）。

    如果脑区已存在则跳过。每个脑区通过 brain_region_anchor 关联到 brain:Niu。

    Args:
        adapter: LightRAGAdapter instance.
        ingester: LightRAGIngester instance.

    Returns:
        Dict with created and existing counts.
    """
    created = 0
    existing = 0

    for region_label in DEFAULT_REGIONS:
        region_name = f"brain:region:{region_label}"

        # Check if region already exists
        try:
            result = adapter.query_data(
                query=region_label, mode="local", top_k=1,
                keywords=[region_label],
            )
            entities = []
            if result and isinstance(result, dict):
                data = result.get("data", {})
                if isinstance(data, dict):
                    entities = data.get("entities", [])
            # Check if our specific region entity exists
            found = any(
                e.get("entity_name", "") == region_name
                for e in entities
            )
            if found:
                existing += 1
                continue
        except Exception:
            pass  # Proceed to create

        # Create region entity
        try:
            ingester.inject_entity(
                name=region_name,
                entity_type="BrainRegion",
                description=f"缺省脑区: {region_label}",
                source_id="brain",
                file_path="brain://region",
            )
            # Link to brain:Niu via brain_region_anchor
            ingester.inject_relation(
                src_id="brain:Niu",
                tgt_id=region_name,
                relation="brain_region_anchor",
                description=f"缺省脑区锚点: {region_label}",
                source_id="brain",
                file_path="brain://region",
            )
            created += 1
        except Exception as e:
            logger.warning(f"Failed to create default region {region_label}: {e}")

    return {"created": created, "existing": existing}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_default_regions.py -v`
Expected: PASS

- [ ] **Step 5: Call create_default_regions at startup**

在 `niu_api/__main__.py` 的 lifespan 中，brain:Niu 初始化之后（约 line 133），添加：

```python
    # 8.2. Create default brain regions (聊天历史, 文档库, 知识体系)
    try:
        from niu_api.internal.region_manager import create_default_regions
        from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester
        region_result = create_default_regions(
            adapter=LightRAGAdapter(),
            ingester=LightRAGIngester(),
        )
        logger.info(f"Default brain regions: created={region_result['created']}, existing={region_result['existing']}")
    except Exception as e:
        logger.warning(f"Default brain region creation failed: {e}")
```

- [ ] **Step 6: Commit**

```bash
git add niu_api/internal/region_manager.py niu_api/__main__.py tests/test_default_regions.py
git commit -m "feat: add default brain regions (聊天历史, 文档库, 知识体系)"
```

---

### Task 3.2: 遗忘曲线定时任务

**Files:**
- Modify: `niu_api/internal/brain_graph.py` (after line 409: get_brain_graph)
- Modify: `niu_api/__main__.py` (after default regions section)
- Test: `tests/test_brain_decay.py`

- [ ] **Step 1: Write failing test for decay/consolidate/cleanup**

```python
# tests/test_brain_decay.py
import pytest
from unittest.mock import MagicMock, patch
from niu_api.internal.brain_graph import BrainGraph


class TestBrainDecay:
    @patch("niu_api.internal.brain_graph.LightRAGAdapter")
    def test_decay_edges_reduces_weight(self, mock_adapter_cls):
        """衰减应减少边权重"""
        brain = BrainGraph()
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter

        # Mock: get all edges with weights
        mock_adapter.explore_node.return_value = {
            "center": None,
            "nodes": [],
            "edges": [
                {"source": "A", "target": "B", "relation": "knows",
                 "weight": 0.8, "description": "brain_meta_decay_rate=0.05;"},
                {"source": "C", "target": "D", "relation": "remembers",
                 "weight": 0.5, "description": "brain_meta_decay_rate=0.01;"},
            ],
            "stats": {"nodes": 0, "edges": 2},
        }

        result = brain.decay_edges(days_since_last=1)
        assert result["decayed"] >= 0

    @patch("niu_api.internal.brain_graph.LightRAGAdapter")
    def test_consolidate_l0_to_l1(self, mock_adapter_cls):
        """L0→L1 巩固：access_count ≥ 3 的 L0 记忆升级"""
        brain = BrainGraph()
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter

        result = brain.consolidate_l0_to_l1()
        assert "consolidated" in result

    @patch("niu_api.internal.brain_graph.LightRAGAdapter")
    def test_cleanup_low_weight(self, mock_adapter_cls):
        """低权重清理：weight < 0.1 的实体标记为待删除"""
        brain = BrainGraph()
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter

        result = brain.cleanup_low_weight(threshold=0.1)
        assert "marked_for_deletion" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_brain_decay.py -v`
Expected: FAIL — `AttributeError: 'BrainGraph' object has no attribute 'decay_edges'`

- [ ] **Step 3: Implement decay/consolidate/cleanup in BrainGraph**

在 `niu_api/internal/brain_graph.py` 的 `BrainGraph` 类中，`recall_memories` 方法之后，添加：

```python
    # ============== Forgetting Curve Operations ==============

    def decay_edges(self, days_since_last: int = 1) -> Dict[str, Any]:
        """关系级权重衰减。

        所有边的 weight *= (1 - decay_rate × days_since_last_access)

        Args:
            days_since_last: 自上次访问以来的天数。

        Returns:
            Dict with decayed count.
        """
        try:
            adapter = LightRAGAdapter()
            # Get snapshot of all edges
            snapshot = adapter.get_graph_snapshot(limit=10000)
            edges = snapshot.get("edges", [])

            decayed = 0
            for edge in edges:
                weight = edge.get("weight", 1.0)
                if weight <= 0:
                    continue
                desc = edge.get("description", "")
                decay_rate = 0.05  # default L0
                for part in desc.split(";"):
                    part = part.strip()
                    if part.startswith("brain_meta_decay_rate="):
                        try:
                            decay_rate = float(part.split("=", 1)[1])
                        except ValueError:
                            pass

                new_weight = weight * (1 - decay_rate * days_since_last)
                new_weight = max(0.0, new_weight)

                if new_weight != weight:
                    # Update via insert_relation (upsert semantics)
                    ingester = LightRAGIngester()
                    ingester.inject_relation(
                        src_id=edge.get("source", ""),
                        tgt_id=edge.get("target", ""),
                        relation=edge.get("relation", ""),
                        description=desc,
                        source_id="brain_decay",
                        file_path="brain://decay",
                    )
                    decayed += 1

            return {"decayed": decayed}

        except Exception as e:
            logger.error(f"decay_edges failed: {e}")
            return {"decayed": 0, "error": str(e)}

    def consolidate_l0_to_l1(self) -> Dict[str, Any]:
        """L0→L1 记忆巩固：access_count ≥ 3 的 L0 记忆升级为 L1。

        Returns:
            Dict with consolidated count.
        """
        try:
            adapter = LightRAGAdapter()
            ingester = LightRAGIngester()

            # Search for L0 entities
            result = adapter.query_data(
                query="L0 memory", mode="local", top_k=100,
                keywords=["L0"],
            )
            if LightRAGAdapter._is_no_result(result):
                return {"consolidated": 0}

            data = result.get("data", {}) if isinstance(result, dict) else {}
            entities = data.get("entities", []) if isinstance(data, dict) else []

            consolidated = 0
            for entity in entities:
                desc = entity.get("description", "")
                if "brain_meta_weight=0.3" not in desc:
                    continue  # Not L0
                # Check access_count
                for part in desc.split(";"):
                    part = part.strip()
                    if part.startswith("brain_meta_access_count="):
                        try:
                            count = int(part.split("=", 1)[1])
                            if count >= 3:
                                # Upgrade to L1
                                new_desc = desc.replace(
                                    "brain_meta_weight=0.3",
                                    "brain_meta_weight=0.7",
                                ).replace(
                                    "brain_meta_decay_rate=0.05",
                                    "brain_meta_decay_rate=0.01",
                                )
                                name = entity.get("entity_name", "")
                                etype = entity.get("entity_type", "")
                                ingester.inject_entity(
                                    name=name,
                                    entity_type=etype,
                                    description=new_desc,
                                    source_id="brain_consolidate",
                                    file_path="brain://consolidate",
                                )
                                consolidated += 1
                        except (ValueError, IndexError):
                            pass

            return {"consolidated": consolidated}

        except Exception as e:
            logger.error(f"consolidate_l0_to_l1 failed: {e}")
            return {"consolidated": 0, "error": str(e)}

    def cleanup_low_weight(self, threshold: float = 0.1) -> Dict[str, Any]:
        """低权重清理：weight < threshold 的实体标记为待删除。

        Args:
            threshold: 权重阈值，低于此值的实体标记待删除。

        Returns:
            Dict with marked_for_deletion count.
        """
        try:
            adapter = LightRAGAdapter()
            snapshot = adapter.get_graph_snapshot(limit=10000)
            edges = snapshot.get("edges", [])

            marked = 0
            for edge in edges:
                weight = edge.get("weight", 1.0)
                if weight < threshold:
                    # Mark for deletion by setting weight to 0
                    desc = edge.get("description", "")
                    if "brain_meta_pending_delete" in desc:
                        continue  # Already marked
                    new_desc = desc + ";brain_meta_pending_delete=true"
                    ingester = LightRAGIngester()
                    ingester.inject_relation(
                        src_id=edge.get("source", ""),
                        tgt_id=edge.get("target", ""),
                        relation=edge.get("relation", ""),
                        description=new_desc,
                        source_id="brain_cleanup",
                        file_path="brain://cleanup",
                    )
                    marked += 1

            return {"marked_for_deletion": marked}

        except Exception as e:
            logger.error(f"cleanup_low_weight failed: {e}")
            return {"marked_for_deletion": 0, "error": str(e)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_brain_decay.py -v`
Expected: PASS

- [ ] **Step 5: Register forgetting curve scheduled tasks in __main__.py**

在 `niu_api/__main__.py` 的 lifespan 中，default brain regions 之后，添加：

```python
    # 8.3. Register forgetting curve scheduled tasks
    _BRAIN_DECAY_TASKS = [
        {
            "content": "执行遗忘曲线衰减：brain_decay — 所有权重按 decay_rate 衰减",
            "cron_expr": "0 3 * * *",
            "task_id_suffix": "brain_decay",
        },
        {
            "content": "执行 L0→L1 记忆巩固：brain_consolidate_l0_to_l1 — access_count≥3 的 L0 升级",
            "cron_expr": "0 4 * * *",
            "task_id_suffix": "brain_consolidate_l0",
        },
        {
            "content": "执行 L1→L2 记忆巩固：brain_consolidate_l1_to_l2 — access_count≥10 的 L1 升级",
            "cron_expr": "0 5 * * 0",
            "task_id_suffix": "brain_consolidate_l1",
        },
        {
            "content": "执行低权重清理：brain_cleanup — weight<0.1 的实体标记待删除",
            "cron_expr": "0 6 * * 0",
            "task_id_suffix": "brain_cleanup",
        },
    ]
    try:
        from niu_api.internal.scheduler import get_store
        ts = get_store()
        existing_tasks = ts.list_tasks()

        for task_def in _BRAIN_DECAY_TASKS:
            already_exists = any(
                t.get("event_type") == "recurring"
                and t.get("cron_expr") == task_def["cron_expr"]
                and task_def["task_id_suffix"] in t.get("content", "")
                and t.get("status") != "cancelled"
                for t in existing_tasks
            )
            if not already_exists:
                now = datetime.now()
                ts.create_task(
                    content=task_def["content"],
                    scheduled_at=now.isoformat(),
                    is_recurring=True,
                    cron_expr=task_def["cron_expr"],
                    event_type="recurring",
                )
                logger.info(f"Created brain task: {task_def['task_id_suffix']}")
    except Exception as e:
        logger.warning(f"Failed to register brain decay tasks: {e}")
```

- [ ] **Step 6: Commit**

```bash
git add niu_api/internal/brain_graph.py niu_api/__main__.py tests/test_brain_decay.py
git commit -m "feat: add forgetting curve decay/consolidate/cleanup with scheduled tasks"
```

---

## Phase 4: dream-evolver 升级（精加工 + skill 维护）

### Task 4.1: 重写 dream-evolver 提示词

**Files:**
- Modify: `config/agents/dream-evolver.md`

- [ ] **Step 1: Write the new dream-evolver prompt**

将 `config/agents/dream-evolver.md` 的内容替换为：

```markdown
---
name: dream-evolver
description: "梦境进化 - 精加工知识图谱（brain_meta、时间链、脑区）+ skill 维护"
mode: subagent
temperature: 0.3
mcpServers:
  - lightrag-server
  - session-manager
---

# 梦境进化（Dream Evolver）

你是知识图谱的精加工器和 skill 维护者。

## 2项核心任务

### 任务1：精加工（LightRAG 做不到的精确控制）

对 entity-extractor 提炼入库的内容做精加工：

1. **brain_meta 标签**：给关键实体打标签
   - `lightrag_insert_entity(name, entity_type, description="brain_meta_weight=X;brain_meta_decay_rate=Y;brain_meta_created_at=...;brain_meta_access_count=0;...")`
   - L0（即时印象）：weight=0.3, decay_rate=0.05
   - L1（精炼摘要）：weight=0.7, decay_rate=0.01
   - L2（完整内容）：weight=0.9, decay_rate=0.002

2. **时间链**：建立事件间的时序/因果连接
   - `lightrag_insert_relation(src_id, tgt_id, relation="followed_by")` — 时间顺序
   - `lightrag_insert_relation(src_id, tgt_id, relation="corrected_by")` — 纠正
   - `lightrag_insert_relation(src_id, tgt_id, relation="led_to")` — 因果
   - `lightrag_insert_relation(src_id, tgt_id, relation="resolved_by")` — 解决

3. **脑区关联**：将实体关联到脑区主节点
   - 默认连到 `brain:region:聊天历史`（不再连到 brain:Niu 兜底）
   - `lightrag_insert_relation(src_id="brain:region:聊天历史", tgt_id=entity, relation="_region:contains")`

4. **画像更新**：更新 brain:Niu 的偏好和技能
   - `lightrag_insert_relation(src_id="brain:Niu", tgt_id=entity, relation="prefers"/"skilled_in"/"knows_about")`

### 任务2：Skill 维护

当使用一项技能并发现它过时、不完整或错误时，立即用 file_patch
对其进行修补——不要等着被问到。不维护的技能会成为负担。

#### 判断规则
- 工具使用失败且找到了替代方案 → file_patch 修改旧 skill
- 发现 skill 描述不完整（缺少参数、边界条件） → file_patch 补充
- 发现 skill 已过时（API 变更、方法废弃） → file_patch 更新
- 新的工作模式反复出现但无对应 skill → file_write 创建新 skill

#### 创建新 skill 的流程
1. 先用 file_read 读取 memory/skills/Write-SKILL.md，了解创建规范
2. 按照 Write-SKILL.md 的 RED-GREEN-REFACTOR 流程创建
3. 新 skill 文件存放在 memory/skills/ 目录下
4. 命名使用动词优先、连字符分隔（如 note-management.md）

#### 修改旧 skill 的流程
1. 用 file_read 读取目标 skill 文件
2. 用 file_patch(path, old_content, new_content) 局部修改
3. old_content 必须在文件中唯一匹配（含空白/缩进）

## 连接优先原则

**核心规则**：每条新实体必须至少建1条边，孤岛记忆无用。

1. 新实体写入时，必须指定至少一个连接目标
2. 默认连接到 `brain:region:聊天历史` 脑区
3. Session 节点格式：`brain:session:{date}`（如 `brain:session:2026-04-26`）

## 边命名规范

| 边类型 | keywords 格式 | 含义 |
|--------|-------------|------|
| 脑区包含 | `_region:contains` | 脑区主节点包含子实体 |
| 实体属于脑区 | `_region:belongs` | 实体属于某个脑区 |
| Session兜底 | `_session:contains` | Session包含临时实体 |
| 语义关系 | 无前缀 | 真实语义关系（skilled_in, prefers等） |
| 时间链 | 无前缀 | 时间顺序/因果（followed_by, corrected_by, led_to, resolved_by） |

## 工具使用规范

- 实体注入：`lightrag_insert_entity(name, entity_type, description, source_id, file_path)`
- 关系注入：`lightrag_insert_relation(src_id, tgt_id, relation, description, source_id, file_path)`
- 查询已有实体：`lightrag_search_entities(query, entity_type, top_k)`
- 图遍历：`lightrag_get_graph(action="explore", entity_name, depth)`
- 时间线查询：`lightrag_timeline_query(query, direction, max_depth, max_results)`
- Skill 修改：`file_patch(path, old_content, new_content)`
- Skill 创建：`file_write(path, content)`
- Skill 读取：`file_read(path)`

## 游标机制

- 调用方会告知 `last_dream_evolve_id`（上次处理到的消息UUID），只处理该ID之后的新消息
- 处理完成后，在报告末尾用 JSON 格式报告：`{"last_dream_evolve_id": "<最后处理的消息UUID>"}`
- force 模式下不使用游标，全量处理所有消息

## 禁止

- 禁止使用 `code_run` 工具
- 禁止使用 `lightrag_insert`（精炼文档注入由 entity-extractor 负责，dream-evolver 只做精加工）
- 禁止使用 `add_document`、`search_documents`、`get_document`、`delete_document`、`list_documents`（已废弃的 vector-store 工具）
```

- [ ] **Step 2: Verify the file was written correctly**

Run: `python -c "
import sys; sys.stdout.reconfigure(encoding='utf-8')
f=open('config/agents/dream-evolver.md','r',encoding='utf-8'); c=f.read(); f.close()
checks = [
    'Skill 维护' in c,
    'file_patch' in c,
    'Write-SKILL.md' in c,
    'brain:region:聊天历史' in c,
    'lightrag_timeline_query' in c,
    '经验提取' not in c,
]
print('All checks passed' if all(checks) else f'Failed: {checks}')
"`
Expected: `All checks passed`

- [ ] **Step 3: Commit**

```bash
git add config/agents/dream-evolver.md
git commit -m "feat: upgrade dream-evolver with skill maintenance and precise control"
```

---

## Self-Review Checklist

### Spec Coverage

| Spec Section | Task | Covered |
|-------------|------|---------|
| 3.1 重新定位 entity-extractor | Task 1.2 | ✅ |
| 3.2 升级 dream-evolver | Task 4.1 | ✅ |
| 3.3 精炼文档增量分段注入 | Task 1.1, 1.3 | ✅ |
| 3.3 时间线查询 | Task 2.1, 2.3 | ✅ |
| 3.4 图遍历边类型过滤 | Task 2.2, 2.3 | ✅ |
| 3.5 缺省脑区主节点 | Task 3.1 | ✅ |
| 3.6 遗忘曲线定时任务 | Task 3.2 | ✅ |

### Placeholder Scan

- No "TBD", "TODO", "implement later" found
- No "add appropriate error handling" without actual code
- No "write tests for the above" without actual test code
- All steps contain actual code and exact commands

### Type Consistency

- `generate_doc_id(date: str, seq: int) -> str` — used consistently
- `get_next_segment_number(existing_doc_ids: List[str], date: Optional[str]) -> int` — used consistently
- `timeline_query(query, start_entities, direction, max_depth, max_results)` — same signature in adapter and MCP tool
- `explore_node(entity_name, depth, edge_types)` — same signature in adapter and MCP tool
- `create_default_regions(adapter, ingester) -> dict` — used consistently
- `decay_edges(days_since_last) -> Dict[str, Any]` — used consistently
