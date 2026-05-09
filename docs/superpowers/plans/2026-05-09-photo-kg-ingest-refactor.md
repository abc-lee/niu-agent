# 照片 KG 结构化注入重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将照片入库从 ainsert+LLM 自由提取改为 inject_custom_kg 结构化注入（chunks=[]），消除 person:{uuid} 实体分裂、大小写不一致、unknown_source 等问题。

**Architecture:** 照片入库改用 `lightrag_insert_custom_kg(entities, relationships, chunks=[], source_id)` 一次性注入照片+人物+关系实体，不触发 LLM。人物命名改用 `lightrag_merge_entities` 一步改名。人物合并改用人名而非 UUID。删除人物时调用 `lightrag_delete_entity`。brain_region_prompt 静态提示改为禁止 person:{uuid} 格式。

**Tech Stack:** Python 3.11+, LightRAG inject_custom_kg, ToolRegistry 同进程调用

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `mcp-servers/photo-server/src/niu_photo_server/__init__.py` | Modify | 照片入库核心：format_photo_ingest_data, sync_photo_to_kg, name_person, merge_persons, delete_person |
| `niu_api/internal/brain_region_prompt.py` | Modify | 静态提示：禁止 person:{uuid} 格式，改用人名 |
| `scripts/test_photo_kg_refactor.py` | Create | 验证重构后的真实数据诊断测试 |

## 核心设计决策

### 决策1：人物实体命名

**规则**：人物实体使用人名作为 entity_name，禁止 `person:{uuid}` 格式。

- 已命名人物：entity_name = 人名（如 "任飞"）
- 未命名人物：entity_name = auto_label（如 "未命名人物_1"）
- UUID 不进图谱，只在 photos.db 中使用

**依据**：KG 开发字典实测结论 #1 — `person:{uuid}` 格式 LLM 不识别，导致实体分裂。

### 决策2：照片入库路径

**规则**：使用 `lightrag_insert_custom_kg(entities, relationships, chunks=[], source_id)` 一次性注入。

- chunks=[] → 不触发 LLM → 100% 可靠
- 显式创建照片实体、人物实体、关系边
- source_id = `photo:{file_path}`

**依据**：KG 开发字典 §2 — inject_custom_kg with chunks=[] 是照片入库的推荐路径。

### 决策3：人物命名路径

**规则**：使用 `lightrag_merge_entities([auto_label], name)` 一步改名。

- 旧实体（auto_label）消失，新实体（name）出现，所有边迁移
- 不走 ainsert，不触发 LLM

**依据**：KG 开发字典 §3 — amerge_entities 是人物命名的推荐方案。

---

### Task 1: 重写 format_photo_ingest_text → format_photo_ingest_data

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:432-456`

将 `format_photo_ingest_text` 改为 `format_photo_ingest_data`，返回结构化 entities + relationships 而非自由文本。

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_photo_kg_refactor.py
"""照片 KG 结构化注入重构验证测试"""

def test_format_photo_ingest_data_named_person():
    """已命名人物：entity_name 用人名，不用 person:{uuid}"""
    from niu_photo_server import format_photo_ingest_data

    result = format_photo_ingest_data(
        file_path="E:/tmp/photo/2026/test.jpg",
        abstract="任飞合影，2026:05:09",
        detected_persons=[
            {"id": "uuid-1234", "name": "任飞", "auto_label": "未命名人物_1"},
        ],
    )

    # 人物实体用人名，不用 person:uuid
    person_entities = [e for e in result["entities"] if e["entity_type"] == "person"]
    assert len(person_entities) == 1
    assert person_entities[0]["entity_name"] == "任飞"
    assert "person:" not in person_entities[0]["entity_name"]
    assert "uuid" not in person_entities[0]["entity_name"]

    # 照片实体存在
    photo_entities = [e for e in result["entities"] if e["entity_type"] == "Photo"]
    assert len(photo_entities) == 1
    assert photo_entities[0]["entity_name"] == "photo:E:/tmp/photo/2026/test.jpg"
    assert photo_entities[0]["file_path"] == "E:/tmp/photo/2026/test.jpg"

    # 关系边存在
    assert len(result["relationships"]) > 0
    # features 边：照片 → 人物
    features_rels = [r for r in result["relationships"] if r["keywords"] == "features"]
    assert len(features_rels) == 1
    assert features_rels[0]["src_id"] == "photo:E:/tmp/photo/2026/test.jpg"
    assert features_rels[0]["tgt_id"] == "任飞"


def test_format_photo_ingest_data_unnamed_person():
    """未命名人物：entity_name 用 auto_label"""
    from niu_photo_server import format_photo_ingest_data

    result = format_photo_ingest_data(
        file_path="E:/tmp/photo/2026/test2.jpg",
        abstract="未命名人物_1合影",
        detected_persons=[
            {"id": "uuid-5678", "name": "", "auto_label": "未命名人物_1"},
        ],
    )

    person_entities = [e for e in result["entities"] if e["entity_type"] == "person"]
    assert len(person_entities) == 1
    assert person_entities[0]["entity_name"] == "未命名人物_1"
    assert "person:" not in person_entities[0]["entity_name"]


def test_format_photo_ingest_data_co_occurrence():
    """多人同框：生成 co_occurs_with 双向关系"""
    from niu_photo_server import format_photo_ingest_data

    result = format_photo_ingest_data(
        file_path="E:/tmp/photo/2026/group.jpg",
        abstract="合影",
        detected_persons=[
            {"id": "uuid-a", "name": "任飞", "auto_label": "未命名人物_1"},
            {"id": "uuid-b", "name": "李明", "auto_label": "未命名人物_2"},
        ],
    )

    co_occurs = [r for r in result["relationships"] if r["keywords"] == "co_occurs_with"]
    assert len(co_occurs) >= 1  # 至少1条同框关系（双向可能被合并）
    # 确认涉及两个人名
    names_in_co = set()
    for r in co_occurs:
        names_in_co.add(r["src_id"])
        names_in_co.add(r["tgt_id"])
    assert "任飞" in names_in_co
    assert "李明" in names_in_co


def test_format_photo_ingest_data_brain_niu_anchors():
    """brain:Niu → 人物/照片 remembers 边存在"""
    from niu_photo_server import format_photo_ingest_data

    result = format_photo_ingest_data(
        file_path="E:/tmp/photo/2026/test.jpg",
        abstract="任飞合影",
        detected_persons=[
            {"id": "uuid-1234", "name": "任飞", "auto_label": "未命名人物_1"},
        ],
    )

    remembers = [r for r in result["relationships"] if r["keywords"] == "remembers"]
    targets = {r["tgt_id"] for r in remembers}
    assert "任飞" in targets
    assert "photo:E:/tmp/photo/2026/test.jpg" in targets
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/test_photo_kg_refactor.py::test_format_photo_ingest_data_named_person -v`
Expected: FAIL — `format_photo_ingest_data` does not exist

- [ ] **Step 3: Write minimal implementation**

Replace `format_photo_ingest_text` (lines 432-456) with `format_photo_ingest_data`:

```python
def format_photo_ingest_data(
    file_path: str, abstract: str, detected_persons: list
) -> dict:
    """格式化照片信息为结构化实体+关系，供 inject_custom_kg 一次性注入。

    返回 {"entities": [...], "relationships": [...]}，不触发 LLM。
    人物实体使用人名（或 auto_label）作为 entity_name，禁止 person:{uuid}。
    """
    photo_entity_name = f"photo:{file_path}"

    # 照片实体
    entities = [
        {
            "entity_name": photo_entity_name,
            "entity_type": "Photo",
            "description": abstract if abstract else f"照片 {Path(file_path).stem}",
            "file_path": file_path,
        }
    ]

    relationships = []

    # brain:Niu → 照片 remembers 边
    relationships.append({
        "src_id": "brain:Niu",
        "tgt_id": photo_entity_name,
        "keywords": "remembers",
        "description": "拥有这张照片",
    })

    # 人物实体 + 关系
    person_names = []
    for p in detected_persons:
        pname = p.get("name", "")
        auto_label = p.get("auto_label", "")
        # 已命名用真名，未命名用 auto_label
        entity_name = pname if pname and not pname.startswith("未命名人物") else auto_label
        if not entity_name:
            continue

        person_names.append(entity_name)
        entities.append({
            "entity_name": entity_name,
            "entity_type": "person",
            "description": f"{entity_name}，出现在照片{Path(file_path).stem}中",
        })

        # 照片 → 人物 features 边
        relationships.append({
            "src_id": photo_entity_name,
            "tgt_id": entity_name,
            "keywords": "features",
            "description": f"照片中出现了{entity_name}",
        })

        # brain:Niu → 人物 remembers 边
        relationships.append({
            "src_id": "brain:Niu",
            "tgt_id": entity_name,
            "keywords": "remembers",
            "description": f"认识{entity_name}",
        })

    # 多人同框：co_occurs_with 双向关系
    for i in range(len(person_names)):
        for j in range(i + 1, len(person_names)):
            a, b = person_names[i], person_names[j]
            relationships.append({
                "src_id": a,
                "tgt_id": b,
                "keywords": "co_occurs_with",
                "description": f"{a}和{b}同框出现",
            })

    return {"entities": entities, "relationships": relationships}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/test_photo_kg_refactor.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/test_photo_kg_refactor.py mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "feat: format_photo_ingest_data — 结构化实体+关系替代自由文本"
```

---

### Task 2: 重写 sync_photo_to_kg — 改用 inject_custom_kg

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:459-489`

将 `sync_photo_to_kg` 从 `lightrag_insert` (ainsert+LLM) 改为 `lightrag_insert_custom_kg` (结构化注入, chunks=[])。

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_photo_kg_refactor.py — 追加到现有测试文件

def test_sync_photo_to_kg_uses_inject_custom_kg(monkeypatch):
    """sync_photo_to_kg 应调用 lightrag_insert_custom_kg，不调用 lightrag_insert"""
    from niu_photo_server import sync_photo_to_kg

    called_tools = {}

    def mock_get(name):
        def mock_fn(**kwargs):
            called_tools[name] = kwargs
            return {"status": "ok"}
        return mock_fn

    class MockRegistry:
        def get(self, name):
            return mock_get(name)

    monkeypatch.setattr("agent.tool_registry.get_registry", lambda: MockRegistry())

    result = sync_photo_to_kg(
        file_path="E:/tmp/photo/2026/test.jpg",
        abstract="任飞合影",
        detected_persons=[
            {"id": "uuid-1234", "name": "任飞", "auto_label": "未命名人物_1"},
        ],
    )

    assert result["status"] == "success"
    # 必须调用 lightrag_insert_custom_kg
    assert "lightrag-server/lightrag_insert_custom_kg" in called_tools
    # 不能调用 lightrag_insert
    assert "lightrag-server/lightrag_insert" not in called_tools

    # 验证传入了正确的参数
    kwargs = called_tools["lightrag-server/lightrag_insert_custom_kg"]
    assert kwargs["chunks"] == []
    assert kwargs["source_id"] == "photo:E:/tmp/photo/2026/test.jpg"
    # 验证 entities 中有人名实体，无 person:uuid
    entity_names = [e["entity_name"] for e in kwargs["entities"]]
    assert "任飞" in entity_names
    assert "photo:E:/tmp/photo/2026/test.jpg" in entity_names
    assert not any("person:" in n for n in entity_names)


def test_sync_photo_to_kg_file_path_set(monkeypatch):
    """照片实体的 file_path 必须显式设置，不能是 unknown_source"""
    from niu_photo_server import sync_photo_to_kg

    called_tools = {}

    def mock_get(name):
        def mock_fn(**kwargs):
            called_tools[name] = kwargs
            return {"status": "ok"}
        return mock_fn

    class MockRegistry:
        def get(self, name):
            return mock_get(name)

    monkeypatch.setattr("agent.tool_registry.get_registry", lambda: MockRegistry())

    sync_photo_to_kg(
        file_path="E:/tmp/photo/2026/test.jpg",
        abstract="test",
        detected_persons=[],
    )

    kwargs = called_tools["lightrag-server/lightrag_insert_custom_kg"]
    photo_entities = [e for e in kwargs["entities"] if e["entity_type"] == "Photo"]
    assert len(photo_entities) == 1
    assert photo_entities[0]["file_path"] == "E:/tmp/photo/2026/test.jpg"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/test_photo_kg_refactor.py::test_sync_photo_to_kg_uses_inject_custom_kg -v`
Expected: FAIL — sync_photo_to_kg still calls lightrag_insert

- [ ] **Step 3: Write minimal implementation**

Replace `sync_photo_to_kg` (lines 459-489):

```python
def sync_photo_to_kg(file_path: str, abstract: str, detected_persons: list) -> dict:
    """同步照片信息到知识图谱（通过 inject_custom_kg 结构化注入，不触发 LLM）"""
    try:
        from agent.tool_registry import get_registry

        data = format_photo_ingest_data(file_path, abstract, detected_persons)
        registry = get_registry()
        inject_fn = registry.get("lightrag-server/lightrag_insert_custom_kg")
        if inject_fn:
            result = inject_fn(
                entities=data["entities"],
                relationships=data["relationships"],
                chunks=[],  # 无 chunks → 不触发 LLM → 100%可靠
                source_id=f"photo:{file_path}",
            )
            logger.info(f"[KG] Photo ingested via inject_custom_kg: {file_path}, result={result}")
        else:
            logger.warning("[KG] lightrag_insert_custom_kg not available in registry")

        # Mark photo as KG-synced to prevent lightrag_sync re-processing
        try:
            with _db_write_lock:
                conn = get_connection()
                conn.execute(
                    "UPDATE photos SET kg_synced = 1 WHERE file_path = ?",
                    (file_path,),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[KG] Failed to mark kg_synced for {file_path}: {e}")

        return {"status": "success", "doc_uri": file_path}

    except Exception as e:
        logger.warning(f"[KG] Photo sync failed: {e}")
        return {"status": "error", "reason": str(e)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/test_photo_kg_refactor.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py scripts/test_photo_kg_refactor.py
git commit -m "feat: sync_photo_to_kg 改用 inject_custom_kg 结构化注入"
```

---

### Task 3: 重写 name_person — 改用 merge_entities 改名

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:1828-1840`

将 name_person 的 KG 同步从 `lightrag_insert` (ainsert+LLM) 改为 `lightrag_merge_entities` 一步改名。

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_photo_kg_refactor.py — 追加

def test_name_person_uses_merge_entities(monkeypatch):
    """name_person 应调用 lightrag_merge_entities 改名，不调用 lightrag_insert"""
    from niu_photo_server import name_person

    called_tools = {}

    def mock_get(name):
        def mock_fn(**kwargs):
            called_tools[name] = kwargs
            return {"status": "ok"}
        return mock_fn

    class MockRegistry:
        def get(self, name):
            return mock_get(name)

    monkeypatch.setattr("agent.tool_registry.get_registry", lambda: MockRegistry())

    # Mock DB: person with auto_label="未命名人物_1"
    import niu_photo_server as ps
    original_get_connection = ps.get_connection

    class MockConn:
        def execute(self, sql, params=None):
            if "SELECT" in sql and "persons" in sql:
                class MockRow:
                    def __getitem__(self, idx): return None
                    def __len__(self): return 2
                # Return a row-like tuple: (id, auto_label)
                return type('Cursor', (), {'fetchone': lambda self: ("uuid-1234", "未命名人物_1")})()
            return type('Cursor', (), {'fetchone': lambda self: None})()
        def commit(self): pass

    monkeypatch.setattr(ps, "get_connection", lambda: MockConn())
    monkeypatch.setattr(ps, "_db_write_lock", type('Lock', (), {'__enter__': lambda self: self, '__exit__': lambda self, *a: None})())

    result = name_person(person_id="uuid-1234", name="任飞")

    # 必须调用 lightrag_merge_entities
    assert "lightrag-server/lightrag_merge_entities" in called_tools
    # 不能调用 lightrag_insert
    assert "lightrag-server/lightrag_insert" not in called_tools

    # 验证 merge_entities 参数：source=auto_label, target=真名
    kwargs = called_tools["lightrag-server/lightrag_merge_entities"]
    assert "未命名人物_1" in kwargs["source_entities"]
    assert kwargs["target_entity"] == "任飞"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/test_photo_kg_refactor.py::test_name_person_uses_merge_entities -v`
Expected: FAIL — name_person still calls lightrag_insert

- [ ] **Step 3: Write minimal implementation**

Replace name_person KG section (lines 1828-1840):

```python
        # KG: 通过 merge_entities 一步改名（旧实体删除+新实体创建+边迁移）
        try:
            from agent.tool_registry import get_registry
            registry = get_registry()
            auto_label = row[1]  # row = (id, auto_label)
            merge_fn = registry.get("lightrag-server/lightrag_merge_entities")
            if merge_fn:
                merge_fn(
                    source_entities=[auto_label],
                    target_entity=name,
                )
                logger.info(f"[NAME_PERSON] KG renamed: {auto_label} → {name}")
            else:
                logger.warning("[NAME_PERSON] lightrag_merge_entities not available in registry")
        except Exception as e:
            logger.warning(f"[NAME_PERSON] LightRAG rename failed: {e}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/test_photo_kg_refactor.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py scripts/test_photo_kg_refactor.py
git commit -m "feat: name_person 改用 merge_entities 一步改名"
```

---

### Task 4: 重写 merge_persons — 改用人名而非 person:{uuid}

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:2012-2042`

将 merge_persons 的 KG 同步从 `person:{uuid}` 格式改为人名格式，并使用 `inject_custom_kg` 更新目标实体描述。

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_photo_kg_refactor.py — 追加

def test_merge_persons_uses_names_not_uuid(monkeypatch):
    """merge_persons 应使用人名作为实体名，不用 person:{uuid}"""
    from niu_photo_server import merge_persons

    called_tools = {}

    def mock_get(name):
        def mock_fn(**kwargs):
            called_tools.setdefault(name, []).append(kwargs)
            return {"status": "ok"}
        return mock_fn

    class MockRegistry:
        def get(self, name):
            return mock_get(name)

    monkeypatch.setattr("agent.tool_registry.get_registry", lambda: MockRegistry())

    # Mock DB with two persons
    import niu_photo_server as ps

    class MockConn:
        def execute(self, sql, params=None):
            return type('Cursor', (), {
                'fetchone': lambda self: None,
                'fetchall': lambda self: [
                    ("uuid-a", "任飞", "未命名人物_1", None, 0.0, 3),
                    ("uuid-b", "李明", "未命名人物_2", None, 0.0, 2),
                ],
            })()
        def commit(self): pass
        def rollback(self): pass

    monkeypatch.setattr(ps, "get_connection", lambda: MockConn())
    monkeypatch.setattr(ps, "_db_write_lock", type('Lock', (), {'__enter__': lambda self: self, '__exit__': lambda self, *a: None})())

    result = merge_persons(person_a_id="uuid-a", person_b_id="uuid-b")

    # 必须调用 lightrag_merge_entities
    assert "lightrag-server/lightrag_merge_entities" in called_tools
    # 必须调用 lightrag_insert_custom_kg 更新描述
    assert "lightrag-server/lightrag_insert_custom_kg" in called_tools

    # merge_entities 参数：source=人名B, target=人名A
    merge_kwargs = called_tools["lightrag-server/lightrag_merge_entities"][0]
    assert "李明" in merge_kwargs["source_entities"]
    assert merge_kwargs["target_entity"] == "任飞"
    # 不应包含 person:uuid 格式
    assert not any("person:" in s for s in merge_kwargs["source_entities"])
    assert "person:" not in merge_kwargs["target_entity"]

    # insert_custom_kg 更新目标实体描述
    inject_kwargs = called_tools["lightrag-server/lightrag_insert_custom_kg"][0]
    assert inject_kwargs["chunks"] == []
    entity_names = [e["entity_name"] for e in inject_kwargs["entities"]]
    assert "任飞" in entity_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/test_photo_kg_refactor.py::test_merge_persons_uses_names_not_uuid -v`
Expected: FAIL — merge_persons still uses person:{uuid} format

- [ ] **Step 3: Write minimal implementation**

Replace merge_persons KG section (lines 2012-2042):

```python
        # 同步 LightRAG：更新目标实体描述，合并源实体关系到目标实体
        try:
            from agent.tool_registry import get_registry

            registry = get_registry()
            name_a = row_a_name if row_a_name else row_a_auto_label
            name_b = row_b_name if row_b_name else row_b_auto_label

            # 1. 用 inject_custom_kg 更新目标实体描述（chunks=[] → 不触发 LLM）
            inject_fn = registry.get("lightrag-server/lightrag_insert_custom_kg")
            if inject_fn:
                inject_fn(
                    entities=[{
                        "entity_name": name_a,
                        "entity_type": "person",
                        "description": f"{name_a}，合并自{name_b}",
                    }],
                    relationships=[],
                    chunks=[],
                    source_id=f"merge:{name_a}",
                )

            # 2. 合并：name_b 的边迁移到 name_a，然后删除 name_b
            merge_fn = registry.get("lightrag-server/lightrag_merge_entities")
            if merge_fn:
                merge_fn(
                    source_entities=[name_b],
                    target_entity=name_a,
                )
            logger.info(f"[MERGE_PERSONS] Merged KG entity {name_b} into {name_a}")
        except Exception as e:
            logger.warning(f"[MERGE_PERSONS] LightRAG sync failed: {e}")
```

注意：`row_a_name`, `row_a_auto_label`, `row_b_name`, `row_b_auto_label` 需要在 KG 同步代码之前从 DB 查询结果中提取。当前代码中 `person_a` 和 `person_b` 的字段是 `(id, name, auto_label, center_embedding, threshold_adjustment, photo_count)`，所以：

```python
            row_a_name = person_a[1]   # name
            row_a_auto_label = person_a[2]  # auto_label
            row_b_name = person_b[1]
            row_b_auto_label = person_b[2]
```

这些变量需要在 KG 同步 try 块之前赋值，替换原来的 `merged_name = name_a if name_a else auto_label_a`。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/test_photo_kg_refactor.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py scripts/test_photo_kg_refactor.py
git commit -m "feat: merge_persons 改用人名而非 person:{uuid}"
```

---

### Task 5: 修复 delete_person — 调用 lightrag_delete_entity

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:1141-1144`

当前 delete_person 只打日志，不实际删除 KG 实体。改为调用 `lightrag_delete_entity`。

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_photo_kg_refactor.py — 追加

def test_delete_person_calls_delete_entity(monkeypatch):
    """delete_person 应调用 lightrag_delete_entity 删除 KG 实体"""
    from niu_photo_server import delete_person

    called_tools = {}

    def mock_get(name):
        def mock_fn(**kwargs):
            called_tools[name] = kwargs
            return {"status": "ok"}
        return mock_fn

    class MockRegistry:
        def get(self, name):
            return mock_get(name)

    monkeypatch.setattr("agent.tool_registry.get_registry", lambda: MockRegistry())

    # Mock DB
    import niu_photo_server as ps

    class MockConn:
        def execute(self, sql, params=None):
            if "SELECT" in sql and "persons" in sql:
                return type('Cursor', (), {'fetchone': lambda self: ("任飞", "未命名人物_1"), 'fetchall': lambda self: []})()
            return type('Cursor', (), {'fetchone': lambda self: None, 'fetchall': lambda self: []})()
        def commit(self): pass

    monkeypatch.setattr(ps, "get_connection", lambda: MockConn())
    monkeypatch.setattr(ps, "_db_write_lock", type('Lock', (), {'__enter__': lambda self: self, '__exit__': lambda self, *a: None})())

    result = delete_person(person_id="uuid-1234")

    # 必须调用 lightrag_delete_entity
    assert "lightrag-server/lightrag_delete_entity" in called_tools
    # 用人名删除，不用 person:uuid
    kwargs = called_tools["lightrag-server/lightrag_delete_entity"]
    assert kwargs["entity_name"] == "任飞"
    assert "person:" not in kwargs["entity_name"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/test_photo_kg_refactor.py::test_delete_person_calls_delete_entity -v`
Expected: FAIL — delete_person does not call lightrag_delete_entity

- [ ] **Step 3: Write minimal implementation**

Replace delete_person KG section (lines 1141-1144):

```python
    # 同步删除知识图谱中的实体
    try:
        from agent.tool_registry import get_registry
        registry = get_registry()
        delete_fn = registry.get("lightrag-server/lightrag_delete_entity")
        if delete_fn:
            delete_fn(entity_name=person_name)
            logger.info(f"[DELETE_PERSON] KG entity deleted: {person_name}")
        else:
            logger.warning("[DELETE_PERSON] lightrag_delete_entity not available in registry")
    except Exception as e:
        logger.warning(f"[DELETE_PERSON] LightRAG entity deletion failed: {e}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/test_photo_kg_refactor.py -v`
Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py scripts/test_photo_kg_refactor.py
git commit -m "feat: delete_person 调用 lightrag_delete_entity 删除 KG 实体"
```

---

### Task 6: 修复 brain_region_prompt — 禁止 person:{uuid} 格式

**Files:**
- Modify: `niu_api/internal/brain_region_prompt.py:25-40`

将静态提示中 `person:{uuid}` 相关规则改为"人物实体使用人名作为 entity_name"。

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_photo_kg_refactor.py — 追加

def test_brain_region_prompt_no_person_uuid():
    """brain_region_prompt 静态提示不应包含 person:{uuid} 格式指导"""
    from niu_api.internal.brain_region_prompt import _STATIC_BRAIN_REGION_PROMPT

    # 不应包含 person:{uuid} 格式指导
    assert "person:{uuid}" not in _STATIC_BRAIN_REGION_PROMPT
    assert "person:xxx" not in _STATIC_BRAIN_REGION_PROMPT

    # 应包含人名作为 entity_name 的指导
    assert "人名" in _STATIC_BRAIN_REGION_PROMPT or "姓名" in _STATIC_BRAIN_REGION_PROMPT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/test_photo_kg_refactor.py::test_brain_region_prompt_no_person_uuid -v`
Expected: FAIL — _STATIC_BRAIN_REGION_PROMPT still contains "person:{uuid}"

- [ ] **Step 3: Write minimal implementation**

Replace `_STATIC_BRAIN_REGION_PROMPT` 中的"照片与人物实体规则"部分 (lines 25-40):

```python
### 人物实体
- 人物实体使用人名作为 entity_name（如"任飞"），禁止 `person:{uuid}` 格式。
- 未命名人物使用 `未命名人物_{n}` 格式（如"未命名人物_1"），命名后通过 merge_entities 改名。
- 如果图谱中已存在同名人物实体，更新其描述即可，不要创建新实体。
- 当人物实体从"未命名人物"变为真实姓名时，这是同一实体的改名（merge_entities），不是新实体。

### 照片实体
- 照片实体已由结构化入库程序预先创建，实体名格式为 `photo:{file_path}`。
- 提取时如果遇到与照片相关的描述，应与已有的 `photo:*` 实体建立关系，不要创建新的照片实体。
- 照片中出现的人物应关联到对应的人名实体，不要用 `person:{uuid}` 格式，也不要用自然语言姓名创建独立的人物实体。

### 合并规则
- 当发现两个实体实际指同一事物时（如"未命名人物_1"和"任飞"），应通过 merge_entities 合并，将旧名合并到新名。
- 同一人物的不同称呼（别名、昵称）应合并到同一人名实体中。
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/test_photo_kg_refactor.py -v`
Expected: 10 PASS

- [ ] **Step 5: Commit**

```bash
git add niu_api/internal/brain_region_prompt.py scripts/test_photo_kg_refactor.py
git commit -m "fix: brain_region_prompt 禁止 person:{uuid} 格式，改用人名"
```

---

### Task 7: 真实数据验证 — 确认重构后 KG 数据正确

**Files:**
- Create: `scripts/test_photo_kg_real_verify.py`

用真实 LightRAG 实例验证重构后的数据质量。此任务在所有代码修改完成后执行。

- [ ] **Step 1: Write verification script**

```python
# scripts/test_photo_kg_real_verify.py
"""照片 KG 重构后真实数据验证。

读取 ~/.niu/lightrag_storage/ 的 JSON 文件，验证：
1. 不存在 person:{uuid} 格式的实体
2. 照片实体 file_path 不为 unknown_source
3. 人物实体使用人名或 auto_label
"""

import json
from pathlib import Path


def load_entities():
    storage = Path.home() / ".niu" / "lightrag_storage"
    entities_file = storage / "kv_store_full_entities.json"
    if not entities_file.exists():
        print(f"[SKIP] {entities_file} not found")
        return {}
    with open(entities_file, "r", encoding="utf-8") as f:
        return json.load(f)


def test_no_person_uuid_entities():
    """不应存在 person:{uuid} 格式的实体"""
    entities = load_entities()
    if not entities:
        return

    person_uuid_entities = []
    for key, value in entities.items():
        name = value.get("entity_name", "") if isinstance(value, dict) else ""
        if name.startswith("person:"):
            person_uuid_entities.append(name)

    if person_uuid_entities:
        print(f"[FAIL] Found {len(person_uuid_entities)} person:uuid entities:")
        for name in person_uuid_entities[:10]:
            print(f"  - {name}")
    else:
        print("[PASS] No person:uuid entities found")


def test_photo_entities_have_file_path():
    """照片实体应有正确的 file_path"""
    entities = load_entities()
    if not entities:
        return

    unknown_source_count = 0
    for key, value in entities.items():
        if not isinstance(value, dict):
            continue
        name = value.get("entity_name", "")
        if name.startswith("photo:"):
            fp = value.get("file_path", "")
            if fp == "unknown_source" or not fp:
                unknown_source_count += 1
                print(f"  [WARN] {name}: file_path={fp}")

    if unknown_source_count:
        print(f"[FAIL] {unknown_source_count} photo entities with unknown_source")
    else:
        print("[PASS] All photo entities have valid file_path")


def test_person_entities_use_names():
    """人物实体应使用人名或 auto_label"""
    entities = load_entities()
    if not entities:
        return

    bad_persons = []
    for key, value in entities.items():
        if not isinstance(value, dict):
            continue
        name = value.get("entity_name", "")
        etype = value.get("entity_type", "")
        if etype == "person" and name.startswith("person:"):
            bad_persons.append(name)

    if bad_persons:
        print(f"[FAIL] {len(bad_persons)} person entities with person:uuid format")
    else:
        print("[PASS] All person entities use name/auto_label format")


if __name__ == "__main__":
    print("=== 照片 KG 重构后真实数据验证 ===\n")
    test_no_person_uuid_entities()
    test_photo_entities_have_file_path()
    test_person_entities_use_names()
    print("\n=== 验证完成 ===")
```

- [ ] **Step 2: Run verification (read-only, no API needed)**

Run: `python scripts/test_photo_kg_real_verify.py`
Expected: 3 PASS（如果旧数据已迁移）或显示需要迁移的实体

- [ ] **Step 3: Commit**

```bash
git add scripts/test_photo_kg_real_verify.py
git commit -m "test: 照片 KG 重构后真实数据验证脚本"
```

---

### Task 8: 旧数据迁移脚本

**Files:**
- Create: `scripts/migrate_photo_kg_entities.py`

将现有图谱中的 `person:{uuid}` 实体迁移为人名实体。

- [ ] **Step 1: Write migration script**

```python
# scripts/migrate_photo_kg_entities.py
"""将现有图谱中的 person:{uuid} 实体迁移为人名实体。

读取 photos.db 获取 UUID→人名映射，然后对每个 person:{uuid} 实体：
1. 调用 lightrag_merge_entities([f"person:{uuid}"], name) 改名
2. 如果未命名，调用 lightrag_merge_entities([f"person:{uuid}"], auto_label)

此脚本应在重构代码部署后运行一次。
"""

import sqlite3
import sys
from pathlib import Path


def get_person_mapping(photos_db_path: str) -> dict:
    """从 photos.db 获取 UUID → (name, auto_label) 映射"""
    conn = sqlite3.connect(photos_db_path)
    cursor = conn.execute("SELECT id, name, auto_label FROM persons")
    mapping = {}
    for row in cursor.fetchall():
        person_id, name, auto_label = row
        target_name = name if name and not name.startswith("未命名人物") else auto_label
        if target_name:
            mapping[person_id] = target_name
    conn.close()
    return mapping


def migrate_person_entities(photos_db_path: str, dry_run: bool = True):
    """迁移 person:{uuid} 实体为人名实体"""
    mapping = get_person_mapping(photos_db_path)
    print(f"Found {len(mapping)} persons in photos.db")

    if dry_run:
        print("\n[DRY RUN] Would migrate:")
        for uuid, name in mapping.items():
            old_name = f"person:{uuid}"
            print(f"  {old_name} → {name}")
        return

    from agent.tool_registry import get_registry
    registry = get_registry()
    merge_fn = registry.get("lightrag-server/lightrag_merge_entities")

    if not merge_fn:
        print("[ERROR] lightrag_merge_entities not available")
        return

    migrated = 0
    for uuid, name in mapping.items():
        old_name = f"person:{uuid}"
        try:
            result = merge_fn(source_entities=[old_name], target_entity=name)
            print(f"  [OK] {old_name} → {name}: {result}")
            migrated += 1
        except Exception as e:
            print(f"  [FAIL] {old_name} → {name}: {e}")

    print(f"\nMigrated {migrated}/{len(mapping)} person entities")


if __name__ == "__main__":
    photos_db = sys.argv[1] if len(sys.argv) > 1 else "REDACTED_WIN_PATH/photos.db"
    dry_run = "--execute" not in sys.argv

    print(f"=== 旧数据迁移: person:{{uuid}} → 人名 ===")
    print(f"photos.db: {photos_db}")
    print(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}\n")

    migrate_person_entities(photos_db, dry_run=dry_run)
```

- [ ] **Step 2: Run dry-run to preview changes**

Run: `python scripts/migrate_photo_kg_entities.py`
Expected: 列出所有 person:{uuid} → 人名映射，不执行

- [ ] **Step 3: Commit**

```bash
git add scripts/migrate_photo_kg_entities.py
git commit -m "feat: 旧数据迁移脚本 person:{uuid} → 人名"
```

---

## 不做的事

1. **不修改 LightRAG 核心代码** — 只修改 photo-server 适配层
2. **不修改 lightrag-server API** — inject_custom_kg, merge_entities, delete_entity 已存在
3. **不删除现有图谱数据** — 迁移脚本用 merge_entities 改名，保留边
4. **不修改 RegionSync/RegionManager** — 那是脑区整改的范围
5. **不修改 format_photo_ingest_text 的调用者** — 只改 sync_photo_to_kg 内部调用

## 验证方法

1. `python -m pytest scripts/test_photo_kg_refactor.py -v` — 10 个单元测试全部 PASS
2. `python scripts/test_photo_kg_real_verify.py` — 真实数据验证 3 PASS
3. `python scripts/migrate_photo_kg_entities.py` — 旧数据迁移 dry-run 预览
4. 照片入库端到端：拖入照片 → KG 中出现 `photo:{path}` + 人名实体 + features/remembers 边
5. 人物命名端到端：命名"任飞" → KG 中 `未命名人物_1` 消失，`任飞` 出现，边迁移
