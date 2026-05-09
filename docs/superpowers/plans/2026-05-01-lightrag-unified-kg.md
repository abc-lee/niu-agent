# LightRAG 统一知识管理架构重构 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将所有 KG/向量库操作统一到 LightRAG 原生 API，删除自管理的 vector-store、kg-server 和自定义图操作代码。

**Architecture:** photo-server 的 KG 操作改用 lightrag-server 的 insert_custom_kg/edit_entity/merge_entities；便签从逐条注入改为整文件 ainsert；脑区激活改用 LightRAG 图检索；删除废弃服务器和引用。

**Tech Stack:** Python, LightRAG (lightrag-hku==1.4.15), MCP ToolRegistry

---

## 文件结构

| 操作 | 文件 | 职责 |
|------|------|------|
| 修改 | `mcp-servers/photo-server/src/niu_photo_server/__init__.py` | KG 操作改用 LightRAG API |
| 修改 | `agent/injector/sync.py` | 便签改为整文件 ainsert |
| 修改 | `agent/generic/runner.py` | 动态注入改用 LightRAG 图检索 |
| 修改 | `agent/handler.py` | 清理 vector-store/kg-server 别名 |
| 修改 | `config/agents/file-processor.md` | 简化文档处理流程 |
| 修改 | `config/mcp-servers.yaml` | 删除 vector-store/kg-server 配置 |
| 删除 | `mcp-servers/vector-store/` | 废弃服务器 |
| 删除 | `mcp-servers/kg-server/` | 废弃服务器 |
| 删除 | `agent/vector_search.py` | 废弃适配器 |
| 创建 | `tests/test_lightrag_unified.py` | 集成测试 |

---

## Task 1: sync_photo_to_kg — 人物实体不挂 file_path，description 只写名字

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:461-467`
- Test: `tests/test_lightrag_unified.py`

**背景**: 当前 `sync_photo_to_kg` 创建人物实体时，description 包含 "detected in photo: {title}"，且挂了 file_path。改造后人物实体只存人物信息，不挂文件路径。

- [ ] **Step 1: 写失败测试**

```python
def test_sync_photo_to_kg_person_entity_no_file_path():
    """人物实体不应挂 file_path，description 只写名字"""
    from unittest.mock import MagicMock, patch

    entities = []
    relations = []

    # 模拟 sync_photo_to_kg 构建实体列表的逻辑
    person_name = "任飞"
    entity_name = f"person:test-uuid"
    file_path = "/photos/test.jpg"
    title = "test photo"

    # 期望的实体结构
    expected_entity = {
        "entity_name": entity_name,
        "entity_type": "Person",
        "description": person_name,
    }

    # 不应包含的字段
    assert "file_path" not in expected_entity
    assert "source_id" not in expected_entity
    assert expected_entity["description"] == "任飞"
    assert expected_entity["description"] != f"{person_name}, detected in photo: {title}"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_sync_photo_to_kg_person_entity_no_file_path -v`
Expected: PASS（此测试验证期望结构，不依赖实际代码。需改为验证实际代码输出）

**修正**：测试需要验证实际代码行为，用 mock 调用实际函数。

```python
def test_sync_photo_to_kg_person_entity_no_file_path():
    """人物实体不应挂 file_path，description 只写名字"""
    from unittest.mock import MagicMock, patch

    with patch("niu_photo_server.get_lightrag") as mock_get_rag, \
         patch("niu_photo_server.get_face_analyzer") as mock_face:
        mock_rag = MagicMock()
        mock_get_rag.return_value = mock_rag

        # 调用 sync_photo_to_kg
        # 验证传给 insert_custom_kg 的 entities 中：
        # 1. Person 类型实体没有 file_path
        # 2. Person 类型实体 description 只写名字
        pass  # 具体实现取决于函数签名
```

**实际方案**：由于 `sync_photo_to_kg` 是内部函数，直接验证其构建的 entities 列表。先读取函数签名再补全测试。

- [ ] **Step 3: 修改 sync_photo_to_kg 中的实体构建逻辑**

在 `mcp-servers/photo-server/src/niu_photo_server/__init__.py` 中，找到 `sync_photo_to_kg` 函数内构建人物实体的代码（约 L461-467）：

```python
# 旧代码:
entities.append({
    "entity_name": entity_name,
    "entity_type": "Person",
    "description": f"{person_name}, detected in photo: {title}",
    "source_id": "photo",
    "file_path": file_path,
})

# 新代码:
entities.append({
    "entity_name": entity_name,
    "entity_type": "Person",
    "description": person_name,
})
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_sync_photo_to_kg_person_entity_no_file_path -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py tests/test_lightrag_unified.py
git commit -m "refactor: sync_photo_to_kg 人物实体不挂 file_path，description 只写名字"
```

---

## Task 2: name_person — description 只写名字，entity_type="Person"

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:1857-1861`
- Test: `tests/test_lightrag_unified.py`

**背景**: 当前 `name_person` 调用 `inject_entity` 时 entity_type="person"（小写），description="Renamed to: {name}"。改造后用 lightrag-server 的 edit_entity，entity_type="Person"（大写），description=name。

- [ ] **Step 1: 写失败测试**

```python
def test_name_person_uses_edit_entity():
    """name_person 应调用 lightrag-server/edit_entity，不用 inject_entity"""
    from unittest.mock import MagicMock, patch, call

    with patch("niu_photo_server.get_tool_registry") as mock_registry_fn:
        mock_registry = MagicMock()
        mock_registry_fn.return_value = mock_registry
        mock_edit = MagicMock(return_value={"status": "success"})
        mock_registry.get.return_value = mock_edit

        # 调用 name_person
        # 验证调用的是 edit_entity 而非 inject_entity
        # 验证参数: entity_type="Person", description=name, file_path=""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_name_person_uses_edit_entity -v`
Expected: FAIL（当前代码调用 inject_entity）

- [ ] **Step 3: 修改 name_person 的 KG 同步代码**

在 `mcp-servers/photo-server/src/niu_photo_server/__init__.py` 中，找到 `name_person` 函数内的 KG 同步部分（约 L1857-1861）：

```python
# 旧代码:
ingester.inject_entity(
    name=f"person:{person_id}",
    entity_type="person",
    description=f"Renamed to: {name}",
)

# 新代码:
from agent.tool_registry import get_registry

registry = get_registry()
edit_entity = registry.get("lightrag-server/edit_entity")
edit_entity(
    entity_name=f"person:{person_id}",
    entity_type="Person",
    description=name,
    file_path="",
)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_name_person_uses_edit_entity -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py tests/test_lightrag_unified.py
git commit -m "refactor: name_person 改用 lightrag-server/edit_entity"
```

---

## Task 3: merge_persons — 合并后删除 person_b，迁移边

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:2025-2044`
- Test: `tests/test_lightrag_unified.py`

**背景**: 当前 `merge_persons` 只建 merged_into 边，不迁移边，不删节点。改造后用 lightrag-server/merge_entities。

- [ ] **Step 1: 写失败测试**

```python
def test_merge_persons_uses_merge_entities():
    """merge_persons 应调用 lightrag-server/merge_entities"""
    from unittest.mock import MagicMock, patch

    with patch("niu_photo_server.get_tool_registry") as mock_registry_fn:
        mock_registry = MagicMock()
        mock_registry_fn.return_value = mock_registry
        mock_merge = MagicMock(return_value={"status": "success"})
        mock_registry.get.return_value = mock_merge

        # 调用 merge_persons
        # 验证调用的是 merge_entities
        # 验证参数: source=person:{b_id}, target=person:{a_id}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_merge_persons_uses_merge_entities -v`
Expected: FAIL

- [ ] **Step 3: 修改 merge_persons 的 KG 同步代码**

在 `mcp-servers/photo-server/src/niu_photo_server/__init__.py` 中，找到 `merge_persons` 函数内的 KG 同步部分（约 L2025-2044）：

```python
# 旧代码:
ingester.inject_entity(
    name=f"person:{person_a_id}",
    entity_type="person",
    description=f"Merged with {person_b_id}, name: {merged_name}",
)
ingester.inject_relation(
    src_id=f"person:{person_b_id}",
    tgt_id=f"person:{person_a_id}",
    relation="merged_into",
    description=f"Person {person_b_id} merged into {person_a_id}",
)

# 新代码:
from agent.tool_registry import get_registry

registry = get_registry()

# 1. 更新 person_a 的名字
edit_entity = registry.get("lightrag-server/edit_entity")
edit_entity(
    entity_name=f"person:{person_a_id}",
    entity_type="Person",
    description=merged_name,
    file_path="",
)

# 2. 合并: person_b 的边迁移到 person_a，然后删除 person_b
merge_entities = registry.get("lightrag-server/merge_entities")
merge_entities(
    source_entity=f"person:{person_b_id}",
    target_entity=f"person:{person_a_id}",
)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_merge_persons_uses_merge_entities -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py tests/test_lightrag_unified.py
git commit -m "refactor: merge_persons 改用 lightrag-server/merge_entities"
```

---

## Task 4: 删除 LightRAGIngester.inject_entity/inject_relation 自实现

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py`
- Test: `tests/test_lightrag_unified.py`

**背景**: Task 1-3 完成后，`LightRAGIngester.inject_entity` 和 `inject_relation` 不再被调用。删除这两个方法，以及 `LightRAGIngester` 类中所有直接操作 NetworkX 图的代码。

- [ ] **Step 1: 写失败测试**

```python
def test_no_direct_networkx_operations():
    """photo-server 不应直接操作 NetworkX 图"""
    import inspect
    from niu_photo_server import LightRAGIngester

    source = inspect.getsource(LightRAGIngester)
    # 不应包含直接操作 NetworkX 的代码
    assert "nx.add_node" not in source
    assert "nx.add_edge" not in source
    assert "_graph.add_node" not in source
    assert "_graph.add_edge" not in source
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_no_direct_networkx_operations -v`
Expected: FAIL（当前代码包含 NetworkX 操作）

- [ ] **Step 3: 删除 LightRAGIngester 中的 inject_entity 和 inject_relation 方法**

在 `mcp-servers/photo-server/src/niu_photo_server/__init__.py` 中：

1. 删除 `LightRAGIngester.inject_entity` 方法（约 L440-460）
2. 删除 `LightRAGIngester.inject_relation` 方法（约 L462-490）
3. 如果 `LightRAGIngester` 类只剩空壳，考虑删除整个类
4. 清理所有对 `ingester.inject_entity` 和 `ingester.inject_relation` 的调用（Task 1-3 已处理了主要调用点，检查是否还有遗漏）

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py -v`
Expected: ALL PASS

- [ ] **Step 5: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py tests/test_lightrag_unified.py
git commit -m "refactor: 删除 LightRAGIngester 自实现的 inject_entity/inject_relation"
```

---

## Task 5: sync_photo_to_kg 改用 insert_custom_kg

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py`
- Test: `tests/test_lightrag_unified.py`

**背景**: 当前 `sync_photo_to_kg` 通过 `LightRAGIngester` 直接操作 NetworkX。改造后改用 lightrag-server 的 `insert_custom_kg` 工具。

- [ ] **Step 1: 写失败测试**

```python
def test_sync_photo_to_kg_uses_insert_custom_kg():
    """sync_photo_to_kg 应调用 lightrag-server/insert_custom_kg"""
    from unittest.mock import MagicMock, patch

    with patch("niu_photo_server.get_tool_registry") as mock_registry_fn:
        mock_registry = MagicMock()
        mock_registry_fn.return_value = mock_registry
        mock_insert = MagicMock(return_value={"status": "success"})
        mock_registry.get.return_value = mock_insert

        # 调用 sync_photo_to_kg
        # 验证调用的是 insert_custom_kg
        # 验证 entities 中 Person 类型没有 file_path
        # 验证 relations 包含 depicts 关系
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_sync_photo_to_kg_uses_insert_custom_kg -v`
Expected: FAIL

- [ ] **Step 3: 重写 sync_photo_to_kg**

在 `mcp-servers/photo-server/src/niu_photo_server/__init__.py` 中，重写 `sync_photo_to_kg` 函数：

```python
def sync_photo_to_kg(file_path: str, title: str, faces: list[dict]) -> dict:
    """同步照片信息到知识图谱，使用 lightrag-server/insert_custom_kg"""
    from agent.tool_registry import get_registry

    entities = []
    relations = []

    # 照片实体
    entities.append({
        "entity_name": file_path,
        "entity_type": "Photo",
        "description": f"照片: {title}",
        "file_path": file_path,
    })

    # 人物实体 + depicts 关系
    for face in faces:
        person_id = face.get("person_id")
        person_name = face.get("person_name", "未知")
        if person_id:
            entity_name = f"person:{person_id}"
            entities.append({
                "entity_name": entity_name,
                "entity_type": "Person",
                "description": person_name,
            })
            relations.append({
                "src_id": file_path,
                "tgt_id": entity_name,
                "relation": "depicts",
                "description": f"照片 {title} 中有 {person_name}",
            })

    registry = get_registry()
    insert_custom_kg = registry.get("lightrag-server/insert_custom_kg")
    return insert_custom_kg(entities=entities, relations=relations)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_sync_photo_to_kg_uses_insert_custom_kg -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py tests/test_lightrag_unified.py
git commit -m "refactor: sync_photo_to_kg 改用 lightrag-server/insert_custom_kg"
```

---

## Task 6: ingest_document — 全文 ainsert，不返回 need_l1

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:2448-2566`
- Test: `tests/test_lightrag_unified.py`

**背景**: 当前 `ingest_document` 返回 `need_l1`，让子Agent生成 L1 摘要。改造后程序自动完成所有步骤，不再返回 need_l1。

- [ ] **Step 1: 写失败测试**

```python
def test_ingest_document_no_need_l1():
    """ingest_document 不应返回 need_l1"""
    from unittest.mock import MagicMock, patch

    with patch("niu_photo_server.get_tool_registry") as mock_registry_fn:
        mock_registry = MagicMock()
        mock_registry_fn.return_value = mock_registry

        # mock lightrag-server/insert 工具
        mock_insert = MagicMock(return_value={"status": "success"})
        mock_registry.get.return_value = mock_insert

        # 调用 ingest_document(file_path="test.txt", mode="copy")
        # 验证返回值中没有 need_l1
        # 验证调用了 lightrag-server/insert（全文 ainsert）
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_ingest_document_no_need_l1 -v`
Expected: FAIL

- [ ] **Step 3: 修改 ingest_document**

在 `mcp-servers/photo-server/src/niu_photo_server/__init__.py` 中，修改 `ingest_document` 函数：

1. 删除 `need_l1` 返回字段
2. 删除对 vector-store/kg-server 的调用
3. 文件搬运完成后，自动调用 `lightrag-server/insert` 做全文 ainsert
4. 返回值改为：

```python
return {
    "status": "success",
    "action": action,
    "file_path": str(Path(final_path).resolve()),
    "original_path": str(source),
    "category": category,
    "content_length": len(content) if content else 0,
}
```

5. 自动判断文件类型（目录/照片/文档），如果是文档：
   - 读文件内容（限制 <20K，超出截断）
   - 全文传给 `lightrag-server/insert`
   - 不截断，LightRAG 内部自动分块

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_ingest_document_no_need_l1 -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py tests/test_lightrag_unified.py
git commit -m "refactor: ingest_document 全文 ainsert，不再返回 need_l1"
```

---

## Task 7: 便签入库改为整文件 ainsert

**Files:**
- Modify: `agent/injector/sync.py:368-466`
- Test: `tests/test_lightrag_unified.py`

**背景**: 当前 `_inject_note_to_lightrag` 逐条 inject_entity。改造后改为整文件 ainsert，让 LightRAG 自动提取实体和关系。

- [ ] **Step 1: 写失败测试**

```python
def test_note_sync_uses_ainsert():
    """便签同步应使用整文件 ainsert，不逐条 inject_entity"""
    from unittest.mock import MagicMock, patch

    with patch("agent.injector.sync.get_tool_registry") as mock_registry_fn:
        mock_registry = MagicMock()
        mock_registry_fn.return_value = mock_registry
        mock_insert = MagicMock(return_value={"status": "success"})
        mock_registry.get.return_value = mock_insert

        # 调用 _inject_note_to_lightrag
        # 验证调用的是 lightrag-server/insert（全文）
        # 验证不是逐条调用 inject_entity
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_note_sync_uses_ainsert -v`
Expected: FAIL

- [ ] **Step 3: 重写 _inject_note_to_lightrag**

在 `agent/injector/sync.py` 中，重写 `_inject_note_to_lightrag` 函数：

```python
def _inject_note_to_lightrag(notes_data: list[dict]) -> None:
    """将便签 JSON 整文件传给 LightRAG ainsert"""
    import json
    from agent.tool_registry import get_registry

    content = json.dumps(notes_data, ensure_ascii=False, indent=2)
    registry = get_registry()
    insert_tool = registry.get("lightrag-server/insert")
    insert_tool(content=content, source="notes")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_note_sync_uses_ainsert -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent/injector/sync.py tests/test_lightrag_unified.py
git commit -m "refactor: 便签入库改为整文件 ainsert"
```

---

## Task 8: 动态注入改用 LightRAG 图检索

**Files:**
- Modify: `agent/generic/runner.py`
- Test: `tests/test_lightrag_unified.py`

**背景**: 当前 `_inject_dynamic_resources` 用 ChromaDB 向量检索。改造后改用 `lightrag-server/query` (mode="local") 做图检索。

- [ ] **Step 1: 写失败测试**

```python
def test_dynamic_injection_uses_lightrag_query():
    """动态注入应使用 lightrag-server/query 而非 ChromaDB"""
    from unittest.mock import MagicMock, patch

    with patch("agent.generic.runner.get_tool_registry") as mock_registry_fn:
        mock_registry = MagicMock()
        mock_registry_fn.return_value = mock_registry
        mock_query = MagicMock(return_value={"status": "success", "response": "..."})
        mock_registry.get.return_value = mock_query

        # 调用 _inject_dynamic_resources
        # 验证调用的是 lightrag-server/query
        # 验证 mode="local"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_dynamic_injection_uses_lightrag_query -v`
Expected: FAIL

- [ ] **Step 3: 修改 _inject_dynamic_resources**

在 `agent/generic/runner.py` 中，修改 `_inject_dynamic_resources`：

1. 删除对 `vector_search` 的导入和使用
2. 改用 `lightrag-server/query` (mode="local") 做图检索
3. 解析返回结果，提取相关工具描述注入上下文

```python
def _inject_dynamic_resources(self, query: str) -> list[dict]:
    """用 LightRAG 图检索替代 ChromaDB 向量检索"""
    from agent.tool_registry import get_registry

    registry = get_registry()
    query_tool = registry.get("lightrag-server/query")
    result = query_tool(query=query, mode="local")

    if result and result.get("status") == "success":
        # 解析返回的实体和关系，提取相关工具
        # 注入到上下文
        pass

    return []
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_dynamic_injection_uses_lightrag_query -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent/generic/runner.py tests/test_lightrag_unified.py
git commit -m "refactor: 动态注入改用 LightRAG 图检索"
```

---

## Task 9: 清理 handler.py 中的 vector-store/kg-server 别名

**Files:**
- Modify: `agent/handler.py`
- Test: `tests/test_lightrag_unified.py`

**背景**: `_TOOL_ALIASES` 中有 28+ 个从 vector-store/kg-server 到 lightrag-server 的别名映射。删除废弃服务器后，这些别名不再需要。

- [ ] **Step 1: 写失败测试**

```python
def test_handler_no_vector_store_kg_server_aliases():
    """handler.py 不应包含 vector-store/kg-server 别名"""
    import inspect
    from agent.handler import GenericHandler

    # 获取 _TOOL_ALIASES
    aliases = GenericHandler._TOOL_ALIASES  # 或通过其他方式获取

    # 不应包含 vector-store 或 kg-server 的别名
    for key in aliases:
        assert not key.startswith("vector-store/"), f"Found alias: {key}"
        assert not key.startswith("kg-server/"), f"Found alias: {key}"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_handler_no_vector_store_kg_server_aliases -v`
Expected: FAIL

- [ ] **Step 3: 删除 handler.py 中的废弃别名**

在 `agent/handler.py` 中，找到 `_TOOL_ALIASES` 字典，删除所有 key 以 `vector-store/` 或 `kg-server/` 开头的条目。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_handler_no_vector_store_kg_server_aliases -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent/handler.py tests/test_lightrag_unified.py
git commit -m "refactor: 清理 handler.py 中 vector-store/kg-server 别名"
```

---

## Task 10: 删除废弃服务器和引用

**Files:**
- Delete: `mcp-servers/vector-store/`
- Delete: `mcp-servers/kg-server/`
- Delete: `agent/vector_search.py`
- Modify: `config/mcp-servers.yaml`
- Modify: `agent/generic/runner.py`（清理 vector_search 导入）
- Test: `tests/test_lightrag_unified.py`

**背景**: 所有功能已迁移到 LightRAG，删除废弃的 vector-store、kg-server 和 vector_search.py。

- [ ] **Step 1: 写失败测试**

```python
def test_no_vector_store_kg_server_references():
    """代码中不应引用 vector-store/kg-server/vector_search"""
    import subprocess

    # 搜索所有 Python 文件中的引用
    result = subprocess.run(
        ["grep", "-r", "vector_search", "agent/"],
        capture_output=True, text=True
    )
    assert result.returncode != 0, f"Found vector_search references:\n{result.stdout}"

    result = subprocess.run(
        ["grep", "-r", "kg-server", "config/"],
        capture_output=True, text=True
    )
    assert result.returncode != 0, f"Found kg-server references:\n{result.stdout}"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py::test_no_vector_store_kg_server_references -v`
Expected: FAIL

- [ ] **Step 3: 执行删除**

```bash
# 删除废弃服务器目录
rm -rf mcp-servers/vector-store/
rm -rf mcp-servers/kg-server/

# 删除废弃适配器
rm agent/vector_search.py
```

- [ ] **Step 4: 清理 config/mcp-servers.yaml**

删除 vector-store 和 kg-server 的配置条目。

- [ ] **Step 5: 清理 runner.py 中的 vector_search 导入**

删除 `from agent.vector_search import ...` 及其所有使用。

- [ ] **Step 6: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py -v`
Expected: ALL PASS

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "refactor: 删除废弃的 vector-store、kg-server 和 vector_search"
```

---

## Task 11: 更新 file-processor.md 提示词

**Files:**
- Modify: `config/agents/file-processor.md`

**背景**: `ingest_document` 不再返回 `need_l1`，子Agent提示词需要简化。

- [ ] **Step 1: 修改 file-processor.md**

简化文档处理流程，删除 `need_l1` 相关步骤：

```markdown
## 文档处理

### 入库
调用 `photo-server/ingest_document`，参数: path, mode="copy"

ingest_document 自动完成：文件搬运 + LightRAG 全文入库（自动提取实体和建链）。

| status | 含义 | 下一步 |
|--------|------|--------|
| `success` | 处理完成 | **结束，直接汇报** |
| `error` | 失败 | 报告错误 |
```

- [ ] **Step 2: 提交**

```bash
git add config/agents/file-processor.md
git commit -m "docs: 简化 file-processor 提示词，删除 need_l1 流程"
```

---

## Task 12: 集成测试 — 端到端验证

**Files:**
- Test: `tests/test_lightrag_unified.py`

**背景**: 所有改造完成后，运行集成测试验证完整流程。

- [ ] **Step 1: 写集成测试**

```python
def test_full_document_ingest_flow():
    """完整文档入库流程：ingest_document → ainsert → 无 need_l1"""
    # 1. mock lightrag-server/insert
    # 2. 调用 ingest_document
    # 3. 验证返回值无 need_l1
    # 4. 验证调用了 lightrag-server/insert

def test_full_photo_ingest_flow():
    """完整照片入库流程：照片 → 人脸识别 → insert_custom_kg"""
    # 1. mock lightrag-server/insert_custom_kg
    # 2. 调用 ingest_document (照片)
    # 3. 验证 Person 实体无 file_path
    # 4. 验证 depicts 关系

def test_full_merge_persons_flow():
    """完整合并流程：edit_entity + merge_entities"""
    # 1. mock lightrag-server/edit_entity + merge_entities
    # 2. 调用 merge_persons
    # 3. 验证调用顺序和参数
```

- [ ] **Step 2: 运行所有测试**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_unified.py -v`
Expected: ALL PASS

- [ ] **Step 3: 提交**

```bash
git add tests/test_lightrag_unified.py
git commit -m "test: 添加 LightRAG 统一知识管理集成测试"
```

---

## 实施顺序总结

| Task | 内容 | 依赖 | 风险 |
|------|------|------|------|
| 1 | sync_photo_to_kg 人物实体不挂 file_path | 无 | 低 |
| 2 | name_person 改用 edit_entity | 无 | 低 |
| 3 | merge_persons 改用 merge_entities | 无 | 中（需测试合并策略） |
| 4 | 删除 LightRAGIngester 自实现 | 1,2,3 | 低 |
| 5 | sync_photo_to_kg 改用 insert_custom_kg | 4 | 低 |
| 6 | ingest_document 全文 ainsert | 无 | 中（流程变更较大） |
| 7 | 便签整文件 ainsert | 无 | 低 |
| 8 | 动态注入改用 LightRAG 图检索 | 无 | 中（需测试检索质量） |
| 9 | 清理 handler.py 别名 | 无 | 低 |
| 10 | 删除废弃服务器和引用 | 8,9 | 低 |
| 11 | 更新 file-processor.md | 6 | 低 |
| 12 | 集成测试 | 全部 | 低 |

**可并行的 Task 组**:
- 组 A: Task 1, 2, 3（photo-server KG 操作，互不依赖）
- 组 B: Task 6, 7, 8（不同文件的独立修改）
- 组 C: Task 9, 11（清理和文档）
- 组 D: Task 4, 5（依赖组 A 完成）
- 组 E: Task 10（依赖组 B, C 完成）
- 组 F: Task 12（依赖全部完成）

---

## C1 代码审查结论：inject_custom_kg 与 lightrag_insert 互补而非替代

> 2026-05-07 追加。经过4轮代码审查，发现原方案中"将所有 KG 入库路径从 inject_custom_kg 迁移到 lightrag_insert"的假设是错误的。两种方法服务于根本不同的目的，必须共存。

### 核心结论

**`inject_custom_kg`（ainsert_custom_kg）和 `lightrag_insert`（ainsert）是互补的，不是替代关系。**

| 方法 | LightRAG API | 机制 | 适用场景 |
|------|-------------|------|---------|
| `lightrag_insert` | `ainsert()` | LLM 自动提取实体/关系，同名实体合并，自动建边 | 自然语言内容（文档、便签、对话） |
| `inject_custom_kg` | `ainsert_custom_kg()` | 手动指定实体/关系/chunks，精确控制名称和属性 | 程序化结构图操作（脑区、人物、照片） |

### 内部调用者分析

| 调用者 | 文件 | 使用方式 | 是否应迁移 |
|--------|------|---------|-----------|
| `brain_graph.ensure_niu_entity()` | `niu_api/internal/brain_graph.py` | 创建 brain:Niu 根实体，精确命名 | **否** — 必须精确控制实体名 |
| `brain_graph.store_memory()` | `niu_api/internal/brain_graph.py` | 存储脑记忆，精确关系类型+权重+metadata | **否** — 必须精确控制关系属性 |
| `region_manager.create_region_nodes()` | `niu_api/internal/region_manager.py` | 创建脑区主节点，精确命名+brain_meta_* | **否** — 程序化结构图 |
| `region_manager.update_region_summaries()` | `niu_api/internal/region_manager.py` | 更新脑区摘要，brain_meta_* 格式 | **否** — 必须精确控制属性 |
| `region_manager.dissolve()` ×2 | `niu_api/internal/region_manager.py` | 解散脑区，精确关系 keywords/weights | **否** — 必须精确控制关系 |
| `region_manager.create_default_regions()` | `niu_api/internal/region_manager.py` | 创建默认脑区，程序化结构图 | **否** — 程序化结构图 |
| `notes_api.sync_note_to_lightrag()` | `niu_api/internal/notes_api.py` | 便签自然语言内容入库 | **是** — 自然语言，适合 ainsert |
| `photo-server` 各种函数 | `mcp-servers/photo-server/` | 人物/照片实体，精确命名 | **否** — 必须精确控制实体名 |

**结论：8个内部调用者中，7个必须保留 inject_custom_kg，仅1个（notes_api）适合迁移到 lightrag_insert。**

### 方案修正

1. **inject_custom_kg 不应标记 DEPRECATED** — 它服务于程序化结构图操作，与 lightrag_insert 互补
2. **需要回退的错误修改**：
   - 删除 `inject_custom_kg` 的 `warnings.warn()` 调用
   - 删除类/模块 docstring 中标记 inject_custom_kg 为 DEPRECATED 的文字
3. **唯一应迁移的调用者**：`notes_api.sync_note_to_lightrag()` 改用 `lightrag_insert`
4. **lightrag-server MCP 工具描述需更新**：明确说明何时用 `insert_custom_kg`（程序化精确控制），何时用 `insert`（自然语言自动提取）

### 未解决的核心问题：非结构化节点与脑区自动建边

当 `lightrag_insert`（ainsert）创建节点时，LLM 自动提取实体和关系。但这些节点与 `inject_custom_kg` 创建的脑区节点之间**没有显式连接**。

**问题本质**：两个世界的桥梁问题
- 世界A：程序化结构图（脑区主节点、brain:Niu、belongs_to 关系） — 由 inject_custom_kg 精确控制
- 世界B：非结构化知识（文档/便签/对话提取的实体和关系） — 由 lightrag_insert LLM 自动提取

**可能的建边机制**（待深入分析）：

1. **LightRAG 实体合并**：如果 ainsert 提取出的实体名与已有实体名完全匹配，LightRAG 的 `_merge_nodes_then_upsert` 会自动合并描述，边自然继承
2. **brain:Niu 锚点模式**：lightrag_insert 的文本中包含 `brain:Niu {relation} {name}` 格式，LLM 提取时可能识别为关系
3. **Leiden 社区检测**：非结构化节点通过边与已有节点连接后，社区检测会自动将其归入相应脑区，脑区主节点随之建立 belongs_to 关系
4. **后置建边步骤**：ainsert 完成后，显式查询新创建的实体，为其建立与相关脑区的连接

详见下方章节。

---

## 非结构化节点与脑区自动建边机制分析

> 2026-05-07 追加。本节深入分析当 `lightrag_insert`（ainsert）创建的节点如何自动与 `inject_custom_kg` 创建的脑区节点建立连接。

### 问题定义

**两个世界**：

| 世界 | 创建方式 | 节点示例 | 关系示例 |
|------|---------|---------|---------|
| 世界A：程序化结构图 | `inject_custom_kg` | `brain:Niu`, `brain:region:编程开发`, `brain:Skill:Python` | `brain_region_anchor`, `_region:contains`, `skilled_in` |
| 世界B：非结构化知识 | `lightrag_insert` (ainsert) | `Python`, `Django`, `数据分析` | `USED_FOR`, `associated_with` |

**核心问题**：世界B的节点如何与世界A的脑区节点建立连接？

### 建边机制分析（4条路径，按可靠性排序）

#### 路径1：LightRAG 实体合并（最可靠，但条件最严格）

**机制**：ainsert 的 LLM 提取出实体名后，如果与图谱中已有实体名**精确匹配**，`_merge_nodes_then_upsert` 会自动合并描述，已有边自然继承。

**条件**：LLM 提取的实体名 == 已有实体名（精确字符串匹配，无模糊匹配）

**实际效果分析**：

| 已有实体名（世界A） | LLM 可能提取的名称 | 是否匹配 | 原因 |
|---------------------|-------------------|---------|------|
| `brain:Niu` | `brain:Niu` | **是** | 锚点文本中显式出现 |
| `brain:Skill:Python` | `Python` | **否** | LLM 不会自动加 `brain:Skill:` 前缀 |
| `brain:region:编程开发` | `编程开发` | **否** | LLM 不会自动加 `brain:region:` 前缀 |
| `Python` | `Python` | **是** | 如果世界A也用裸名创建实体 |
| `Python` | `Python语言` | **否** | LLM 命名不一致 |

**结论**：实体合并**只能保证 brain:Niu 的匹配**（因为锚点文本中显式出现），对其他实体名无法保证匹配。

**关键发现**：brain_graph.py 的 `store_memory()` 创建的实体名是 `brain:{EntityType}:{label}` 格式（如 `brain:Skill:Python`），而 LLM 提取的实体名是裸名（如 `Python`）。**两者永远不会精确匹配**，因此实体合并路径对 brain_graph 创建的实体无效。

#### 路径2：brain:Niu 锚点模式（当前已实现，部分有效）

**机制**：`lightrag_insert` 的文本中包含 `brain:Niu {relation} {name}` 格式。LLM 提取时，如果识别出 `brain:Niu` 和 `{name}` 两个实体以及它们之间的关系，就会在图谱中建立连接。

**当前包含锚点的调用点**：

| 调用点 | 锚点格式 | 关系类型 |
|--------|---------|---------|
| DreamWriter.write_semantic_entity | `brain:Niu {relation} {name}。` | remembers/skilled_in/knows_about/uses |
| DreamWriter.write_episodic_event | `brain:Niu experienced brain:event:{name}。` | experienced |
| lightrag_insert_entity MCP 工具 | `brain:Niu {relation} {name}。` | remembers/skilled_in/knows_about/uses |
| upsert_interaction_habit | `brain:Niu uses {entity_name}。` | uses |
| SkillSync._inject_skill_to_lightrag | `brain:Niu skilled_in {name}。` | skilled_in |
| lightrag_sync._sync_skills_and_tools | `brain:Niu skilled_in {name}。` | skilled_in |

**当前缺少锚点的调用点**：

| 调用点 | 影响 |
|--------|------|
| lightrag_sync._sync_photos_db | 照片/人物实体在图谱中孤立，无法从 brain:Niu 导航到达 |
| photo-server.sync_photo_to_kg | 同上 |
| photo-server.sync_video_to_kg | 视频实体孤立 |
| SkillSync._inject_note_to_lightrag | 便签实体孤立 |
| DreamWriter.write_semantic_relation | 关系不连 brain:Niu（设计如此，关系只需连接两端实体） |
| lightrag_insert_relation MCP 工具 | 同上 |

**锚点的实际效果**：LLM 提取 `brain:Niu skilled_in Python` 时，会：
1. 提取出实体 `brain:Niu`（与已有节点精确匹配 → 合并）
2. 提取出实体 `Python`（可能不与 `brain:Skill:Python` 匹配 → 新建独立节点）
3. 提取出关系 `brain:Niu --skilled_in--> Python`

**结果**：`Python` 节点通过 `skilled_in` 边连接到 `brain:Niu`，但**不**与 `brain:Skill:Python` 合并。图谱中存在两个独立节点：`brain:Skill:Python`（程序化创建）和 `Python`（LLM 提取）。

#### 路径3：Leiden 社区检测（延迟建边，最优雅）

**机制**：非结构化节点通过边与已有节点连接后，Leiden 社区检测会自动将其归入相应脑区，脑区主节点随之建立 `_region:contains` 关系。

**前提条件**：非结构化节点必须先通过路径1或路径2与图谱中已有节点建立至少一条边。

**建边流程**：

```
1. lightrag_insert 创建节点 "Python"，通过锚点建立 brain:Niu --skilled_in--> Python
2. Leiden 社区检测运行
3. Python 与 brain:Skill:Python（如果存在）在同一社区（因为都与 brain:Niu 相连）
4. 社区 → brain:region:编程开发 主节点
5. brain:region:编程开发 --_region:contains--> Python（自动建立）
```

**关键优势**：零代码修改，完全复用现有 Leiden + RegionManager 机制。

**限制**：
- 社区检测是延迟的（每日02:00或批量插入后），不是实时的
- 如果非结构化节点没有与任何已有节点建立边（如照片/便签），社区检测无法将其归入任何脑区
- 社区检测的粒度取决于 resolution 参数，可能将相关实体分到不同社区

#### 路径4：后置建边步骤（最直接，但需要额外代码）

**机制**：ainsert 完成后，显式查询新创建的实体，为其建立与相关脑区的连接。

**实现方案**：

```python
async def link_to_region(entity_name: str, region_manager: RegionManager):
    """将新实体连接到最相关的脑区"""
    # 1. 向量搜索：用实体描述在 entities_vdb 中搜索最相似的脑区主节点
    # 2. 如果相似度 > 阈值，建立 _region:contains 关系
    regions = region_manager.get_all_regions()
    # 用实体的 embedding 与脑区主节点的 embedding 做余弦匹配
    best_region = find_most_similar_region(entity_name, regions)
    if best_region and best_region.similarity > 0.6:
        await region_manager.add_member_to_region(best_region.name, entity_name)
```

**优势**：实时建边，不依赖社区检测的延迟。

**劣势**：需要额外代码，每次 ainsert 后都需要运行，增加延迟。

### 综合建边策略（推荐方案）

**三层递进建边**：

```
第一层：brain:Niu 锚点（实时，零成本）
  → 所有 lightrag_insert 调用都应包含 brain:Niu 锚点
  → 保证新实体至少与 brain:Niu 有一条边
  → 当前缺失：照片、视频、便签

第二层：Leiden 社区检测（延迟，零额外代码）
  → 非结构化节点通过第一层的边与 brain:Niu 相连
  → 社区检测自动将其归入脑区
  → 脑区主节点建立 _region:contains 关系
  → 限制：延迟的，不是实时的

第三层：向量匹配后置建边（实时，需额外代码，可选）
  → 对高优先级实体（如用户明确提到的概念），ainsert 后立即做向量匹配
  → 找到最相似脑区，建立 _region:contains 关系
  → 仅在需要实时脑区归属时启用
```

### 需要修复的锚点缺失

**优先级P0**（必须修复，否则实体孤立）：

| 调用点 | 当前状态 | 修复方案 |
|--------|---------|---------|
| `_sync_photos_db` | 无锚点 | 照片文本追加 `brain:Niu remembers {photo_id}。`；人物追加 `brain:Niu remembers {person_id}。` |
| `sync_photo_to_kg` | 无锚点 | 同上 |
| `sync_video_to_kg` | 无锚点 | 视频文本追加 `brain:Niu remembers {video_id}。` |
| `_inject_note_to_lightrag` | 无锚点 | 便签文本追加 `brain:Niu remembers {note_id}。` |

**优先级P1**（建议修复，提升一致性）：

| 问题 | 当前状态 | 修复方案 |
|------|---------|---------|
| DreamWriter vs lightrag_insert_entity 格式不一致 | 逗号 vs 空格分隔 | 统一为逗号分隔（DreamWriter 格式） |
| SkillSync 用 "技能:" 前缀 | 与 "语义记忆:" 不一致 | 统一为 "语义记忆:" 前缀 |
| 便签 JSON 无格式 | 直接序列化 | 改为结构化自然语言格式 |

### 实体名重复问题（brain:Skill:Python vs Python）

**当前状况**：
- `brain_graph.store_memory()` 创建 `brain:Skill:Python`（三段式，程序化）
- `lightrag_insert` 的 LLM 提取创建 `Python`（裸名，非结构化）
- 两者是**不同节点**，精确匹配不可能

**这不是 bug，而是设计如此**：
- `brain:Skill:Python` 是程序化精确控制的实体，有特定的 brain_meta_* 属性和权重
- `Python` 是 LLM 自动提取的实体，有自然语言描述
- 两者通过 brain:Niu 的边间接关联（`brain:Niu --skilled_in--> brain:Skill:Python` 和 `brain:Niu --skilled_in--> Python`）
- Leiden 社区检测会将它们归入同一脑区

**如果需要合并**（可选优化）：
- 方案A：让 `store_memory()` 也用裸名（`Python` 而非 `brain:Skill:Python`）— 但这破坏了程序化命名空间
- 方案B：让 `lightrag_insert` 的文本中使用三段式名称（`brain:Skill:Python` 而非 `Python`）— 但 LLM 不保证输出精确名称
- 方案C：后置合并步骤，定期扫描图谱，将 `Python` 和 `brain:Skill:Python` 用 `merge_entities` 合并 — 可行但复杂
- **推荐**：保持现状，让两者通过 brain:Niu 边和社区检测间接关联，不做显式合并

### 总结

| 建边机制 | 可靠性 | 实时性 | 代码成本 | 当前状态 |
|---------|--------|--------|---------|---------|
| 实体合并（路径1） | 高（条件严格） | 实时 | 零 | 仅 brain:Niu 可匹配 |
| brain:Niu 锚点（路径2） | 中（依赖 LLM） | 实时 | 零 | **部分缺失**（照片/视频/便签） |
| Leiden 社区检测（路径3） | 高 | 延迟 | 零 | 已实现 |
| 向量匹配后置建边（路径4） | 中 | 实时 | 中 | 未实现 |

**推荐实施顺序**：
1. **P0**：修复锚点缺失（照片/视频/便签追加 brain:Niu 锚点）
2. **P1**：统一文本格式（前缀、分隔符）
3. **P2**：观察 Leiden 社区检测的实际效果，如果不够好再考虑实现路径4
4. **可选**：实体名合并（方案C），仅在出现大量重复实体时考虑

---

## 脑区提示词注入方案 — 通过 LLM 代理拦截

> 2026-05-07 追加。本节设计通过 LLM 代理服务器拦截 LightRAG 的提取请求，注入脑区架构信息，让大模型在建边时考虑脑区归属。

### 核心思路

**问题**：LightRAG 只是个图谱库，它没有"主体大脑"的概念。硬塞给它 `brain:region:xxx` 节点，它也不知道这是什么意思。大模型在建边时不知道脑区架构，所以建出来的边是"语义相关"的，不是"脑区归属"的。

**解法**：LightRAG 的所有 LLM 请求都经过我们的代理服务器（`niu_api/llm_proxy.py`），我们在请求中**强行注入脑区架构提示词**，让大模型在建边时就考虑脑区归属。

**关键认知**：图谱数据是异步注入的，注入时没有"当前活跃脑区"的概念。所以不需要告诉大模型"现在哪个脑区亮了"，只需要：
1. **讲清楚脑区是什么、干什么用的** — 让大模型理解脑区架构的设计意图
2. **告诉它现有的脑区结构** — 让大模型知道图中已有哪些脑区
3. **告诉它如何建新脑区** — 让大模型在提取到新领域知识时能主动创建脑区

大模型不是傻子，讲清楚原理它自然就会用。

**为什么是代理注入而非改源码**：
1. 零侵入 — 不修改 LightRAG 源码，pip upgrade 安全
2. 动态 — 脑区结构会随知识增长变化，每次请求注入最新的脑区列表
3. 集中 — 所有 LLM 请求都经过代理，一个注入点覆盖所有场景

### LightRAG 本身没有社区检测能力

经过对 LightRAG 源码（`E:\tools\LightRAG`）的彻底调查，结论明确：

**LightRAG 没有社区检测功能 — 既没有 Leiden/Louvain 等图算法实现，也没有提示词驱动的社区报告生成。**

这与 Microsoft GraphRAG 形成根本性区别：

| | LightRAG | GraphRAG |
|---|---------|----------|
| 社区检测 | **无** | Leiden 算法（图算法） |
| 社区报告 | **无** | LLM 为每个社区生成摘要（提示词驱动） |
| global 查询 | 向量检索关系边 + 图遍历 | 检索社区报告摘要 |
| 设计哲学 | 在线增量检索，无需离线预处理 | 离线社区分层 + 报告生成 |

LightRAG 的 `global` 模式是"从关系维度检索"（向量检索关系边 → 获取端点实体），不是 GraphRAG 的"从社区摘要检索"。

**这意味着**：脑区（社区检测 + 激活/衰减）完全是我们自己的架构，LightRAG 一无所知。所以必须通过提示词注入让大模型理解脑区。

### 架构现状

```
LightRAG 内部 LLM 调用
    ↓
_llm_model_func(prompt, system_prompt=..., ...)
    ↓ model="proxy-model", base_url="http://localhost:9876/llm/v1"
    ↓
POST /llm/v1/chat/completions  ← 我们的代理
    ↓
llm_proxy.py: chat_completions()
    ↓ 当前：直接转发，零修改
    ↓ 改造后：检测提取请求 → 注入脑区提示词 → 转发
    ↓
LiteLLM → 外部 LLM API
```

**代理已有完整的请求拦截能力**：
- 请求格式：OpenAI 兼容（`messages: [{role, content}]`）
- 转换函数：`openai_to_litellm_messages()` 逐条复制 role + content
- 注入点：第350行，`litellm_messages` 转换后、`call_llm_via_litellm()` 调用前

### LightRAG 提取请求的特征

从日志 `logs/llm_interaction_20260507.log` 中观察到的实际请求：

**System Prompt 特征**：
```
---Role---
You are a Knowledge Graph Specialist responsible for extracting entities and relationships from the input text.
```

**实体类型列表**（已自定义）：
```
Person,Organization,Technology,Concept,Location,Event,Document,Photo,Video,Note,Chat,Skill,Tool,Knowledge,InteractionHabit,EpisodicEvent,BrainRegion,Other
```

**检测方法**：当 system prompt 包含 `"Knowledge Graph Specialist"` 时，识别为 LightRAG 实体提取请求。

### 注入内容设计

当检测到 LightRAG 实体提取请求时，在 system prompt 末尾追加脑区架构指导：

```
---Brain Region Architecture---
You are building a knowledge graph for an AI assistant named Niu. The graph has a brain region structure that organizes knowledge by professional domain.

What are brain regions:
- Brain regions are domain-specific knowledge clusters (e.g., "财务知识" for finance, "编程开发" for programming).
- Each brain region has a master node: brain:region:{name} (entity_type=BrainRegion).
- All brain regions are anchored to the root self-entity brain:Niu via "brain_region_anchor" relationships.
- Members of a brain region are connected via "belongs_to_region" relationships to the region master node.
- When knowledge clearly belongs to a professional domain, it should be organized under the corresponding brain region.

Current brain regions in the graph:
{current_regions_text}

How to handle brain regions during extraction:
1. When you extract an entity that clearly belongs to one of the existing brain regions above, add a relationship: entity --belongs_to_region--> brain:region:{region_name}
2. When you extract entities that form a new professional domain not covered by existing brain regions, create a new brain region:
   - Create entity: brain:region:{new_region_name} (entity_type=BrainRegion, description=summary of this domain)
   - Create relationship: brain:Niu --brain_region_anchor--> brain:region:{new_region_name}
   - Create relationship: entity --belongs_to_region--> brain:region:{new_region_name}
3. The entity "brain:Niu" is the root self-entity. All memory relationships start from brain:Niu.
4. Brain region entities (entity_type=BrainRegion) are structural containers, not content entities. Do not extract them as regular knowledge entities.
```

**`current_regions_text` 示例**（从图谱中实时读取）：
```
- brain:region:编程开发: Python/NumPy/Web技术栈, 数据分析方法
- brain:region:财务知识: 报销流程、预算审批、财务报表
- brain:region:项目管理: AI_Bot项目、需求变更
- brain:region:日常偏好: 暗色主题、远程办公
- brain:region:聊天历史: 日常对话中提炼的偏好和经验
```

### 注入逻辑

```python
# llm_proxy.py 中的注入逻辑

BRAIN_REGION_INJECTION_MARKER = "Knowledge Graph Specialist"

async def chat_completions(request: OpenAIChatRequest) -> OpenAIChatResponse:
    litellm_messages = openai_to_litellm_messages(request.messages)

    # === 脑区提示词注入 ===
    litellm_messages = inject_brain_region_context(litellm_messages)

    # 继续原有流程
    response = await call_llm_via_litellm(messages=litellm_messages, ...)


def inject_brain_region_context(messages: list[dict]) -> list[dict]:
    """检测 LightRAG 提取请求，注入脑区架构信息"""

    # 1. 检测：找到 system 消息，检查是否包含特征标记
    system_idx = None
    for i, msg in enumerate(messages):
        if msg["role"] == "system" and BRAIN_REGION_INJECTION_MARKER in (msg.get("content") or ""):
            system_idx = i
            break

    if system_idx is None:
        return messages  # 非 LightRAG 提取请求，不注入

    # 2. 获取当前脑区结构（从图谱中读取，非会话级激活状态）
    brain_context = get_brain_region_structure()
    if not brain_context:
        return messages  # 脑区系统未初始化，不注入

    # 3. 构造注入文本
    injection = build_brain_region_injection(brain_context)

    # 4. 追加到 system prompt 末尾
    messages[system_idx]["content"] = messages[system_idx]["content"] + "\n\n" + injection

    return messages
```

### 脑区结构获取

```python
def get_brain_region_structure() -> list[dict] | None:
    """从图谱中读取当前脑区结构

    注意：这里读取的是图谱中持久化的脑区列表，
    不是会话级的激活状态（异步注入没有活跃脑区的概念）。
    """
    try:
        from niu_api.internal.lightrag_manager import get_lightrag_manager
        manager = get_lightrag_manager()
        if not manager or not hasattr(manager, 'region_manager'):
            return None

        region_mgr = manager.region_manager
        regions = region_mgr.get_all_regions()

        result = []
        for region in regions:
            members = region_mgr.get_region_members(region.name)
            member_summary = ", ".join(members[:5])
            if len(members) > 5:
                member_summary += f" (+{len(members)-5} more)"

            result.append({
                "name": region.name,          # "brain:region:编程开发"
                "label": region.label,        # "编程开发"
                "description": region.description,
                "members_summary": member_summary,
            })

        return result

    except Exception:
        return None  # 脑区系统未就绪，静默跳过
```

### 效果分析

**注入前**（当前行为）：
- LLM 提取 "报销单" 实体，建边 "报销单 --associated_with--> 财务部"
- 不考虑脑区归属，边是纯语义的
- 查询 "报销" 时，向量检索命中所有含 "报销" 的实体，不区分脑区

**注入后**（期望行为）：
- LLM 提取 "报销单" 实体，看到脑区列表中有 "财务知识"
- 建边 "报销单 --associated_with--> 财务部"（语义边）
- 建边 "报销单 --belongs_to_region--> brain:region:财务知识"（脑区归属边）
- 查询 "报销" 时，脑区 "财务知识" 被激活，检索沿财务线走
- 即使中间聊了几句闲话，"财务知识" 脑区缓慢衰减（0.92/轮），不会立即熄灭

**与向量检索的本质区别**：
- 向量检索：`similarity("报销", all_entities)` → 命中所有语义相似的实体，不区分专业领域
- 脑区图检索：`activate("财务知识") → traverse(belongs_to_region) → 只返回财务脑区内的实体` → 定向检索

**大模型自主创建新脑区**：
- 当注入的知识属于全新领域（如"法律合规"），现有脑区列表中没有
- LLM 理解了脑区原理后，会主动创建 `brain:region:法律合规` + anchor + belongs_to 关系
- 这比硬编码的社区检测更灵活，大模型能理解语义边界

### Token 开销估算

注入文本的 token 消耗：
- 固定部分（原理说明 + 建边指导）：~250 tokens
- 每个脑区：~20-30 tokens（名称 + 5个成员）
- 10个脑区总计：~250 + 300 = ~550 tokens

LightRAG 提取请求的 system prompt 本身约 800-1000 tokens，注入后增加约 550 tokens（~55%），在可接受范围内。

### 实施步骤

1. **修改 `niu_api/llm_proxy.py`**：
   - 添加 `inject_brain_region_context()` 函数
   - 在 `chat_completions()` 中调用注入逻辑
   - 添加配置开关（`BRAIN_REGION_INJECTION_ENABLED = True`）

2. **修改 `niu_api/internal/lightrag_manager.py`**：
   - 暴露 `region_manager` 属性
   - 或提供 `get_brain_region_structure()` 方法

3. **测试**：
   - 注入前：对比 LLM 提取结果（无脑区归属边）
   - 注入后：确认 LLM 输出包含 `belongs_to_region` 关系
   - 确认 LLM 能自主创建新脑区
   - 确认 token 开销在预期范围内

4. **调优**：
   - 注入文本的详细程度（成员数量、描述长度）
   - 是否对查询请求也注入（让查询时也考虑脑区）

### 备选方案：修改 LightRAG 源码 prompt

如果代理注入方案不可行（如性能问题、请求格式不兼容），备选方案是直接修改 LightRAG 的提取 prompt：

1. 修改 `E:/opencode/venv/Lib/site-packages/lightrag/prompt.py` 中的 `PROMPTS["entity_extraction_system_prompt"]`
2. 在 prompt 末尾追加脑区架构指导（静态文本，脑区列表需要硬编码或定期更新）
3. 缺点：无法动态注入最新脑区列表，pip upgrade 会覆盖

**优先级**：代理注入 >> 修改源码 prompt >> 运行时覆盖 PROMPTS 字典
