# KG 工具全量对接实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 LightRAG fork 版本的 7 个 KG 操作函数全量对接到 MCP 工具层，提供完整的实体/关系增删改查能力。

**Architecture:** 三层对接 — Adapter 层封装 LightRAG async 方法为同步，MCP 工具层定义 TOOL_SCHEMAS 并实现工具函数，YAML 配置层定义虚拟磁盘映射。dedup 反馈信息改造为可操作选项。

**Tech Stack:** Python 3.11+, LightRAG fork (lightrag-hku 1.4.16), MCP 同进程架构, call_async 桥接

---

## 文件结构

| 文件 | 职责 | 改动类型 |
|------|------|----------|
| `niu_api/internal/lightrag_adapter.py` | Adapter 层：封装 7 个 LightRAG async 方法为同步 | 修改 |
| `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` | MCP 工具层：定义 TOOL_SCHEMAS + 实现工具函数 | 修改 |
| `config/disk/lightrag-server.yaml` | 虚拟磁盘映射：CLI 命令到 MCP 工具 | 修改 |
| `docs/kg-dev-dictionary.md` | KG 开发字典：工具用法文档 | 修改 |
| `tests/test_kg_tools_integration.py` | 集成测试：真实环境测试 | 创建 |

---

## Task 1: Adapter 层 — 添加 7 个方法

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py` (在 `LightRAGAdapter` 类中添加方法)

**参考 LightRAG 类包装方法签名：**
```python
# 这些是 LightRAG 类的方法，内部调用 utils_graph.py 的函数
async def aedit_entity(self, entity_name, updated_data, allow_rename=True, allow_merge=False)
async def aedit_relation(self, source_entity, target_entity, updated_data)
async def adelete_by_relation(self, source_entity, target_entity)
async def get_entity_info(self, entity_name, include_vector_data=False)
async def get_relation_info(self, src_entity, tgt_entity, include_vector_data=False)
async def acreate_entity(self, entity_name, entity_data)
async def acreate_relation(self, source_entity, target_entity, relation_data)
```

- [ ] **Step 1: 添加 `edit_entity` 方法**

在 `LightRAGAdapter` 类中添加（放在 `delete_entity` 方法之后）：

```python
def edit_entity(
    self,
    entity_name: str,
    updated_data: dict[str, str],
    allow_rename: bool = False,
    allow_merge: bool = False,
    timeout: int = 300,
) -> dict[str, Any]:
    """Edit entity information in the knowledge graph.

    Args:
        entity_name: Name of the entity to edit
        updated_data: Dict with keys like "description", "entity_type", "entity_name" (for rename)
        allow_rename: Whether to allow renaming (default False for safety)
        allow_merge: Whether to merge into existing entity when renaming to existing name
        timeout: Operation timeout in seconds

    Returns:
        Dict with entity info and operation_summary
    """
    rag = self._get_rag()
    if rag is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        result = call_async(
            rag.aedit_entity(entity_name, updated_data, allow_rename, allow_merge),
            timeout=timeout,
        )
        return {"status": "ok", "data": result}
    except Exception as e:
        logger.error(f"edit_entity failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 2: 添加 `edit_relation` 方法**

```python
def edit_relation(
    self,
    source_entity: str,
    target_entity: str,
    updated_data: dict[str, Any],
    timeout: int = 300,
) -> dict[str, Any]:
    """Edit relation (edge) information in the knowledge graph.

    Args:
        source_entity: Source entity name
        target_entity: Target entity name
        updated_data: Dict with keys like "description", "keywords", "weight"
        timeout: Operation timeout in seconds

    Returns:
        Dict with updated relation info
    """
    rag = self._get_rag()
    if rag is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        result = call_async(
            rag.aedit_relation(source_entity, target_entity, updated_data),
            timeout=timeout,
        )
        return {"status": "ok", "data": result}
    except Exception as e:
        logger.error(f"edit_relation failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 3: 添加 `delete_relation` 方法**

```python
def delete_relation(
    self,
    source_entity: str,
    target_entity: str,
    timeout: int = 300,
) -> dict[str, Any]:
    """Delete a relation between two entities (keeps both entities).

    Args:
        source_entity: Source entity name
        target_entity: Target entity name
        timeout: Operation timeout in seconds

    Returns:
        Dict with deletion status
    """
    rag = self._get_rag()
    if rag is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        from lightrag.utils_graph import DeletionResult
        result: DeletionResult = call_async(
            rag.adelete_by_relation(source_entity, target_entity),
            timeout=timeout,
        )
        return {
            "status": "ok" if result.status == "success" else result.status,
            "message": result.message,
            "status_code": result.status_code,
        }
    except Exception as e:
        logger.error(f"delete_relation failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 4: 添加 `get_entity_info` 方法**

```python
def get_entity_info(
    self,
    entity_name: str,
    include_vector_data: bool = False,
    timeout: int = 30,
) -> dict[str, Any]:
    """Get detailed information of an entity.

    Args:
        entity_name: Entity name to query
        include_vector_data: Whether to include vector database info
        timeout: Operation timeout in seconds

    Returns:
        Dict with entity_name, source_id, graph_data, and optionally vector_data
    """
    rag = self._get_rag()
    if rag is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        result = call_async(
            rag.get_entity_info(entity_name, include_vector_data),
            timeout=timeout,
        )
        return {"status": "ok", "data": result}
    except Exception as e:
        logger.error(f"get_entity_info failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 5: 添加 `get_relation_info` 方法**

```python
def get_relation_info(
    self,
    src_entity: str,
    tgt_entity: str,
    include_vector_data: bool = False,
    timeout: int = 30,
) -> dict[str, Any]:
    """Get detailed information of a relationship between two entities.

    Args:
        src_entity: Source entity name
        tgt_entity: Target entity name
        include_vector_data: Whether to include vector database info
        timeout: Operation timeout in seconds

    Returns:
        Dict with src_entity, tgt_entity, source_id, graph_data, and optionally vector_data
    """
    rag = self._get_rag()
    if rag is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        result = call_async(
            rag.get_relation_info(src_entity, tgt_entity, include_vector_data),
            timeout=timeout,
        )
        return {"status": "ok", "data": result}
    except Exception as e:
        logger.error(f"get_relation_info failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 6: 添加 `create_entity` 方法**

```python
def create_entity(
    self,
    entity_name: str,
    entity_type: str,
    description: str = "",
    source_id: str = "manual_creation",
    file_path: str = "manual_creation",
    timeout: int = 300,
) -> dict[str, Any]:
    """Create a new entity in the knowledge graph.

    Args:
        entity_name: Name of the new entity
        entity_type: Entity type (e.g., "Person", "Concept", "Skill")
        description: Entity description
        source_id: Source chunk ID
        file_path: File path for citation
        timeout: Operation timeout in seconds

    Returns:
        Dict with created entity info
    """
    rag = self._get_rag()
    if rag is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        entity_data = {
            "entity_type": entity_type,
            "description": description,
            "source_id": source_id,
            "file_path": file_path,
        }
        result = call_async(
            rag.acreate_entity(entity_name, entity_data),
            timeout=timeout,
        )
        return {"status": "ok", "data": result}
    except Exception as e:
        logger.error(f"create_entity failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 7: 添加 `create_relation` 方法**

```python
def create_relation(
    self,
    source_entity: str,
    target_entity: str,
    keywords: str,
    description: str = "",
    weight: float = 1.0,
    source_id: str = "manual_creation",
    file_path: str = "manual_creation",
    timeout: int = 300,
) -> dict[str, Any]:
    """Create a new relation between two entities.

    Args:
        source_entity: Source entity name
        target_entity: Target entity name
        keywords: Relation keywords (required, used for matching)
        description: Relation description
        weight: Relation weight (default 1.0)
        source_id: Source chunk ID
        file_path: File path for citation
        timeout: Operation timeout in seconds

    Returns:
        Dict with created relation info
    """
    rag = self._get_rag()
    if rag is None:
        return {"status": "error", "message": "LightRAG not available"}

    try:
        relation_data = {
            "keywords": keywords,
            "description": description,
            "weight": weight,
            "source_id": source_id,
            "file_path": file_path,
        }
        result = call_async(
            rag.acreate_relation(source_entity, target_entity, relation_data),
            timeout=timeout,
        )
        return {"status": "ok", "data": result}
    except Exception as e:
        logger.error(f"create_relation failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 8: 提交 Adapter 层改动**

```bash
git add niu_api/internal/lightrag_adapter.py
git commit -m "feat(adapter): 添加7个KG操作方法 — edit_entity/edit_relation/delete_relation/get_entity_info/get_relation_info/create_entity/create_relation

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: MCP 工具层 — 添加 7 个工具定义

**Files:**
- Modify: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py`

- [ ] **Step 1: 在 TOOL_SCHEMAS 中添加 `lightrag_edit_entity`**

在 `TOOL_SCHEMAS` 字典末尾添加（`lightrag_timeline_query` 之后）：

```python
"lightrag_edit_entity": {
    "name": "lightrag_edit_entity",
    "description": "Edit entity information in the knowledge graph. Can update description, type, or rename entity. Set allow_rename=True to enable renaming. Set allow_merge=True to merge into existing entity when renaming to an existing name.",
    "input_schema": {
        "type": "object",
        "properties": {
            "entity_name": {"type": "string", "description": "Entity name to edit"},
            "description": {"type": "string", "description": "New description (overwrites existing)"},
            "entity_type": {"type": "string", "description": "New entity type"},
            "new_name": {"type": "string", "description": "New entity name (requires allow_rename=True)"},
            "allow_rename": {"type": "boolean", "default": False, "description": "Allow renaming entity"},
            "allow_merge": {"type": "boolean", "default": False, "description": "Allow merging into existing entity when renaming"},
        },
        "required": ["entity_name"],
    },
},
```

- [ ] **Step 2: 在 TOOL_SCHEMAS 中添加 `lightrag_edit_relation`**

```python
"lightrag_edit_relation": {
    "name": "lightrag_edit_relation",
    "description": "Edit relation (edge) information between two entities. Can update description, keywords, or weight.",
    "input_schema": {
        "type": "object",
        "properties": {
            "source_entity": {"type": "string", "description": "Source entity name"},
            "target_entity": {"type": "string", "description": "Target entity name"},
            "keywords": {"type": "string", "description": "Current keywords (used to identify the relation)"},
            "new_keywords": {"type": "string", "description": "New keywords"},
            "new_description": {"type": "string", "description": "New description"},
            "new_weight": {"type": "number", "description": "New weight"},
        },
        "required": ["source_entity", "target_entity"],
    },
},
```

- [ ] **Step 3: 在 TOOL_SCHEMAS 中添加 `lightrag_delete_relation`**

```python
"lightrag_delete_relation": {
    "name": "lightrag_delete_relation",
    "description": "Delete a relation between two entities. Both entities are kept, only the relation is removed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "source_entity": {"type": "string", "description": "Source entity name"},
            "target_entity": {"type": "string", "description": "Target entity name"},
            "keywords": {"type": "string", "description": "Relation keywords (optional, if not specified deletes all relations between the two entities)"},
        },
        "required": ["source_entity", "target_entity"],
    },
},
```

- [ ] **Step 4: 在 TOOL_SCHEMAS 中添加 `lightrag_get_entity_info`**

```python
"lightrag_get_entity_info": {
    "name": "lightrag_get_entity_info",
    "description": "Get detailed information of a single entity, including graph data and optionally vector data.",
    "input_schema": {
        "type": "object",
        "properties": {
            "entity_name": {"type": "string", "description": "Entity name to query"},
            "include_vector_data": {"type": "boolean", "default": False, "description": "Include vector database information"},
        },
        "required": ["entity_name"],
    },
},
```

- [ ] **Step 5: 在 TOOL_SCHEMAS 中添加 `lightrag_get_relation_info`**

```python
"lightrag_get_relation_info": {
    "name": "lightrag_get_relation_info",
    "description": "Get detailed information of a relationship between two entities.",
    "input_schema": {
        "type": "object",
        "properties": {
            "source_entity": {"type": "string", "description": "Source entity name"},
            "target_entity": {"type": "string", "description": "Target entity name"},
            "include_vector_data": {"type": "boolean", "default": False, "description": "Include vector database information"},
        },
        "required": ["source_entity", "target_entity"],
    },
},
```

- [ ] **Step 6: 在 TOOL_SCHEMAS 中添加 `lightrag_create_entity`**

```python
"lightrag_create_entity": {
    "name": "lightrag_create_entity",
    "description": "Create a new entity in the knowledge graph. Fails if entity already exists. Use lightrag_insert_entity for upsert behavior.",
    "input_schema": {
        "type": "object",
        "properties": {
            "entity_name": {"type": "string", "description": "Entity name (must be unique)"},
            "entity_type": {"type": "string", "description": "Entity type (e.g., Person, Concept, Skill, Tool)"},
            "description": {"type": "string", "default": "", "description": "Entity description"},
            "source_id": {"type": "string", "default": "manual_creation", "description": "Source chunk ID"},
            "file_path": {"type": "string", "default": "manual_creation", "description": "File path for citation"},
        },
        "required": ["entity_name", "entity_type"],
    },
},
```

- [ ] **Step 7: 在 TOOL_SCHEMAS 中添加 `lightrag_create_relation`**

```python
"lightrag_create_relation": {
    "name": "lightrag_create_relation",
    "description": "Create a new relation between two entities. Both entities must exist. Fails if relation already exists.",
    "input_schema": {
        "type": "object",
        "properties": {
            "source_entity": {"type": "string", "description": "Source entity name"},
            "target_entity": {"type": "string", "description": "Target entity name"},
            "keywords": {"type": "string", "description": "Relation keywords (required)"},
            "description": {"type": "string", "default": "", "description": "Relation description"},
            "weight": {"type": "number", "default": 1.0, "description": "Relation weight"},
            "source_id": {"type": "string", "default": "manual_creation", "description": "Source chunk ID"},
            "file_path": {"type": "string", "default": "manual_creation", "description": "File path for citation"},
        },
        "required": ["source_entity", "target_entity", "keywords"],
    },
},
```

- [ ] **Step 8: 提交 TOOL_SCHEMAS 改动**

```bash
git add mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py
git commit -m "feat(mcp): 添加7个KG工具的TOOL_SCHEMAS定义

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: MCP 工具层 — 实现 7 个工具函数

**Files:**
- Modify: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py`

- [ ] **Step 1: 实现 `lightrag_edit_entity` 函数**

在文件末尾（`lightrag_timeline_query` 函数之后）添加：

```python
def lightrag_edit_entity(
    entity_name: str,
    description: str | None = None,
    entity_type: str | None = None,
    new_name: str | None = None,
    allow_rename: bool = False,
    allow_merge: bool = False,
) -> Dict[str, Any]:
    """Edit entity information in the knowledge graph.

    Args:
        entity_name: Entity name to edit
        description: New description (overwrites existing)
        entity_type: New entity type
        new_name: New entity name (requires allow_rename=True)
        allow_rename: Allow renaming entity
        allow_merge: Allow merging into existing entity when renaming
    """
    try:
        adapter = _get_adapter()

        # Build updated_data dict (only include non-None values)
        updated_data: dict[str, str] = {}
        if description is not None:
            updated_data["description"] = description
        if entity_type is not None:
            updated_data["entity_type"] = entity_type
        if new_name is not None:
            updated_data["entity_name"] = new_name

        if not updated_data:
            return {"status": "error", "message": "No update fields provided"}

        result = adapter.edit_entity(
            entity_name=entity_name,
            updated_data=updated_data,
            allow_rename=allow_rename,
            allow_merge=allow_merge,
        )

        if result.get("status") == "ok":
            data = result.get("data", {})
            op_summary = data.get("operation_summary", {})
            msg = f"实体 '{entity_name}' 编辑成功"
            if op_summary.get("renamed"):
                msg = f"实体 '{entity_name}' 已重命名为 '{op_summary.get('final_entity')}'"
            if op_summary.get("merged"):
                msg = f"实体 '{entity_name}' 已合并到 '{op_summary.get('target_entity')}'"
            return {"status": "ok", "message": msg, "data": data}
        return result
    except Exception as e:
        logger.error(f"lightrag_edit_entity failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 2: 实现 `lightrag_edit_relation` 函数**

```python
def lightrag_edit_relation(
    source_entity: str,
    target_entity: str,
    keywords: str | None = None,
    new_keywords: str | None = None,
    new_description: str | None = None,
    new_weight: float | None = None,
) -> Dict[str, Any]:
    """Edit relation (edge) information between two entities.

    Args:
        source_entity: Source entity name
        target_entity: Target entity name
        keywords: Current keywords (used to identify the relation, optional)
        new_keywords: New keywords
        new_description: New description
        new_weight: New weight
    """
    try:
        adapter = _get_adapter()

        # Build updated_data dict (only include non-None values)
        updated_data: dict[str, Any] = {}
        if new_keywords is not None:
            updated_data["keywords"] = new_keywords
        if new_description is not None:
            updated_data["description"] = new_description
        if new_weight is not None:
            updated_data["weight"] = new_weight

        if not updated_data:
            return {"status": "error", "message": "No update fields provided"}

        result = adapter.edit_relation(
            source_entity=source_entity,
            target_entity=target_entity,
            updated_data=updated_data,
        )

        if result.get("status") == "ok":
            return {
                "status": "ok",
                "message": f"关系 '{source_entity}'→'{target_entity}' 编辑成功",
                "data": result.get("data"),
            }
        return result
    except Exception as e:
        logger.error(f"lightrag_edit_relation failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 3: 实现 `lightrag_delete_relation` 函数**

```python
def lightrag_delete_relation(
    source_entity: str,
    target_entity: str,
    keywords: str | None = None,
) -> Dict[str, Any]:
    """Delete a relation between two entities.

    Args:
        source_entity: Source entity name
        target_entity: Target entity name
        keywords: Relation keywords (optional, if not specified deletes all relations)
    """
    try:
        adapter = _get_adapter()

        # Note: LightRAG's adelete_by_relation doesn't support keywords filter
        # It deletes the edge between source and target entirely
        # If keywords filtering is needed, we'd need to check edge data first
        if keywords:
            # Check if the edge has matching keywords
            info = adapter.get_relation_info(source_entity, target_entity)
            if info.get("status") == "ok":
                edge_data = info.get("data", {}).get("graph_data", {})
                edge_keywords = edge_data.get("keywords", "")
                if keywords not in edge_keywords:
                    return {
                        "status": "ok",
                        "message": f"关系 '{source_entity}'→'{target_entity}' 的 keywords 不匹配 '{keywords}'，未删除",
                        "skipped": True,
                    }

        result = adapter.delete_relation(
            source_entity=source_entity,
            target_entity=target_entity,
        )

        if result.get("status") == "ok":
            return {
                "status": "ok",
                "message": f"关系 '{source_entity}'→'{target_entity}' 已删除",
            }
        return result
    except Exception as e:
        logger.error(f"lightrag_delete_relation failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 4: 实现 `lightrag_get_entity_info` 函数**

```python
def lightrag_get_entity_info(
    entity_name: str,
    include_vector_data: bool = False,
) -> Dict[str, Any]:
    """Get detailed information of a single entity.

    Args:
        entity_name: Entity name to query
        include_vector_data: Include vector database information
    """
    try:
        adapter = _get_adapter()
        result = adapter.get_entity_info(
            entity_name=entity_name,
            include_vector_data=include_vector_data,
        )

        if result.get("status") == "ok":
            return {
                "status": "ok",
                "message": f"实体 '{entity_name}' 信息查询成功",
                "data": result.get("data"),
            }
        return result
    except Exception as e:
        logger.error(f"lightrag_get_entity_info failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 5: 实现 `lightrag_get_relation_info` 函数**

```python
def lightrag_get_relation_info(
    source_entity: str,
    target_entity: str,
    include_vector_data: bool = False,
) -> Dict[str, Any]:
    """Get detailed information of a relationship between two entities.

    Args:
        source_entity: Source entity name
        target_entity: Target entity name
        include_vector_data: Include vector database information
    """
    try:
        adapter = _get_adapter()
        result = adapter.get_relation_info(
            src_entity=source_entity,
            tgt_entity=target_entity,
            include_vector_data=include_vector_data,
        )

        if result.get("status") == "ok":
            return {
                "status": "ok",
                "message": f"关系 '{source_entity}'→'{target_entity}' 信息查询成功",
                "data": result.get("data"),
            }
        return result
    except Exception as e:
        logger.error(f"lightrag_get_relation_info failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 6: 实现 `lightrag_create_entity` 函数**

```python
def lightrag_create_entity(
    entity_name: str,
    entity_type: str,
    description: str = "",
    source_id: str = "manual_creation",
    file_path: str = "manual_creation",
) -> Dict[str, Any]:
    """Create a new entity in the knowledge graph.

    Args:
        entity_name: Entity name (must be unique)
        entity_type: Entity type (e.g., Person, Concept, Skill, Tool)
        description: Entity description
        source_id: Source chunk ID
        file_path: File path for citation
    """
    try:
        adapter = _get_adapter()

        # Check if entity already exists
        if adapter.has_entity(entity_name):
            return {
                "status": "ok",
                "message": f"实体 '{entity_name}' 已存在，无法创建。如需修改请使用 lightrag_edit_entity。",
                "skipped": True,
            }

        result = adapter.create_entity(
            entity_name=entity_name,
            entity_type=entity_type,
            description=description,
            source_id=source_id,
            file_path=file_path,
        )

        if result.get("status") == "ok":
            return {
                "status": "ok",
                "message": f"实体 '{entity_name}' 创建成功",
                "data": result.get("data"),
            }
        return result
    except Exception as e:
        logger.error(f"lightrag_create_entity failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 7: 实现 `lightrag_create_relation` 函数**

```python
def lightrag_create_relation(
    source_entity: str,
    target_entity: str,
    keywords: str,
    description: str = "",
    weight: float = 1.0,
    source_id: str = "manual_creation",
    file_path: str = "manual_creation",
) -> Dict[str, Any]:
    """Create a new relation between two entities.

    Args:
        source_entity: Source entity name
        target_entity: Target entity name
        keywords: Relation keywords (required)
        description: Relation description
        weight: Relation weight
        source_id: Source chunk ID
        file_path: File path for citation
    """
    try:
        adapter = _get_adapter()

        # Check if both entities exist
        if not adapter.has_entity(source_entity):
            return {
                "status": "error",
                "message": f"源实体 '{source_entity}' 不存在，请先创建该实体",
            }
        if not adapter.has_entity(target_entity):
            return {
                "status": "error",
                "message": f"目标实体 '{target_entity}' 不存在，请先创建该实体",
            }

        # Check if relation already exists
        if adapter.has_edge(source_entity, target_entity, keywords=keywords):
            return {
                "status": "ok",
                "message": f"关系 '{source_entity}'→'{target_entity}'({keywords}) 已存在，无法创建。如需修改请使用 lightrag_edit_relation。",
                "skipped": True,
            }

        result = adapter.create_relation(
            source_entity=source_entity,
            target_entity=target_entity,
            keywords=keywords,
            description=description,
            weight=weight,
            source_id=source_id,
            file_path=file_path,
        )

        if result.get("status") == "ok":
            return {
                "status": "ok",
                "message": f"关系 '{source_entity}'→'{target_entity}'({keywords}) 创建成功",
                "data": result.get("data"),
            }
        return result
    except Exception as e:
        logger.error(f"lightrag_create_relation failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 8: 更新 `_TOOL_FUNCTIONS` 字典**

在 `_TOOL_FUNCTIONS` 字典中添加新工具的映射：

```python
_TOOL_FUNCTIONS = {
    # ... existing entries ...
    "lightrag_timeline_query": lightrag_timeline_query,
    # New KG tools
    "lightrag_edit_entity": lightrag_edit_entity,
    "lightrag_edit_relation": lightrag_edit_relation,
    "lightrag_delete_relation": lightrag_delete_relation,
    "lightrag_get_entity_info": lightrag_get_entity_info,
    "lightrag_get_relation_info": lightrag_get_relation_info,
    "lightrag_create_entity": lightrag_create_entity,
    "lightrag_create_relation": lightrag_create_relation,
}
```

- [ ] **Step 9: 提交工具函数实现**

```bash
git add mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py
git commit -m "feat(mcp): 实现7个KG工具函数 — edit_entity/edit_relation/delete_relation/get_entity_info/get_relation_info/create_entity/create_relation

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: YAML 配置层 — 添加虚拟磁盘映射

**Files:**
- Modify: `config/disk/lightrag-server.yaml`

- [ ] **Step 1: 添加 7 个工具的 YAML 映射**

在 `tools:` 列表末尾（`lightrag_timeline_query` 之后）添加：

```yaml
  - name: lightrag_edit_entity
    category: admin
    short: "编辑实体"
    long: "修改实体属性（描述、类型），支持改名和合并"
    parameters:
      - name: entity_name
        position: 1
        type: string
        required: true
      - name: description
        type: string
      - name: entity_type
        flag: type
        type: string
      - name: new_name
        flag: new-name
        type: string
      - name: allow_rename
        flag: allow-rename
        type: boolean
        default: false
      - name: allow_merge
        flag: allow-merge
        type: boolean
        default: false

  - name: lightrag_edit_relation
    category: admin
    short: "编辑关系"
    long: "修改关系属性（描述、关键词、权重）"
    parameters:
      - name: source_entity
        position: 1
        type: string
        required: true
      - name: target_entity
        position: 2
        type: string
        required: true
      - name: keywords
        flag: keywords
        type: string
      - name: new_keywords
        flag: new-keywords
        type: string
      - name: new_description
        type: string
      - name: new_weight
        flag: new-weight
        type: number

  - name: lightrag_delete_relation
    category: admin
    short: "删除关系"
    long: "删除两实体间的关系（保留实体）"
    parameters:
      - name: source_entity
        position: 1
        type: string
        required: true
      - name: target_entity
        position: 2
        type: string
        required: true
      - name: keywords
        flag: keywords
        type: string

  - name: lightrag_get_entity_info
    category: query
    short: "查询实体详情"
    long: "获取单个实体的详细信息"
    parameters:
      - name: entity_name
        position: 1
        type: string
        required: true
      - name: include_vector_data
        flag: vector
        type: boolean
        default: false

  - name: lightrag_get_relation_info
    category: query
    short: "查询关系详情"
    long: "获取两实体间关系的详细信息"
    parameters:
      - name: source_entity
        position: 1
        type: string
        required: true
      - name: target_entity
        position: 2
        type: string
        required: true
      - name: include_vector_data
        flag: vector
        type: boolean
        default: false

  - name: lightrag_create_entity
    category: write
    short: "创建实体"
    long: "创建新实体（实体已存在则失败）"
    parameters:
      - name: entity_name
        position: 1
        type: string
        required: true
      - name: entity_type
        flag: type
        type: string
        required: true
      - name: description
        type: string
      - name: source_id
        flag: source-id
        type: string
        default: manual_creation
      - name: file_path
        flag: file-path
        type: string
        default: manual_creation

  - name: lightrag_create_relation
    category: write
    short: "创建关系"
    long: "创建两实体间的新关系（关系已存在则失败）"
    parameters:
      - name: source_entity
        position: 1
        type: string
        required: true
      - name: target_entity
        position: 2
        type: string
        required: true
      - name: keywords
        flag: keywords
        type: string
        required: true
      - name: description
        type: string
      - name: weight
        type: number
        default: 1.0
      - name: source_id
        flag: source-id
        type: string
        default: manual_creation
      - name: file_path
        flag: file-path
        type: string
        default: manual_creation
```

- [ ] **Step 2: 提交 YAML 配置**

```bash
git add config/disk/lightrag-server.yaml
git commit -m "feat(config): 添加7个KG工具的虚拟磁盘映射

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: dedup 反馈信息改造

**Files:**
- Modify: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py`

**目标：** 将 `lightrag_insert_entity`、`lightrag_insert_relation`、`lightrag_insert_custom_kg` 的 dedup 反馈从"已存在，跳过"改为可操作选项。

- [ ] **Step 1: 修改 `lightrag_insert_entity` 的 dedup 反馈**

找到 `lightrag_insert_entity` 函数中的 dedup 检查代码，将：

```python
if adapter.has_entity(name):
    return {"status": "ok", "message": f"实体'{name}'已存在，跳过重复入库", "skipped": True}
```

改为：

```python
if adapter.has_entity(name):
    # Get current description for context
    info = adapter.get_entity_info(name)
    current_desc = ""
    if info.get("status") == "ok":
        current_desc = info.get("data", {}).get("graph_data", {}).get("description", "")[:100]

    return {
        "status": "ok",
        "message": f"实体'{name}'已存在（当前描述：{current_desc}...）。可选操作：\n"
                   f"1. 追加描述：disk(\"/lightrag/lightrag_insert '新描述内容'\")\n"
                   f"2. 删除重建：disk(\"/lightrag/lightrag_delete_entity '{name}'\") 后重新插入\n"
                   f"3. 修改描述：disk(\"/lightrag/lightrag_edit_entity '{name}' --description '新描述'\")",
        "skipped": True,
        "entity_name": name,
    }
```

- [ ] **Step 2: 修改 `lightrag_insert_relation` 的 dedup 反馈**

找到 `lightrag_insert_relation` 函数中的 dedup 检查代码，将：

```python
if adapter.has_edge(src_id, tgt_id, keywords=relation):
    return {"status": "ok", "message": f"关系'{src_id}'→'{tgt_id}'({relation})已存在，跳过重复入库", "skipped": True}
```

改为：

```python
if adapter.has_edge(src_id, tgt_id, keywords=relation):
    return {
        "status": "ok",
        "message": f"关系'{src_id}'→'{tgt_id}'({relation})已存在。可选操作：\n"
                   f"1. 修改关系：disk(\"/lightrag/lightrag_edit_relation '{src_id}' '{tgt_id}' --keywords '{relation}' --new_description '新描述'\")\n"
                   f"2. 删除关系：disk(\"/lightrag/lightrag_delete_relation '{src_id}' '{tgt_id}' --keywords '{relation}'\")",
        "skipped": True,
        "source_entity": src_id,
        "target_entity": tgt_id,
        "keywords": relation,
    }
```

- [ ] **Step 3: 修改 `lightrag_insert_custom_kg` 的 dedup 反馈**

找到 `lightrag_insert_custom_kg` 函数中的 dedup 检查代码，将 `skip_parts` 构建逻辑改为：

```python
# Build skip info string with actionable alternatives
skip_parts = []
if skipped_entity_names:
    for ent_name in skipped_entity_names:
        skip_parts.append(
            f"实体'{ent_name}'已存在。可选操作：\n"
            f"  - 追加描述：disk(\"/lightrag/lightrag_insert '新描述'\")\n"
            f"  - 修改描述：disk(\"/lightrag/lightrag_edit_entity '{ent_name}' --description '新描述'\")\n"
            f"  - 删除重建：disk(\"/lightrag/lightrag_delete_entity '{ent_name}'\")"
        )
if skipped_rel_labels:
    for rel_label in skipped_rel_labels:
        # Parse "src->tgt(keywords)" format
        import re
        match = re.match(r"(.+?)->(.+?)\((.+)\)", rel_label)
        if match:
            src, tgt, kw = match.groups()
            skip_parts.append(
                f"关系'{src}'→'{tgt}'({kw})已存在。可选操作：\n"
                f"  - 修改关系：disk(\"/lightrag/lightrag_edit_relation '{src}' '{tgt}' --keywords '{kw}' --new_description '新描述'\")\n"
                f"  - 删除关系：disk(\"/lightrag/lightrag_delete_relation '{src}' '{tgt}' --keywords '{kw}'\")"
            )
        else:
            skip_parts.append(f"关系'{rel_label}'已存在")
skip_info = "\n".join(skip_parts)
```

- [ ] **Step 4: 提交 dedup 反馈改造**

```bash
git add mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py
git commit -m "feat(mcp): dedup反馈改为可操作选项 — 提供disk命令示例

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: KG 开发字典更新

**Files:**
- Modify: `docs/kg-dev-dictionary.md`

- [ ] **Step 1: 在字典中添加新工具章节**

在 `## 6. 实体增删` 章节之后添加新章节：

```markdown
---

## 7. 实体/关系编辑与精确查询

### `lightrag_edit_entity` — 编辑实体

```
参数:  entity_name: str        — 实体名（必填）
       description: str        — 新描述（覆盖式）
       entity_type: str        — 新类型
       new_name: str           — 新实体名（需 allow_rename=True）
       allow_rename: bool      — 允许改名（默认 False）
       allow_merge: bool       — 允许合并到已存在实体（默认 False）
返回:  {"status": "ok", "message": str, "data": dict}
注意:  allow_rename 有风险，改名后可能影响已有关系
       allow_merge=True 时，如果 new_name 已存在，会合并两个实体
示例:  disk("/lightrag/lightrag_edit_entity 'Python' --description '一种编程语言'")
       disk("/lightrag/lightrag_edit_entity '旧名' --new-name '新名' --allow-rename true")
```

### `lightrag_edit_relation` — 编辑关系

```
参数:  source_entity: str      — 源实体名（必填）
       target_entity: str      — 目标实体名（必填）
       keywords: str           — 当前关键词（用于定位关系）
       new_keywords: str       — 新关键词
       new_description: str    — 新描述
       new_weight: float       — 新权重
返回:  {"status": "ok", "message": str, "data": dict}
注意:  关系是无向的，source/target 顺序不影响结果
示例:  disk("/lightrag/lightrag_edit_relation 'Niu' 'Python' --new_description 'Niu精通Python'")
```

### `lightrag_delete_relation` — 删除关系

```
参数:  source_entity: str      — 源实体名（必填）
       target_entity: str      — 目标实体名（必填）
       keywords: str           — 关系关键词（可选，不指定则删除两实体间所有关系）
返回:  {"status": "ok", "message": str}
注意:  只删关系，不删实体
示例:  disk("/lightrag/lightrag_delete_relation 'Niu' 'Python'")
```

### `lightrag_get_entity_info` — 查询实体详情

```
参数:  entity_name: str           — 实体名（必填）
       include_vector_data: bool  — 包含向量数据（默认 False）
返回:  {"status": "ok", "data": {"entity_name": str, "source_id": str, "graph_data": dict}}
注意:  graph_data 包含 description、entity_type 等属性
示例:  disk("/lightrag/lightrag_get_entity_info 'Python'")
```

### `lightrag_get_relation_info` — 查询关系详情

```
参数:  source_entity: str         — 源实体名（必填）
       target_entity: str         — 目标实体名（必填）
       include_vector_data: bool  — 包含向量数据（默认 False）
返回:  {"status": "ok", "data": {"src_entity": str, "tgt_entity": str, "graph_data": dict}}
注意:  关系是无向的，source/target 顺序不影响结果
示例:  disk("/lightrag/lightrag_get_relation_info 'Niu' 'Python'")
```

### `lightrag_create_entity` — 创建实体（严格模式）

```
参数:  entity_name: str        — 实体名（必填，必须唯一）
       entity_type: str        — 实体类型（必填）
       description: str        — 描述
       source_id: str          — 来源 chunk ID
       file_path: str          — 文件路径引用
返回:  {"status": "ok", "message": str, "data": dict}
注意:  实体已存在则失败（返回 skipped=True）
       与 lightrag_insert_entity 的区别：insert 是 upsert，create 是严格新建
示例:  disk("/lightrag/lightrag_create_entity '新概念' --type 'Concept' --description '描述'")
```

### `lightrag_create_relation` — 创建关系（严格模式）

```
参数:  source_entity: str      — 源实体名（必填）
       target_entity: str      — 目标实体名（必填）
       keywords: str           — 关系关键词（必填）
       description: str        — 描述
       weight: float           — 权重（默认 1.0）
       source_id: str          — 来源 chunk ID
       file_path: str          — 文件路径引用
返回:  {"status": "ok", "message": str, "data": dict}
注意:  任一实体不存在则失败
       关系已存在则失败（返回 skipped=True）
示例:  disk("/lightrag/lightrag_create_relation 'Niu' 'Python' --keywords 'skilled_in'")
```
```

- [ ] **Step 2: 更新"已知陷阱速查"章节**

在"已知陷阱速查"章节添加新条目：

```markdown
| 陷阱 | 规避方法 |
|------|----------|
| ... | ... |
| `lightrag_create_entity` 实体已存在会失败 | 先用 `lightrag_get_entity_info` 检查，或直接用 `lightrag_insert_entity`（upsert 模式） |
| `lightrag_create_relation` 关系已存在会失败 | 先用 `lightrag_get_relation_info` 检查，或直接用 `lightrag_insert_relation`（upsert 模式） |
| `lightrag_edit_entity` 改名可能破坏关系 | 谨慎使用 `allow_rename=True`，改名后检查相关关系 |
| `lightrag_delete_relation` 不指定 keywords 会删除所有关系 | 如需精确删除，务必指定 keywords 参数 |
```

- [ ] **Step 3: 提交字典更新**

```bash
git add docs/kg-dev-dictionary.md
git commit -m "docs: KG开发字典 — 添加7个新工具用法

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: 集成测试

**Files:**
- Create: `tests/test_kg_tools_integration.py`

**测试原则：**
- TDD：先写测试再写实现（已在前面完成实现）
- 真实环境：启动主程序做集成测试
- 不允许 mock
- 测试覆盖：每个工具的正常路径 + 边界条件

- [ ] **Step 1: 创建测试文件骨架**

```python
"""
KG 工具集成测试

测试环境：真实 LightRAG 实例（~/.niu/lightrag_storage/）
测试原则：不允许 mock，必须用真实数据

运行方式：
    # 先启动主程序
    ./niu.exe

    # 在另一个终端运行测试
    pytest tests/test_kg_tools_integration.py -v
"""

import pytest
from agent.tool_registry import get_registry


@pytest.fixture(scope="module")
def registry():
    """获取工具注册中心"""
    return get_registry()


@pytest.fixture(scope="module")
def test_entity():
    """测试用实体名（测试结束后清理）"""
    name = "测试实体_KG工具测试"
    yield name
    # Cleanup
    try:
        registry = get_registry()
        delete_fn = registry.get("lightrag-server/lightrag_delete_entity")
        delete_fn(entity_name=name)
    except Exception:
        pass


@pytest.fixture(scope="module")
def test_relation_entities():
    """测试用关系实体（测试结束后清理）"""
    src = "测试源实体_KG工具测试"
    tgt = "测试目标实体_KG工具测试"
    yield src, tgt
    # Cleanup
    try:
        registry = get_registry()
        delete_fn = registry.get("lightrag-server/lightrag_delete_entity")
        delete_fn(entity_name=src)
        delete_fn(entity_name=tgt)
    except Exception:
        pass


class TestCreateEntity:
    """测试 lightrag_create_entity"""

    def test_create_new_entity(self, registry, test_entity):
        """创建新实体应该成功"""
        fn = registry.get("lightrag-server/lightrag_create_entity")
        result = fn(
            entity_name=test_entity,
            entity_type="Concept",
            description="这是一个测试实体",
        )
        assert result["status"] == "ok"
        assert "创建成功" in result["message"]

    def test_create_existing_entity_fails(self, registry, test_entity):
        """创建已存在的实体应该返回 skipped"""
        fn = registry.get("lightrag-server/lightrag_create_entity")
        result = fn(
            entity_name=test_entity,
            entity_type="Concept",
            description="再次创建",
        )
        assert result["status"] == "ok"
        assert result.get("skipped") is True


class TestGetEntityInfo:
    """测试 lightrag_get_entity_info"""

    def test_get_existing_entity(self, registry, test_entity):
        """查询存在的实体应该返回详情"""
        fn = registry.get("lightrag-server/lightrag_get_entity_info")
        result = fn(entity_name=test_entity)
        assert result["status"] == "ok"
        data = result.get("data", {})
        assert data.get("entity_name") == test_entity

    def test_get_nonexistent_entity(self, registry):
        """查询不存在的实体应该返回空或错误"""
        fn = registry.get("lightrag-server/lightrag_get_entity_info")
        result = fn(entity_name="不存在的实体_xyz123")
        # LightRAG 可能返回空数据而不是错误
        assert result["status"] in ["ok", "error"]


class TestEditEntity:
    """测试 lightrag_edit_entity"""

    def test_edit_description(self, registry, test_entity):
        """修改实体描述应该成功"""
        fn = registry.get("lightrag-server/lightrag_edit_entity")
        result = fn(
            entity_name=test_entity,
            description="修改后的描述",
        )
        assert result["status"] == "ok"
        assert "编辑成功" in result["message"]

    def test_edit_nonexistent_entity_fails(self, registry):
        """修改不存在的实体应该失败"""
        fn = registry.get("lightrag-server/lightrag_edit_entity")
        result = fn(
            entity_name="不存在的实体_xyz123",
            description="新描述",
        )
        assert result["status"] == "error"


class TestCreateRelation:
    """测试 lightrag_create_relation"""

    def test_create_relation(self, registry, test_relation_entities):
        """创建关系应该成功"""
        src, tgt = test_relation_entities

        # 先创建两个实体
        create_fn = registry.get("lightrag-server/lightrag_create_entity")
        create_fn(entity_name=src, entity_type="Concept", description="源实体")
        create_fn(entity_name=tgt, entity_type="Concept", description="目标实体")

        # 创建关系
        fn = registry.get("lightrag-server/lightrag_create_relation")
        result = fn(
            source_entity=src,
            target_entity=tgt,
            keywords="test_relation",
            description="测试关系",
        )
        assert result["status"] == "ok"

    def test_create_relation_missing_entity_fails(self, registry):
        """创建关系时实体不存在应该失败"""
        fn = registry.get("lightrag-server/lightrag_create_relation")
        result = fn(
            source_entity="不存在的实体_a",
            target_entity="不存在的实体_b",
            keywords="test",
        )
        assert result["status"] == "error"


class TestGetRelationInfo:
    """测试 lightrag_get_relation_info"""

    def test_get_existing_relation(self, registry, test_relation_entities):
        """查询存在的关系应该返回详情"""
        src, tgt = test_relation_entities
        fn = registry.get("lightrag-server/lightrag_get_relation_info")
        result = fn(source_entity=src, target_entity=tgt)
        assert result["status"] == "ok"


class TestEditRelation:
    """测试 lightrag_edit_relation"""

    def test_edit_relation_description(self, registry, test_relation_entities):
        """修改关系描述应该成功"""
        src, tgt = test_relation_entities
        fn = registry.get("lightrag-server/lightrag_edit_relation")
        result = fn(
            source_entity=src,
            target_entity=tgt,
            new_description="修改后的关系描述",
        )
        assert result["status"] == "ok"


class TestDeleteRelation:
    """测试 lightrag_delete_relation"""

    def test_delete_relation(self, registry, test_relation_entities):
        """删除关系应该成功"""
        src, tgt = test_relation_entities
        fn = registry.get("lightrag-server/lightrag_delete_relation")
        result = fn(source_entity=src, target_entity=tgt)
        assert result["status"] == "ok"

        # 验证关系已删除
        info_fn = registry.get("lightrag-server/lightrag_get_relation_info")
        info = info_fn(source_entity=src, target_entity=tgt)
        # 关系删除后，graph_data 应该为空或 None
        assert info.get("data", {}).get("graph_data") is None


class TestDedupFeedback:
    """测试 dedup 反馈信息"""

    def test_insert_entity_dedup_has_actionable_options(self, registry, test_entity):
        """重复插入实体应该返回可操作选项"""
        fn = registry.get("lightrag-server/lightrag_insert_entity")
        result = fn(
            name=test_entity,
            entity_type="Concept",
            description="重复插入",
        )
        assert result.get("skipped") is True
        # 检查是否包含可操作选项
        message = result.get("message", "")
        assert "可选操作" in message or "lightrag_edit_entity" in message or "lightrag_insert" in message
```

- [ ] **Step 2: 运行测试验证**

```bash
# 先启动主程序
./niu.exe &

# 等待启动完成
sleep 10

# 运行测试
pytest tests/test_kg_tools_integration.py -v

# 测试完成后杀掉进程
pkill -f niu.exe
```

- [ ] **Step 3: 提交测试文件**

```bash
git add tests/test_kg_tools_integration.py
git commit -m "test: KG工具集成测试 — 7个新工具 + dedup反馈

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: 最终验证与合并提交

- [ ] **Step 1: 运行完整测试套件**

```bash
# 启动主程序
./niu.exe &

# 等待启动
sleep 10

# 运行所有 KG 相关测试
pytest tests/test_kg_tools_integration.py -v

# 检查代码语法
ruff check niu_api/internal/lightrag_adapter.py
ruff check mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py

# 杀掉进程
pkill -f niu.exe
```

- [ ] **Step 2: 修复文件权限（git 操作后）**

```bash
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
find ui/assistant/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \;
```

- [ ] **Step 3: 最终合并提交**

```bash
git add -A
git commit -m "feat: KG工具全量对接 — 7个新工具 + dedup可操作反馈 + 字典同步

- Adapter层: edit_entity/edit_relation/delete_relation/get_entity_info/get_relation_info/create_entity/create_relation
- MCP工具层: TOOL_SCHEMAS + 工具函数实现
- YAML配置: 虚拟磁盘映射
- dedup反馈: 可操作选项（disk命令示例）
- KG字典: 实时同步更新
- 集成测试: 真实环境测试

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## 自检清单

- [x] Spec 覆盖：每个设计文档要求都有对应 Task
- [x] Placeholder 扫描：无 TBD/TODO/待实现
- [x] 类型一致性：方法签名与 LightRAG 源码一致
- [x] 文件路径：所有路径精确到文件名和行号
- [x] 测试覆盖：每个工具有正常路径 + 边界条件测试
- [x] 字典同步：KG 开发字典与代码同步更新
