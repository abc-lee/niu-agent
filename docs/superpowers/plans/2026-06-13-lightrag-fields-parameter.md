# lightrag_query_data fields 参数 + 截断应对提示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为知识图谱查询工具添加 `fields` 可选参数，控制返回字段以缩小输出量；在工具描述中添加截断应对提示，指导 LLM 在结果被截断时如何调整参数。

**Architecture:** 1) 在 MCP 工具层（lightrag_query_data、lightrag_search_entities）添加 `fields` 可选参数，返回前对实体/关系/chunk 做字段裁剪；2) 在 lightrag_adapter.py 的 query_data 方法透传 fields 参数；3) 在 TOOL_SCHEMAS 和磁盘配置中同步更新；4) 在工具描述中补充截断应对提示。缺省不传 fields 时行为不变（全量输出）。

**Tech Stack:** Python, lightrag-server MCP, lightrag_adapter.py, YAML 配置

---

## File Structure

| File | Responsibility |
|------|---------------|
| `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` | MCP 工具函数 + TOOL_SCHEMAS：添加 fields 参数 + 截断提示 |
| `niu_api/internal/lightrag_adapter.py` | Adapter 透传 fields 参数 + 字段裁剪逻辑 |
| `config/disk/lightrag-server.yaml` | 磁盘工具配置：添加 fields 参数 |
| `config/agents/niu.md` | 主 Agent 定义：更新知识图谱操作提示 |

---

### Task 1: Adapter 层添加字段裁剪逻辑

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py:189-245`

**设计：**
- 在 `query_data` 方法中添加 `fields` 可选参数（`Optional[List[str]] = None`）
- 当 `fields` 不为 None 时，对返回结果中的 entities/relationships/chunks 做字段裁剪
- 裁剪逻辑放在 Adapter 层而非 LightRAG 原生层（不改 LightRAG 代码）
- 可选字段列表：
  - entities: `entity_name`, `entity_type`, `description`, `source_id`, `file_path`, `created_at`
  - relationships: `src_id`, `tgt_id`, `description`, `keywords`, `weight`, `source_id`, `file_path`, `created_at`
  - chunks: `content`, `file_path`, `chunk_id`

- [ ] **Step 1: 在 lightrag_adapter.py 中添加字段裁剪函数**

在 `LightRAGAdapter` 类之前（约第 30 行附近）添加：

```python
def _filter_result_fields(result: dict, fields: list) -> dict:
    """对 query_data 返回结果做字段裁剪，只保留指定字段。

    Args:
        result: query_data 返回的完整结果 dict
        fields: 要保留的字段名列表。None 或空列表表示不过滤。

    Returns:
        裁剪后的结果 dict（原地修改 result 中的 data 部分）
    """
    if not fields:
        return result
    field_set = set(fields)
    data = result.get("data", {})
    # 裁剪 entities
    if "entities" in data:
        data["entities"] = [
            {k: v for k, v in ent.items() if k in field_set}
            for ent in data["entities"]
        ]
    # 裁剪 relationships
    if "relationships" in data:
        data["relationships"] = [
            {k: v for k, v in rel.items() if k in field_set}
            for rel in data["relationships"]
        ]
    # 裁剪 chunks
    if "chunks" in data:
        data["chunks"] = [
            {k: v for k, v in ch.items() if k in field_set}
            for ch in data["chunks"]
        ]
    return result
```

- [ ] **Step 2: 在 query_data 方法中添加 fields 参数并调用裁剪**

`niu_api/internal/lightrag_adapter.py:189`，将 `query_data` 方法签名改为：

```python
    def query_data(
        self,
        query: str,
        mode: str = "local",
        top_k: Optional[int] = None,
        keywords: Optional[List[str]] = None,
        filter_lambda=None,
        fields: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
```

在 `docstring` 的 Args 部分添加：

```python
            fields: Optional list of field names to include in the output.
                When provided, only these fields are kept in each entity/relationship/chunk.
                Common choices: ["entity_name", "entity_type"] for name-only lists.
                None (default) returns all fields (no filtering).
```

在方法末尾，`return result` 之前添加裁剪调用：

```python
            result = call_async(rag.aquery_data(query, param=param), timeout=120)
            if fields:
                result = _filter_result_fields(result, fields)
            return result
```

- [ ] **Step 3: 验证语法**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python3 -c "import py_compile; py_compile.compile('niu_api/internal/lightrag_adapter.py', doraise=True); print('OK')"`

Expected: OK

- [ ] **Step 4: Commit**

```bash
git add niu_api/internal/lightrag_adapter.py
git commit -m "feat: add fields parameter to lightrag_adapter.query_data"
```

---

### Task 2: MCP 工具层添加 fields 参数 + 截断应对提示

**Files:**
- Modify: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py`

**设计：**
- `lightrag_query_data` 和 `lightrag_search_entities` 都添加 `fields` 可选参数
- 在 TOOL_SCHEMAS 的 description 中添加截断应对提示
- 在函数实现中透传 fields 参数给 adapter.query_data()

- [ ] **Step 1: 更新 TOOL_SCHEMAS 中 lightrag_query_data 的定义**

`__init__.py:153-190`，将 `lightrag_query_data` 的 TOOL_SCHEMAS 替换为：

```python
    "lightrag_query_data": {
        "name": "lightrag_query_data",
        "description": (
            "Query the knowledge base returning structured data (entities + relationships + chunks). "
            "MODES: 'local' (entity-centric graph traversal, RECOMMENDED for most queries), "
            "'global' (community-level overview), 'hybrid' (local+global combined, slower), "
            "'naive' (vector-only, NO graph data), 'mix' (all combined, slowest). "
            "KEY OPTIMIZATION: When you provide 'keywords', the query skips LLM keyword extraction "
            "and uses your keywords directly — this eliminates LLM latency (~10-100s -> <1s) while "
            "keeping full graph traversal capability. ALWAYS provide keywords when you know the search "
            "terms (e.g., query='便签' keywords=['便签']). Only omit keywords for complex natural "
            "language queries that need LLM interpretation.\n\n"
            "TRUNCATION AVOIDANCE: If results are truncated ([截断] marker appears), take these steps:\n"
            "1. Reduce top_k (e.g., 10→5→3)\n"
            "2. Switch to narrower mode: mix→hybrid→local\n"
            "3. Provide more specific keywords (exact entity names work best)\n"
            "4. Use fields=['entity_name','entity_type'] to get name-only lists without descriptions\n"
            "5. Use lightrag_get_entity_info for single-entity detail instead of broad query"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
                "mode": {
                    "type": "string",
                    "enum": ["naive", "local", "global", "hybrid", "mix", "bypass"],
                    "default": "local",
                    "description": "Retrieval mode. 'local' is best for finding specific entities. 'hybrid' adds community context but is slower. 'naive' skips graph entirely.",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Pre-provided keywords to skip LLM extraction. DRAMATICALLY faster. Use the core nouns/terms from your query. E.g., query='查看便签' -> keywords=['便签']. For 'local' mode these become ll_keywords; for 'global'/'hybrid' they become both hl and ll keywords.",
                },
                "top_k": {
                    "type": "integer",
                    "default": 10,
                    "description": "Number of top results to retrieve",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional field names to include in output. When provided, only these fields are kept per entity/relationship/chunk, reducing output size. Common choices: ['entity_name','entity_type'] for name-only lists. Default: all fields (no filtering). Available entity fields: entity_name, entity_type, description, source_id, file_path, created_at. Available relationship fields: src_id, tgt_id, description, keywords, weight, source_id, file_path, created_at.",
                },
            },
            "required": ["query"],
        },
    },
```

- [ ] **Step 2: 更新 TOOL_SCHEMAS 中 lightrag_search_entities 的定义**

`__init__.py:192-221`，将 `lightrag_search_entities` 的 TOOL_SCHEMAS 替换为：

```python
    "lightrag_search_entities": {
        "name": "lightrag_search_entities",
        "description": (
            "Search for entities of a specific type in the knowledge graph. "
            "Uses local mode (entity-focused) and filters by entity_type. "
            "Common types: skill, tool, knowledge, person, photo, concept.\n\n"
            "TRUNCATION AVOIDANCE: If results are truncated, reduce top_k, provide specific keywords, "
            "or use fields=['entity_name','entity_type'] to get compact name-only lists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
                "entity_type": {
                    "type": "string",
                    "default": "",
                    "description": "Entity type to filter (skill, tool, knowledge, person, photo, concept)",
                },
                "top_k": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max results",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "提供keywords时跳过LLM关键词提取，近即时返回（<1秒）；不提供时由LightRAG自动提取（5-30秒，依赖LLM可用）。推荐提供keywords以获得最佳性能。从查询中提取核心名词/术语作为keywords。",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional field names to include in output. E.g., ['entity_name','entity_type'] for name-only lists. Default: all fields.",
                },
            },
            "required": ["query"],
        },
    },
```

- [ ] **Step 3: 更新 lightrag_query_data 函数签名和实现**

`__init__.py:681-706`，将 `lightrag_query_data` 函数替换为：

```python
def lightrag_query_data(
    query: str,
    mode: str = "local",
    keywords: Optional[list] = None,
    top_k: int = 10,
    fields: Optional[list] = None,
):
    """Query returning structured data (entities + relationships + chunks).

    When keywords are provided, skips LLM keyword extraction for near-instant
    results while keeping full graph traversal. Without keywords, LLM extraction
    adds 5-30s latency.

    Args:
        fields: Optional list of field names to include. When provided, only
            these fields are kept per entity/relationship/chunk. E.g.,
            fields=["entity_name","entity_type"] returns name-only lists.
            Default None returns all fields.
    """
    valid_modes = {"naive", "local", "global", "hybrid", "mix", "bypass"}
    if mode not in valid_modes:
        return {"status": "error", "message": f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(valid_modes))}"}
    try:
        adapter = _get_adapter()
        result = adapter.query_data(
            query=query, mode=mode, top_k=top_k, keywords=keywords,
            fields=fields,
        )
        if LightRAGAdapter._is_no_result(result):
            return {"status": "no_results", "message": "No relevant results found in knowledge graph"}
        return result
    except Exception as e:
        logger.error(f"lightrag_query_data failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 4: 更新 lightrag_search_entities 函数签名和实现**

`__init__.py:709-725` 附近，将函数签名和调用改为：

```python
def lightrag_search_entities(
    query: str,
    entity_type: str = "",
    top_k: int = 10,
    keywords: Optional[list] = None,
    fields: Optional[list] = None,
) -> Dict[str, Any]:
    """Search for entities of a specific type."""
    try:
        adapter = _get_adapter()
        # 当 entity_type 和 fields 同时提供时，自动包含 entity_type 字段
        # 否则字段裁剪会先于 filter_by_entity_type 执行，导致过滤失效
        if entity_type and fields and "entity_type" not in fields:
            fields = list(fields) + ["entity_type"]
        result = adapter.query_data(query=query, mode="local", top_k=top_k, keywords=keywords, fields=fields)
```

（后续代码不变，只改签名和调用中增加 `fields=fields`）

- [ ] **Step 5: 验证语法**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python3 -c "import py_compile; py_compile.compile('mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py', doraise=True); print('OK')"`

Expected: OK

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py
git commit -m "feat: add fields parameter + truncation hints to lightrag query tools"
```

---

### Task 3: 磁盘工具配置 + photo-server 调用同步

**Files:**
- Modify: `config/disk/lightrag-server.yaml:37-58`
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:2133-2140`

- [ ] **Step 1: 更新磁盘工具配置**

`config/disk/lightrag-server.yaml:37-58`，在 `lightrag_query_data` 的 parameters 列表末尾添加：

```yaml
      - name: fields
        flag: fields
        type: array
        cli_format: repeatable
```

同样在 `lightrag_search_entities` 的 parameters 列表末尾（约第 76-79 行之后）添加：

```yaml
      - name: fields
        flag: fields
        type: array
        cli_format: repeatable
```

- [ ] **Step 2: 确认 photo-server 调用不需要修改**

`mcp-servers/photo-server/src/niu_photo_server/__init__.py:2140`：
```python
result = query_fn(query=target_name, mode="local", keywords=[target_name], top_k=20)
```

此调用不传 fields 参数，走默认全量输出。photo-server 需要完整数据做同名实体合并，不需要裁剪。**无需修改。**

- [ ] **Step 3: Commit**

```bash
git add config/disk/lightrag-server.yaml
git commit -m "feat: add fields parameter to lightrag disk tool config"
```

---

### Task 4: 更新 Niu.MD 主 Agent 定义

**Files:**
- Modify: `config/agents/niu.md`

- [ ] **Step 1: 在知识图谱查询提示后添加截断应对说明**

`config/agents/niu.md` 第 65 行（`常见场景：...` 之后、`## 完整闭环` 之前）插入：

```markdown

**查询结果截断应对**：查询结果可能因数据量大被截断（出现 [截断] 标记）。应对策略：
1. 降低 top_k（10→5→3）
2. 切换更窄的 mode（mix→hybrid→local）
3. 提供更精确的 keywords（用具体实体名而非宽泛词）
4. 使用 fields=['entity_name','entity_type'] 只返回实体名列表，不返回描述——查看大社区（如人际关系）的成员列表时特别有用
5. 查单个实体详情用 `lightrag_get_entity_info`
```

- [ ] **Step 2: Commit**

```bash
git add config/agents/niu.md
git commit -m "docs: add fields parameter and truncation tips to niu.md agent config"
```
