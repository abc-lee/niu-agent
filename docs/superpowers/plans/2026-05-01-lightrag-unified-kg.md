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
