# lightrag_search_entities 移除 entity_type + fields 参数 + 截断应对提示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从 `lightrag_search_entities` 移除 `entity_type` 后置过滤参数（语义搜索不需要类型过滤），为其添加 `fields` 参数和截断应对提示；同时保留 `lightrag_list_entities` 的 `entity_type` 作为"按类型枚举"的正确方案；修复 `lightrag_list_entities` 返回格式中 `id` → `entity_name` 的字段名不一致问题。

**Architecture:** `lightrag_search_entities` 是语义搜索工具，后置过滤 `entity_type` 是有害的（先取 top_k 个全类型实体再筛，可能为 0）。移除后，按类型枚举的需求应使用 `lightrag_list_entities --entity-type`。同时为 `lightrag_search_entities` 添加 `fields` 参数（控制返回字段缩小输出）和截断应对提示（指导 LLM 在结果被截断时如何调整参数）。`lightrag_list_entities` 返回格式统一为 `entity_name` 字段（与 search_entities 一致），避免 LLM 混淆。

**Tech Stack:** Python, lightrag-server MCP, lightrag_adapter.py, YAML 配置

---

## File Structure

| File | Responsibility |
|------|---------------|
| `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` | MCP 工具函数 + TOOL_SCHEMAS：移除 entity_type，添加 fields + 截断提示 |
| `niu_api/internal/lightrag_adapter.py` | Adapter 透传 fields 参数 + 字段裁剪逻辑 + list_entities 字段名修复 |
| `config/disk/lightrag-server.yaml` | 磁盘工具配置：移除 entity_type，添加 fields |
| `config/agents/dream-evolver.md` | 子 Agent：移除 entity_type 参数引用，强化去重指令 |
| `config/agents/entity-extractor.md` | 子 Agent：移除 entity_type 参数引用 |
| `config/agents/niu.md` | 主 Agent 定义：添加截断应对提示 |
| `tests/test_lightrag_server.py` | 测试：删除 test_search_with_type_filter |

---

### Task 1: Adapter 层添加字段裁剪逻辑

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py`

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

在 docstring 的 Args 部分添加：

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

Run: `cd <repo_root> && python3 -c "import py_compile; py_compile.compile('niu_api/internal/lightrag_adapter.py', doraise=True); print('OK')"`

Expected: OK

- [ ] **Step 4: Commit**

```bash
git add niu_api/internal/lightrag_adapter.py
git commit -m "feat: add fields parameter to lightrag_adapter.query_data"
```

---

### Task 2: 修复 lightrag_list_entities 返回格式（id → entity_name）

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py:1179,1202`

`lightrag_list_entities` 返回格式中用 `id` 字段表示实体名，而 `lightrag_search_entities` 用 `entity_name`。统一为 `entity_name`，避免 LLM 混淆。

- [ ] **Step 1: 修改 list_entities 中有 entity_type 过滤时的返回格式**

`niu_api/internal/lightrag_adapter.py:1179`，将：
```python
                            nodes.append({
                                "id": node_id,
                                "entity_type": nt,
                                "description": node_data.get("description", ""),
                            })
```
改为：
```python
                            nodes.append({
                                "entity_name": node_id,
                                "entity_type": nt,
                                "description": node_data.get("description", ""),
                            })
```

- [ ] **Step 2: 修改 list_entities 中无过滤时的返回格式**

`niu_api/internal/lightrag_adapter.py:1201`，将：
```python
                        nodes.append({
                            "id": node.id,
                            "entity_type": node.properties.get("entity_type", "other"),
                            "description": node.properties.get("description", ""),
                        })
```
改为：
```python
                        nodes.append({
                            "entity_name": node.id,
                            "entity_type": node.properties.get("entity_type", "other"),
                            "description": node.properties.get("description", ""),
                        })
```

- [ ] **Step 3: 验证语法**

Run: `cd <repo_root> && python3 -c "import py_compile; py_compile.compile('niu_api/internal/lightrag_adapter.py', doraise=True); print('OK')"`

Expected: OK

- [ ] **Step 4: Commit**

```bash
git add niu_api/internal/lightrag_adapter.py
git commit -m "fix: unify list_entities return field id → entity_name for consistency with search_entities"
```

---

### Task 3: MCP 工具层：移除 entity_type + 添加 fields + 截断提示

**Files:**
- Modify: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py`

- [ ] **Step 1: 更新 TOOL_SCHEMAS 中 lightrag_search_entities 的定义**

`__init__.py:203-239`，将 `lightrag_search_entities` 的 TOOL_SCHEMAS 替换为：

```python
    "lightrag_search_entities": {
        "name": "lightrag_search_entities",
        "description": (
            "Search for entities in the knowledge graph using semantic search (local mode). "
            "Returns entities related to your query. For listing ALL entities of a specific type "
            "(e.g., all persons), use lightrag_list_entities with entity_type filter instead.\n\n"
            "TRUNCATION AVOIDANCE: If results are truncated, take these steps:\n"
            "1. Reduce top_k (e.g., 10→5→3)\n"
            "2. Provide more specific keywords (exact entity names work best)\n"
            "3. Use fields=['entity_name','entity_type'] to get name-only lists without descriptions\n"
            "4. Use lightrag_get_entity_info for single-entity detail instead of broad query"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
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

- [ ] **Step 2: 更新 lightrag_search_entities 函数签名和实现**

`__init__.py:735-763`，将整个函数替换为：

```python
def lightrag_search_entities(
    query: str,
    top_k: int = 10,
    keywords: Optional[list] = None,
    fields: Optional[list] = None,
) -> Dict[str, Any]:
    """Search for entities in the knowledge graph using semantic search."""
    try:
        adapter = _get_adapter()
        result = adapter.query_data(query=query, mode="local", top_k=top_k, keywords=keywords, fields=fields)
        if LightRAGAdapter._is_no_result(result):
            return {"status": "no_results", "message": "No relevant results found in knowledge graph"}
        data = result.get("data", result) if isinstance(result, dict) else {}
        if isinstance(data, list):
            return {"status": "ok", "data": data}
        entities = data.get("entities", []) if isinstance(data, dict) else []
        return {"status": "ok", "data": entities}
    except Exception as e:
        logger.error(f"lightrag_search_entities failed: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 3: 验证语法**

Run: `cd <repo_root> && python3 -c "import py_compile; py_compile.compile('mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py', doraise=True); print('OK')"`

Expected: OK

- [ ] **Step 4: Commit**

```bash
git add mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py
git commit -m "feat: remove entity_type from search_entities, add fields + truncation hints"
```

---

### Task 4: 磁盘工具配置 + 测试更新

**Files:**
- Modify: `config/disk/lightrag-server.yaml:64-87`
- Modify: `tests/test_lightrag_server.py:160-173`

- [ ] **Step 1: 更新磁盘工具配置**

`config/disk/lightrag-server.yaml`，将 `lightrag_search_entities` 的 parameters 部分（行 68-87）改为：

```yaml
    parameters:
      - name: query
        position: 1
        type: string
        required: true
      - name: top_k
        flag: top-k
        type: integer
        default: 10
      - name: keywords
        flag: keywords
        type: array
        cli_format: repeatable
      - name: fields
        flag: fields
        type: array
        cli_format: repeatable
```

即删除 `entity_type` 的3行（原行73-75），保留其他参数不变。

- [ ] **Step 2: 更新测试文件**

`tests/test_lightrag_server.py:160-173`，删除 `test_search_with_type_filter` 测试（因为 entity_type 参数已移除）。在同一个 `TestLightragSearchEntities` 类中，新增一个测试验证 `fields` 参数：

```python
    def test_search_with_fields(self):
        """Should pass fields parameter to adapter."""
        mod = _import_module()
        mock_adapter = MagicMock()
        mock_adapter.query_data.return_value = {
            "data": {"entities": [{"entity_name": "Python", "entity_type": "skill"}]}
        }
        mod._adapter = mock_adapter
        mod.LightRAGAdapter._is_no_result = MagicMock(return_value=False)

        result = mod.lightrag_search_entities(query="python", fields=["entity_name", "entity_type"])

        mock_adapter.query_data.assert_called_once_with(
            query="python", mode="local", top_k=10, keywords=None, fields=["entity_name", "entity_type"]
        )
        assert result["status"] == "ok"
```

- [ ] **Step 3: 验证语法**

Run: `cd <repo_root> && python3 -c "import py_compile; py_compile.compile('tests/test_lightrag_server.py', doraise=True); print('OK')" && python3 -c "import py_compile; py_compile.compile('config/disk/lightrag-server.yaml', doraise=True)" || echo "YAML 不需要 py_compile，手动检查格式"`

Expected: OK

- [ ] **Step 4: Commit**

```bash
git add config/disk/lightrag-server.yaml tests/test_lightrag_server.py
git commit -m "feat: update disk config + tests for search_entities entity_type removal"
```

---

### Task 5: 更新子 Agent 配置（移除 entity_type + 强化去重指令）

**Files:**
- Modify: `config/agents/dream-evolver.md:186,215`
- Modify: `config/agents/entity-extractor.md:67`

- [ ] **Step 1: 更新 dream-evolver.md**

行 186，将：
```
- 去重检查：`lightrag_search_entities(query, keywords=实体名, entity_type, top_k=5)` 检查是否已存在
```
改为：
```
- 去重检查：`lightrag_search_entities(query, keywords=实体名, top_k=5)` 检查同名是否已存在。实体名是唯一标识，同名即重复。需要按类型枚举所有实体时用 `lightrag_list_entities --entity-type 类型名`
```

行 215，将：
```
- `lightrag_search_entities(query, keywords, entity_type, top_k)` — **必须提供 keywords 参数**：...
```
改为：
```
- `lightrag_search_entities(query, keywords, top_k)` — **必须提供 keywords 参数**：你是大模型，自己就能从 query 中提取核心关键词，不需要 LightRAG 再调 LLM 提取。提供 keywords 近即时返回（<1秒），不提供需 5-30 秒且可能失败。top_k=5（硬性要求）
- `lightrag_list_entities(list_type, entity_type, limit)` — 按类型枚举实体（如查看所有人物、所有技能）。entity_type 支持按类型过滤（person/skill/tool/knowledge/photo/concept）
```

- [ ] **Step 2: 更新 entity-extractor.md**

行 67，将：
```
- 查询已有实体：`lightrag_search_entities(query, entity_type, top_k)`
```
改为：
```
- 查询已有实体：`lightrag_search_entities(query, top_k)` — 语义搜索，按关键词找相关实体。需要按类型枚举时用 `lightrag_list_entities --entity-type 类型名`
```

- [ ] **Step 3: Commit**

```bash
git add config/agents/dream-evolver.md config/agents/entity-extractor.md
git commit -m "docs: remove entity_type from search_entities in sub-agent configs"
```

---

### Task 6: 更新 Niu.MD 主 Agent 定义（截断应对提示）

**Files:**
- Modify: `config/agents/niu.md`

- [ ] **Step 1: 在知识图谱查询提示后添加截断应对说明**

`config/agents/niu.md` 知识图谱相关内容区域，插入：

```markdown

**查询结果截断应对**：查询结果可能因数据量大被截断（出现 [截断] 标记）。应对策略：
1. 降低 top_k（10→5→3）
2. 提供更精确的 keywords（用具体实体名而非宽泛词）
3. 使用 fields=['entity_name','entity_type'] 只返回实体名列表，不返回描述——查看大类别（如通讯录）的成员列表时特别有用
4. 查单个实体详情用 `lightrag_get_entity_info`
5. 按类型枚举实体用 `lightrag_list_entities --entity-type person`（直接遍历图节点，不依赖语义搜索）
```

- [ ] **Step 2: Commit**

```bash
git add config/agents/niu.md
git commit -m "docs: add truncation tips to niu.md agent config"
```

---

### Task 7: 验证端到端功能

**Files:** 无代码修改

- [ ] **Step 1: 启动程序，测试 lightrag_search_entities 无 entity_type 参数**

在对话中输入类似：`搜索知识图谱中的Python相关实体`

验证返回结果中不再有 entity_type 过滤逻辑，只返回语义搜索结果。

- [ ] **Step 2: 测试 lightrag_list_entities 按 entity_type 过滤**

在对话中输入类似：`列出所有人物实体`

验证 `lightrag_list_entities --entity-type person` 正常返回按类型过滤的结果。

- [ ] **Step 3: 测试 fields 参数**

在对话中输入类似：`搜索知识图谱中的Python，只返回实体名和类型`

验证 fields=['entity_name','entity_type'] 正常裁剪输出。

- [ ] **Step 4: 验证 dream-evolver 仍能正常去重**

观察一条消息触发 dream-evolver 后，确认去重检查调用 `lightrag_search_entities(query, keywords=实体名, top_k=5)` 不传 entity_type 仍能工作。
