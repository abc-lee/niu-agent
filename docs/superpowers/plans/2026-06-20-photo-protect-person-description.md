# 照片入库保护人物描述 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 照片入库时，不修改已有人物实体的 description，只建边关系。

**Architecture:** 五层封堵：(1) adapter.merge_entities 透传 merge_strategy（关键中间层）；(2) lightrag-server 层透传 merge_strategy；(3) 照片入库主路径过滤已存在 person entity；(4) name_person/merge_persons 不注入低质量 description + 传 keep_last 策略（target 排在 data_list 最后）；(5) 首次入库改进人物 description。

**Tech Stack:** Python, photo-server MCP, lightrag-server MCP, LightRAG fork, lightrag_adapter

---

## 修改文件清单

| 文件 | 职责 |
|------|------|
| `niu_api/internal/lightrag_adapter.py` | adapter.merge_entities 增加 merge_strategy 和 target_entity_data 透传（关键中间层） |
| `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` | lightrag_merge_entities 增加 merge_strategy 透传 |
| `mcp-servers/photo-server/src/niu_photo_server/__init__.py` | 照片入库主路径、name_person、merge_persons、_merge_duplicate_person_entities |

---

### Task 1: adapter.merge_entities 增加 merge_strategy 和 target_entity_data 透传

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py:1257-1335`

**原因:** 这是最关键的中间层。`lightrag-server` 的 `lightrag_merge_entities` 调用 `adapter.merge_entities()`，后者调用 `rag.amerge_entities()`。当前 `adapter.merge_entities()` 只接受 `source_entities` 和 `target_entity`，不接收也不传递 `merge_strategy` 和 `target_entity_data`。即使 lightrag-server 层正确透传，参数在 adapter 层会被丢弃。

- [ ] **Step 1: 修改 adapter.merge_entities 签名和实现**

当前签名（lightrag_adapter.py 第 1257 行）：
```python
def merge_entities(self, source_entities: list[str], target_entity: str) -> dict:
```

改为：
```python
def merge_entities(self, source_entities: list[str], target_entity: str, merge_strategy: dict | None = None, target_entity_data: dict | None = None) -> dict:
```

同时修改内部调用（第 1332-1333 行），将：
```python
result = call_async(
    rag.amerge_entities(resolved_sources, resolved_target),
    timeout=300,
)
```

改为：
```python
result = call_async(
    rag.amerge_entities(resolved_sources, resolved_target, merge_strategy=merge_strategy, target_entity_data=target_entity_data),
    timeout=300,
)
```

- [ ] **Step 2: 提交**

```bash
git add niu_api/internal/lightrag_adapter.py
git commit -m "feat: adapter.merge_entities adds merge_strategy and target_entity_data passthrough"
```

---

### Task 2: lightrag_merge_entities 增加 merge_strategy 和 target_entity_data 透传

**Files:**
- Modify: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py:1424-1440`

**原因:** 当前 `lightrag_merge_entities` 只接受 `source_entities` 和 `target_entity`，不传 `merge_strategy`。Task 1 已在 adapter 层添加支持，此 Task 在 lightrag-server 层添加透传。

- [ ] **Step 1: 修改 lightrag_merge_entities 签名，透传 merge_strategy 和 target_entity_data**

```python
def lightrag_merge_entities(
    source_entities: List[str],
    target_entity: str,
    merge_strategy: dict = None,
    target_entity_data: dict = None,
) -> Dict[str, Any]:
    """Merge multiple entities into one."""
    try:
        adapter = _get_adapter()
        return adapter.merge_entities(
            source_entities=source_entities,
            target_entity=target_entity,
            merge_strategy=merge_strategy,
            target_entity_data=target_entity_data,
        )
    except Exception as e:
        logger.error(f"lightrag_merge_entities failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 2: 同步更新 TOOL_SCHEMAS 中 lightrag_merge_entities 的 schema**

在 `lightrag_merge_entities` 对应的 TOOL_SCHEMAS 条目中，`input_schema.properties` 增加 `merge_strategy` 和 `target_entity_data` 参数定义：

```python
"merge_strategy": {
    "type": "object",
    "description": "Merge strategy per field. Keys are field names (e.g. 'description'), values are strategy names ('concatenate', 'keep_first', 'keep_last', 'overwrite'). Default: {'description': 'concatenate', 'entity_type': 'keep_first'}",
},
"target_entity_data": {
    "type": "object",
    "description": "Specific values to set for target entity after merge. Overrides merged data. E.g. {'description': 'custom desc'}",
},
```

- [ ] **Step 3: 提交**

```bash
git add mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py
git commit -m "feat: lightrag_merge_entities adds merge_strategy and target_entity_data passthrough"
```

---

### Task 3: 照片入库主路径 — 过滤已存在的 person entity

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:667-700`（`_do_sync_photo_to_kg_sync` 中 `format_photo_ingest_data` 调用到 `custom_kg_fn` 调用之间）

**原理:** `format_photo_ingest_data` 每次都构造 person entity 带 description。在注入 KG 前，对 `entity_type == "person"` 且已存在于 KG 的 entity，从 entities 列表中移除。relationships 不受影响（`ainsert_custom_kg` 的 `has_nodes_batch` 会发现已存在节点，只建边不创建占位节点）。

**重要**: chunk_text 必须在过滤前构造，使用完整的人物名列表，确保向量检索不受影响。

- [ ] **Step 1: 在 `_do_sync_photo_to_kg_sync` 中，重新组织 chunk_text 构造和 entity 过滤的顺序**

当前代码流程：
1. `data = format_photo_ingest_data(...)`
2. `entity_names = [e["entity_name"] for e in data["entities"]]` ← 从 data["entities"] 提取
3. 构造 chunk_text
4. 调用 custom_kg_fn

需要改为：
1. `data = format_photo_ingest_data(...)`
2. 从 `data["entities"]` 提取完整人物名列表（用于 chunk_text）
3. 过滤已存在的 person entity
4. 用完整人物名列表构造 chunk_text
5. 调用 custom_kg_fn

在 `data = format_photo_ingest_data(file_path, abstract, detected_persons)` 这行之后，将原有的 `entity_names` 和 `chunk_text` 构造代码替换为：

```python
        # 保存过滤前的完整人物名列表（用于 chunk_text，确保向量检索完整）
        all_entity_names = [e["entity_name"] for e in data["entities"]]
        all_person_names = [e["entity_name"] for e in data["entities"] if e.get("entity_type") == "person"]

        # 过滤已存在的 person entity：照片入库不应覆盖已有的人物 description
        # 已存在的 person 只建边（relationships 不受影响），不注入 entity
        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            from niu_api.internal.lightrag_manager import graph_read_lock
            _adapter = LightRAGAdapter()
            _rag = _adapter._get_rag()
            if _rag is not None:
                _graph_obj = getattr(_rag, "chunk_entity_relation_graph", None)
                _nx_graph = _graph_obj._graph if hasattr(_graph_obj, "_graph") else _graph_obj
                with graph_read_lock():
                    _existing_persons = set()
                    _new_entities = []
                    for ent in data["entities"]:
                        if ent.get("entity_type") == "person" and _nx_graph.has_node(ent["entity_name"].lower()):
                            _existing_persons.add(ent["entity_name"])
                        else:
                            _new_entities.append(ent)
                    data["entities"] = _new_entities
                if _existing_persons:
                    logger.info(f"[KG] Skipping existing person entities: {_existing_persons}")
        except Exception as e:
            logger.warning(f"[KG] Person entity filter failed, injecting all entities: {e}")

        normalized_path = file_path.replace("\\", "/").lower()
        normalized_stem = Path(normalized_path).stem
        registry = get_registry()

        # --- 构建 chunk_text（使用完整人物名列表，不受过滤影响） ---
        chunk_text = (
            f"照片 {normalized_stem}：{abstract}\n"
            f"实体：{', '.join(all_entity_names)}\n"
        )
        if all_person_names:
            chunk_text += f"人物：{', '.join(all_person_names)}\n"
```

- [ ] **Step 2: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "fix: filter existing person entities before photo KG injection, preserve chunk_text"
```

---

### Task 4: name_person — 不注入带"原名"的 description + keep_last 策略

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:2332-2349`

**原理:** `name_person` 调用 `inject_fn` 确保"任飞"实体存在，但注入了 `"任飞，原名未命名人物_1"` 的 description。如果"任飞"已存在，去重层跳过不会覆盖；如果"任飞"不存在（首次改名场景），会创建带"原名"的低质量 description。改为：description 不含"原名"信息。

**重要**: merge_strategy 必须用 `"keep_last"` 而非 `"keep_first"`，因为 `_merge_attributes` 的 data_list 中 source 在前、target 在后，`"keep_last"` 保留 target（"任飞"）的 description。

- [ ] **Step 1: 修改 name_person 中的 inject_fn 调用**

将：
```python
            if inject_fn:
                inject_fn(
                    entities=[{
                        "entity_name": name,
                        "entity_type": "person",
                        "description": f"{name}，原名{source_entity}",
                    }],
                    relationships=[],
                    chunks=[],
                    source_id=f"rename_{source_entity}",
                )
```

改为：
```python
            if inject_fn:
                inject_fn(
                    entities=[{
                        "entity_name": name,
                        "entity_type": "person",
                        "description": name,
                    }],
                    relationships=[],
                    chunks=[],
                    source_id=f"rename_{source_entity}",
                )
```

- [ ] **Step 2: 修改 name_person 中的 merge_fn 调用，传 keep_last 策略保护 description**

将：
```python
            if merge_fn:
                merge_fn(
                    source_entities=[source_entity],
                    target_entity=name,
                )
```

改为：
```python
            if merge_fn:
                merge_fn(
                    source_entities=[source_entity],
                    target_entity=name,
                    merge_strategy={"description": "keep_last"},
                )
```

**为什么 keep_last**: `_merge_attributes` 的 data_list = [source 数据, target 数据]，`keep_last` 取 `values[-1]` 即 target 的 description，保留"任飞"的高质量描述，不被"未命名人物_1"的照片描述覆盖。

- [ ] **Step 3: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "fix: name_person preserves target description with keep_last merge strategy"
```

---

### Task 5: merge_persons — 不注入带"合并自"的 description + keep_last 策略

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:2565-2588`

**原理:** 同 Task 4，`merge_persons` 注入了 `"任飞，合并自xxx"` 的 description，且 merge 用 concatenate 策略拼接 description。两处都需要修复。

- [ ] **Step 1: 修改 merge_persons 中的 inject_fn 调用**

将：
```python
                inject_fn(
                    entities=[{
                        "entity_name": kg_name_a,
                        "entity_type": "person",
                        "description": f"{kg_name_a}，合并自{kg_name_b}",
                    }],
                    relationships=[],
                    chunks=[],
                    source_id=f"merge_{kg_name_a}",
                )
```

改为：
```python
                inject_fn(
                    entities=[{
                        "entity_name": kg_name_a,
                        "entity_type": "person",
                        "description": kg_name_a,
                    }],
                    relationships=[],
                    chunks=[],
                    source_id=f"merge_{kg_name_a}",
                )
```

- [ ] **Step 2: 修改 merge_persons 中的 merge_fn 调用，传 keep_last 策略**

将：
```python
                if merge_fn:
                    merge_fn(
                        source_entities=[kg_name_b],
                        target_entity=kg_name_a,
                    )
```

改为：
```python
                if merge_fn:
                    merge_fn(
                        source_entities=[kg_name_b],
                        target_entity=kg_name_a,
                        merge_strategy={"description": "keep_last"},
                    )
```

- [ ] **Step 3: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "fix: merge_persons preserves target description with keep_last merge strategy"
```

---

### Task 6: _merge_duplicate_person_entities — 传 keep_last 策略

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:2192-2196`

**原理:** `_merge_duplicate_person_entities` 调用 `merge_fn` 合并近似名称实体（如 `"任飞(人物)"` → `"任飞"`），同样使用 concatenate 策略，会把低质量 description 拼到目标实体上。

- [ ] **Step 1: 修改 _merge_duplicate_person_entities 中的 merge_fn 调用**

将：
```python
            merge_fn(
                source_entities=unique_similar,
                target_entity=target_name,
            )
```

改为：
```python
            merge_fn(
                source_entities=unique_similar,
                target_entity=target_name,
                merge_strategy={"description": "keep_last"},
            )
```

- [ ] **Step 2: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "fix: _merge_duplicate_person_entities uses keep_last merge strategy for description"
```

---

### Task 7: format_photo_ingest_data — 首次入库时改进人物 description

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:581-583`

**原理:** 当前 description 为 `"任飞，出现在照片20090603_092316中（person_id=uuid）"`，照片关联信息应该通过 features 边关系表达，不应冗余存储在 description 中。改为只保留 person_id（人脸识别溯源需要的唯一标识）。

- [ ] **Step 1: 修改 description 构造逻辑**

将：
```python
        desc = f"{entity_name}，出现在照片{normalized_stem}中"
        if person_pid:
            desc += f"（person_id={person_pid}）"
```

改为：
```python
        desc = entity_name
        if person_pid:
            desc += f"（person_id={person_pid}）"
```

- [ ] **Step 2: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "fix: person description no longer includes photo appearance info"
```

---

## 验证步骤

1. 启动程序 `./niu`
2. 入库通讯录，确认"任飞"有电话号码 description
3. 入库含"任飞"的照片，确认 description 不被覆盖
4. 入库全新人物的照片，确认新实体正常创建（description 只有人名和 person_id）
5. 测试 name_person：把"未命名人物_1"改名为"任飞"，确认已有"任飞"的 description 不变
6. 测试 merge_persons：合并两个已命名人物，确认保留目标的 description
7. 确认 features 边关系正常建立（照片→人物）
8. 确认向量检索仍能找到"任飞"（chunk_text 包含所有人物名）
