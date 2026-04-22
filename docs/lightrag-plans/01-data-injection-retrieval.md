# LightRAG Data Injection & Retrieval Plan

> Date: 2026-04-22
> Status: Design Draft
> Replaces: vector-store (SQLite + MiniLM-L12) + kg-server (KuzuDB)

## 1. Executive Summary

This document specifies how each data type in the ai-bot system will be injected into LightRAG and retrieved from it. The core challenge is that LightRAG's `ainsert()` uses LLM-based entity extraction, which is perfect for unstructured text but catastrophic for structured data where entity names must be exact (e.g., MCP tool names like `kg-server/explore_node`).

**Strategy**: Use `ainsert_custom_kg()` for all structured data types (Skills, MCP tools, Photos) where we pre-define entities and relationships. Use `ainsert()` only for unstructured content (System Manual, Documents) where LLM entity extraction adds value.

---

## 2. LightRAG API Reference

### 2.1 `ainsert(input, split_by_character, split_by_character_only, ids, file_paths, track_id)`

- Chunks text by token size (default 1200 tokens)
- Calls LLM to extract entities and relationships from each chunk
- Merges extracted entities/relations into the knowledge graph
- Stores chunks in vector DB for retrieval
- **Risk**: LLM may extract irrelevant or wrong entities from structured content

### 2.2 `ainsert_custom_kg(custom_kg, full_doc_id)`

- Accepts pre-defined `custom_kg` dict with keys: `chunks`, `entities`, `relationships`
- **chunks**: `[{"content": str, "source_id": str, "file_path": str, "chunk_order_index": int}]`
- **entities**: `[{"entity_name": str, "entity_type": str, "description": str, "source_id": str, "file_path": str}]`
- **relationships**: `[{"src_id": str, "tgt_id": str, "description": str, "keywords": str, "weight": float, "source_id": str, "file_path": str}]`
- No LLM calls -- deterministic, exact entity names preserved
- **Use for**: All structured data where exact names matter

### 2.3 `aquery(query, param)` / `aquery_data(query, param)`

- `param.mode`: `"local"` (vector), `"global"` (graph), `"hybrid"` (vector+graph), `"mix"` (hybrid+chunks), `"naive"` (basic), `"bypass"` (no retrieval)
- `param.top_k`: Number of top items to retrieve
- `param.only_need_context`: If True, returns context without LLM generation
- `aquery_data()` returns structured data without LLM generation

---

## 3. Data Type A: Skills (V3)

### 3.1 Current System

- **Storage**: `vectors.db` with `id="skill:{name}"`, `metadata.category="skill"`, `metadata.level="l1"`
- **Content**: `"{name}: {description} 触发词: {triggers} 标签: {tags}"`
- **Sync**: `SkillSync` watches `memory/skills/*.md`, extracts triggers/description/tags via regex
- **Retrieval**: `search_multi(categories={"skill": {"limit": 3, "min_score": 0.25}})`
- **Trigger-signal**: When a tool is called, search skills by tool name (min_score=0.3)

### 3.2 Injection Method: `ainsert_custom_kg()`

**Why not `ainsert()`**: If we use `ainsert()`, the LLM would extract entities from the skill content, which could include irrelevant terms from the skill body (e.g., code snippets, examples). The skill NAME and TRIGGER WORDS are the critical entities -- they must be exact.

**Custom KG structure**:

```python
custom_kg = {
    "chunks": [{
        "content": f"{name}: {description}\n触发词: {', '.join(triggers)}\n标签: {', '.join(tags)}",
        "source_id": f"skill:{name}",
        "file_path": str(skill_file),
        "chunk_order_index": 0,
    }],
    "entities": [
        # Skill itself as an entity
        {
            "entity_name": f"Skill:{name}",
            "entity_type": "skill",
            "description": description,
            "source_id": f"skill:{name}",
            "file_path": str(skill_file),
        },
        # Each trigger word as an entity
        *[{
            "entity_name": f"Trigger:{trigger}",
            "entity_type": "trigger_word",
            "description": f"触发词，属于技能 {name}",
            "source_id": f"skill:{name}",
            "file_path": str(skill_file),
        } for trigger in triggers],
        # Each tag as an entity
        *[{
            "entity_name": f"Tag:{tag}",
            "entity_type": "skill_tag",
            "description": f"标签，属于技能 {name}",
            "source_id": f"skill:{name}",
            "file_path": str(skill_file),
        } for tag in tags],
    ],
    "relationships": [
        # Skill HAS_TRIGGER Trigger
        *[{
            "src_id": f"Skill:{name}",
            "tgt_id": f"Trigger:{trigger}",
            "description": f"技能 {name} 的触发词",
            "keywords": f"触发,{trigger}",
            "weight": 1.0,
            "source_id": f"skill:{name}",
            "file_path": str(skill_file),
        } for trigger in triggers],
        # Skill HAS_TAG Tag
        *[{
            "src_id": f"Skill:{name}",
            "tgt_id": f"Tag:{tag}",
            "description": f"技能 {name} 的标签",
            "keywords": f"标签,{tag}",
            "weight": 0.8,
            "source_id": f"skill:{name}",
            "file_path": str(skill_file),
        } for tag in tags],
    ],
}
```

### 3.3 Entity Extraction Strategy: Pre-defined (no LLM)

- Skill name -> `Skill:{name}` entity (type: `skill`)
- Each trigger word -> `Trigger:{word}` entity (type: `trigger_word`)
- Each tag -> `Tag:{tag}` entity (type: `skill_tag`)
- Relationships: `Skill -[HAS_TRIGGER]-> Trigger`, `Skill -[HAS_TAG]-> Tag`

### 3.4 Retrieval Method

- **Primary**: `aquery_data(query, param=QueryParam(mode="local", top_k=3))`
  - Vector search finds the skill chunk by semantic similarity to user query
- **Graph traversal supplement**: `aquery_data(query, param=QueryParam(mode="hybrid", top_k=5))`
  - If user mentions a trigger word exactly, hybrid mode follows the graph to find the parent Skill entity
- **Tool-signal lookup**: When tool `X` is called, query `aquery_data("tool:X", mode="local")` to find skills associated with that tool

### 3.5 Mapping to Current System

| Current | LightRAG Replacement |
|---------|---------------------|
| `search_multi(categories={"skill": ...})` | `aquery_data(query, mode="local", top_k=3)` |
| `search(query=tool_name, metadata_filter={"category":"skill"})` | `aquery_data(tool_name, mode="local", top_k=2)` |
| `SkillSync._sync_skill()` | Build `custom_kg` dict, call `ainsert_custom_kg()` |
| `SkillSync._delete_skill()` | Delete from LightRAG graph + vector stores (needs new API) |

### 3.6 Migration Notes

- SkillSync must be rewritten to call `ainsert_custom_kg()` instead of direct SQLite writes
- The `_extract_triggers()`, `_extract_description()`, `_extract_tags()` methods remain unchanged -- they parse the markdown file, then we build the custom_kg structure
- Delete operation: LightRAG lacks a built-in `adelete()`. We need to either (a) implement entity/chunk deletion via the graph storage API directly, or (b) mark skills as "deleted" and filter them out at query time. **Recommendation**: Implement direct deletion via `knowledge_graph_inst.delete_node()` and `chunks_vdb.delete()`.

---

## 4. Data Type B: MCP Tool Descriptions (V5)

### 4.1 Current System

- **Storage**: `vectors.db` with `id="mcp_tool:{server}/{name}"`, `metadata.category="mcp_tool"`, `metadata.level="l1"`
- **Content**: `"{server}/{name}: {description}"`
- **Retrieval**: `search_multi(categories={"mcp_tool": {"limit": 10, "min_score": 0.25}})`
- **Downstream**: `ToolLifecycleManager.update_from_search(tool_name, score)`
- **Recursion**: `query_pattern` records can redirect queries (e.g., "search photos" -> "photo-server/search_photos")

### 4.2 Injection Method: `ainsert_custom_kg()`

**Why not `ainsert()`**: The tool name `kg-server/explore_node` MUST be preserved exactly. LLM entity extraction would mangle it (e.g., extract "explore_node" as a separate entity without the server prefix, or hallucinate a "kg-server" organization entity).

**Custom KG structure**:

```python
custom_kg = {
    "chunks": [{
        "content": f"{server}/{name}: {description}",
        "source_id": f"mcp_tool:{server}/{name}",
        "file_path": f"mcp://{server}/{name}",
        "chunk_order_index": 0,
    }],
    "entities": [
        # Tool as entity (name is EXACT server/name)
        {
            "entity_name": f"Tool:{server}/{name}",
            "entity_type": "mcp_tool",
            "description": description,
            "source_id": f"mcp_tool:{server}/{name}",
            "file_path": f"mcp://{server}/{name}",
        },
        # Server as entity
        {
            "entity_name": f"Server:{server}",
            "entity_type": "mcp_server",
            "description": f"MCP server providing {server} tools",
            "source_id": f"mcp_tool:{server}/{name}",
            "file_path": f"mcp://{server}",
        },
    ],
    "relationships": [
        {
            "src_id": f"Server:{server}",
            "tgt_id": f"Tool:{server}/{name}",
            "description": f"Server {server} provides tool {name}",
            "keywords": f"provides,{server},{name}",
            "weight": 1.0,
            "source_id": f"mcp_tool:{server}/{name}",
            "file_path": f"mcp://{server}",
        },
    ],
}
```

### 4.3 Entity Extraction Strategy: Pre-defined (no LLM)

- Tool name -> `Tool:{server}/{name}` entity (type: `mcp_tool`)
- Server name -> `Server:{server}` entity (type: `mcp_server`)
- Relationship: `Server -[PROVIDES]-> Tool`
- When two tools from the same server are both present, they share the same Server entity, creating implicit co-occurrence in the graph

### 4.4 Retrieval Method

- **Primary**: `aquery_data(query, param=QueryParam(mode="local", top_k=10))`
  - Vector search matches user query to tool descriptions
  - This replaces `search_multi(categories={"mcp_tool": ...})`
- **Co-activation graph path**: `aquery_data(tool_name, param=QueryParam(mode="global", top_k=5))`
  - When a tool is called, global mode traverses the graph to find sibling tools from the same server
  - This replaces `ToolLifecycleManager._coactivate_same_server_tools()`

### 4.5 Mapping to Current System

| Current | LightRAG Replacement |
|---------|---------------------|
| `search_multi(categories={"mcp_tool": ...})` | `aquery_data(query, mode="local", top_k=10)` |
| `ToolLifecycleManager._coactivate_same_server_tools()` | `aquery_data(tool_name, mode="global", top_k=5)` then filter by same server |
| `update_from_search(tool_name, score)` | Score extracted from `aquery_data` similarity results |
| Query pattern recursion | Keep in vector store (not in graph); recursion logic stays in adapter |

### 4.6 Key Design Decision: Tool Names as Entity Names

The `entity_name` field in LightRAG's graph is a string that gets embedded for vector search. By using `Tool:kg-server/explore_node` as the entity name:

1. **Exact match**: The full tool name is preserved as-is in the graph
2. **Vector search**: The embedding captures semantic meaning from both the tool name and description
3. **Graph traversal**: Following relationships from `Server:kg-server` finds all its tools
4. **No LLM hallucination**: Entity is created by us, not extracted by LLM

---

## 5. Data Type C: System Manual (V6)

### 5.1 Current System

- **Storage**: `vectors.db` with `id="manual:{chapter}"`, `metadata.category="document"`, `metadata.level="l1"`
- **Content**: L1 summaries of manual chapters (pipe-delimited format)
- **Retrieval**: `search_multi(categories={"document": {"limit": 20, "min_score": 0.3}})`

### 5.2 Injection Method: `ainsert()` (LLM extraction enabled)

**Why `ainsert()` is appropriate here**:
- System manual chapters are natural language prose
- LLM entity extraction will correctly identify technologies, concepts, and procedures
- The hierarchical structure (chapter > section > subsection) is preserved by chunk ordering
- We WANT LightRAG to discover cross-chapter entity relationships (e.g., "MCP" mentioned in both the architecture chapter and the tools chapter)

**Chunking strategy**:

```python
# Split by chapter headings to preserve chapter-level granularity
await lightrag.ainsert(
    input=manual_content,
    split_by_character="\n## ",  # Split on ## headings
    split_by_character_only=False,  # Re-chunk long sections by token size
    ids=[f"manual:{chapter_name}"],
    file_paths=["docs/SYSTEM_MANUAL.md"],
)
```

### 5.3 Entity Extraction Strategy: LLM-driven (default)

- LightRAG's default entity extraction prompt will identify:
  - Technologies (e.g., "KuzuDB", "InsightFace", "ToolRegistry")
  - Concepts (e.g., "工作记忆", "衰减-覆盖评分模式")
  - Architectural components (e.g., "Agent核心", "MCP同进程架构")
- These entities will be linked across chapters automatically when the same entity is mentioned in multiple chunks

### 5.4 Retrieval Method

- **Primary**: `aquery_data(query, param=QueryParam(mode="hybrid", top_k=8))`
  - Hybrid combines local vector search with global graph traversal
  - If user asks about "how does MCP work", hybrid finds the MCP entity and traverses to all related chunks
- **Context-only**: `aquery_data(query, param=QueryParam(mode="hybrid", top_k=8, only_need_context=True))`
  - Returns context chunks without LLM generation (for injection into system prompt)

### 5.5 Mapping to Current System

| Current | LightRAG Replacement |
|---------|---------------------|
| `search_multi(categories={"document": ...})` with min_score=0.3 | `aquery_data(query, mode="hybrid", top_k=8, only_need_context=True)` |
| L1 summary pipe format | LightRAG auto-chunks; chapter headings preserved via `split_by_character` |
| L2 pointer for full content | LightRAG stores full chunks in `text_chunks` storage |

### 5.6 Hierarchical Structure Handling

LightRAG's auto-chunking splits by token size, which may break mid-section. To preserve chapter boundaries:

1. Use `split_by_character="\n## "` to split on chapter headings first
2. Each chapter becomes a chunk; long chapters are further split by token size
3. The chunk's `chunk_order_index` preserves reading order
4. Cross-chapter entity references are automatically discovered by the LLM extraction

---

## 6. Data Type D: Photos/Documents (V7)

### 6.1 Current System

- **Photo storage**: `photos.db` with photo metadata + face detection results
- **KG storage**: `knowledge.db` with `Document` nodes (source="photo"), `Entity` nodes (type="person"), `MENTIONS` edges, `RELATED_TO` edges for co-occurrence
- **Vector storage**: L1 descriptions of photos in `vectors.db` (category="document")
- **KGSync**: Backfills KG from photos.db -- creates person entities, photo documents, MENTIONS edges, co_occurrence edges
- **KGScanner**: Scans pending documents for entity extraction

### 6.2 Injection Method: `ainsert_custom_kg()` for photos

**Why not `ainsert()`**: Photo descriptions are short, structured text like "户外场景，3人，小明、小红、小刚在公园野餐". If we use `ainsert()`, the LLM might extract "野餐" as an entity (which is fine) but might also miss person names or create wrong relationships. Since we already have structured data (person names from face detection, scene descriptions), we should use it directly.

**Custom KG structure**:

```python
custom_kg = {
    "chunks": [{
        "content": photo_description,  # e.g., "户外场景，小明、小红在公园野餐"
        "source_id": f"photo:{photo_id}",
        "file_path": file_path,
        "chunk_order_index": 0,
    }],
    "entities": [
        # Photo as entity
        {
            "entity_name": f"Photo:{photo_id}",
            "entity_type": "photo",
            "description": photo_description,
            "source_id": f"photo:{photo_id}",
            "file_path": file_path,
        },
        # Each person as entity (from face detection)
        *[{
            "entity_name": f"Person:{person_name}",
            "entity_type": "person",
            "description": f"人物，出现在照片 {photo_id}",
            "source_id": f"photo:{photo_id}",
            "file_path": file_path,
        } for person_name in detected_persons],
        # Location as entity (if extracted)
        *[{
            "entity_name": f"Location:{location}",
            "entity_type": "location",
            "description": f"地点，出现在照片 {photo_id}",
            "source_id": f"photo:{photo_id}",
            "file_path": file_path,
        } for location in locations],
    ],
    "relationships": [
        # Photo DEPICTS Person
        *[{
            "src_id": f"Photo:{photo_id}",
            "tgt_id": f"Person:{person_name}",
            "description": f"照片 {photo_id} 中出现了 {person_name}",
            "keywords": f"depicts,{person_name}",
            "weight": 0.9,  # High confidence from face detection
            "source_id": f"photo:{photo_id}",
            "file_path": file_path,
        } for person_name in detected_persons],
        # Photo TAKEN_AT Location
        *[{
            "src_id": f"Photo:{photo_id}",
            "tgt_id": f"Location:{location}",
            "description": f"照片 {photo_id} 拍摄于 {location}",
            "keywords": f"taken_at,{location}",
            "weight": 0.7,
            "source_id": f"photo:{photo_id}",
            "file_path": file_path,
        } for location in locations],
        # Person CO_APPEARS_WITH Person (from co_occurrence)
        *[{
            "src_id": f"Person:{person_a}",
            "tgt_id": f"Person:{person_b}",
            "description": f"{person_a} 和 {person_b} 共同出现 {count} 次",
            "keywords": f"co_appears,{person_a},{person_b}",
            "weight": min(0.3 + count * 0.05, 0.9),
            "source_id": f"photo:{photo_id}",
            "file_path": file_path,
        } for person_a, person_b, count in co_occurrences],
    ],
}
```

### 6.3 Entity Extraction Strategy: Hybrid

- **Person entities**: Pre-defined from face detection (exact names from `persons` table)
- **Location entities**: Pre-defined if GPS data is available; otherwise LLM-extracted from description
- **Scene/activity entities**: LLM-extracted from description text (e.g., "野餐", " hiking")
- **Implementation**: Use `ainsert_custom_kg()` for person/location (exact), then optionally run `ainsert()` on the description text only to discover scene/activity entities

### 6.4 Retrieval Method

- **Find photos by person**: `aquery_data("Person:{name} 的照片", mode="global", top_k=20)`
  - Global mode traverses the graph from the Person entity to find all connected Photo entities
- **Find photos by scene**: `aquery_data("野餐的照片", mode="local", top_k=10)`
  - Local mode uses vector search on photo descriptions
- **Find people who appear with X**: `aquery_data("和 {name} 一起出现的人", mode="global", top_k=10)`
  - Global mode follows CO_APPEARS_WITH edges from the Person entity
- **Find photos by location**: `aquery_data("在 {location} 拍的照片", mode="hybrid", top_k=10)`
  - Hybrid mode: vector search finds the location, graph traversal finds connected photos

### 6.5 Mapping to Current System

| Current | LightRAG Replacement |
|---------|---------------------|
| `KGSync._sync_photos_db()` | Build custom_kg, call `ainsert_custom_kg()` |
| `kg-server/explore_node(entity_id="person:X")` | `aquery_data("person:X", mode="global", top_k=20)` |
| `kg-server/get_related_entities(doc_uri=photo_path)` | `aquery_data(photo_path, mode="local", top_k=10)` |
| `kg-server/find_path(from_id, to_id)` | `aquery_data(query, mode="global")` or direct graph query |
| `kg-server/surprising_connections()` | Custom Cypher query on LightRAG's Neo4J/NetworkX backend |
| `search_multi(categories={"document": ...})` | `aquery_data(query, mode="local", top_k=20)` |

### 6.6 For Non-Photo Documents (PDF, Word, etc.)

Use `ainsert()` with LLM entity extraction:
- These are unstructured text where LLM extraction is valuable
- Entities like people, organizations, technologies, concepts will be discovered
- Relationships between entities across documents will be automatically built

---

## 7. Data Type E: Interaction Habits (V4) — 纠错文档

### 7.1 当前系统

- **存储**: `vectors.db` with `id="habit:{type}:{counter}"`, `metadata.category="interaction_habit"`, `metadata.level="l1"`
- **内容**: 自然语言描述（如 "用户偏好使用 kg-server/explore_node 查询实体关系"）
- **置信度**: `metadata.confidence = {"success_count": N, "fail_count": M, "last_used": "2026-04-22"}`
- **检索**: `search_interaction_habits(query, habit_type, limit, min_score)`
- **更新**: `update_habit_confidence(habit_id, result)` — 增减成功/失败计数；3次失败后自动删除

### 7.2 交互习惯的本质：纠错信号

交互习惯的核心用途是**纠错**：

```
场景：用户说"浏览新闻"
  → Agent 理解错误，调用了 vector-store/search_documents
  → 用户纠正："不是搜索文档，是打开浏览器"
  → Agent 重新调用 browser-server/navigate ✓
  → 产生纠错信号：用户说"浏览新闻"时，正确工具是 browser-server/navigate
```

### 7.3 新方案：纠错文档 + ainsert() 自动提取

**核心思路**：把交互习惯变成一个 markdown 文档，纳入 LightRAG 的文档管理体系。

#### 纠错文档格式

文件路径：`~/.niu/interaction-corrections.md`

```markdown
# 交互纠错记录

> 此文件由系统自动维护，记录用户意图与工具调用的对应关系。
> 修改后自动同步到知识图谱。

## 浏览新闻
- ✅ 正确：browser-server/navigate（成功5次）
- ❌ 错误：vector-store/search_documents（失败1次）
- 触发词：浏览、打开网页、访问网站、看网页

## 搜索照片
- ✅ 正确：photo-server/search_photos（成功8次）
- ❌ 错误：vector-store/search_documents（失败2次）
- 触发词：找照片、搜图片、看相册、照片里有没有

## 查看日程
- ✅ 正确：scheduler/list_tasks（成功3次）
- 触发词：日程、待办、提醒、计划
```

#### 为什么用文档而不是 SQLite + 图谱双轨

| 维度 | SQLite + 图谱双轨 | 纠错文档 + ainsert |
|------|-------------------|-------------------|
| 数据源 | 两个（SQLite状态 + 图谱知识） | 一个（md文件） |
| 同步逻辑 | 需要手动同步（阈值触发） | 无需同步，文件改了自动重新索引 |
| 可读性 | 不可读（SQLite blob + 图谱节点） | 人可读可编辑 |
| 维护成本 | 高（两套存储 + 同步机制） | 低（一个文件 + watchdog） |
| LLM提取 | 不走LLM，需手动建关系 | 走LLM，自动提取意图→工具关系 |
| 用户干预 | 不可能 | 用户可以直接编辑md文件 |

#### 注入方式

```python
async def sync_correction_doc(lightrag: LightRAG, doc_path: Path):
    """将纠错文档注入 LightRAG，LLM 自动提取意图→工具关系"""

    content = doc_path.read_text(encoding="utf-8")

    # 用 ainsert() 走 LLM 提取
    # LLM 会自动识别：
    #   - 实体：UserIntent(浏览新闻), Tool(browser-server/navigate), ...
    #   - 关系：浏览新闻 --CORRECT_TOOL--> browser-server/navigate
    #   - 关系：浏览新闻 --WRONG_TOOL--> vector-store/search_documents
    #   - 关系：浏览新闻 --TRIGGERED_BY--> 触发词
    await lightrag.ainsert(
        input=content,
        ids=["interaction-corrections"],
        file_paths=[str(doc_path)],
    )
```

#### 自动更新机制

```python
class CorrectionDocManager:
    """管理纠错文档的自动更新"""

    def __init__(self, doc_path: Path, lightrag: LightRAG):
        self.doc_path = doc_path
        self.lightrag = lightrag
        self._sections: dict[str, CorrectionSection] = {}
        self._load()

    def record_success(self, user_intent: str, tool_name: str):
        """记录一次成功的工具调用"""
        section = self._sections.setdefault(user_intent, CorrectionSection(user_intent))
        if tool_name not in section.correct_tools:
            section.correct_tools[tool_name] = 0
        section.correct_tools[tool_name] += 1
        self._flush()  # 写入md文件

    def record_failure(self, user_intent: str, tool_name: str):
        """记录一次失败的工具调用"""
        section = self._sections.setdefault(user_intent, CorrectionSection(user_intent))
        if tool_name not in section.wrong_tools:
            section.wrong_tools[tool_name] = 0
        section.wrong_tools[tool_name] += 1
        self._flush()

    def _flush(self):
        """将内存中的纠错记录写入md文件"""
        content = self._render_markdown()
        self.doc_path.write_text(content, encoding="utf-8")
        # 触发 LightRAG 重新索引（异步，不阻塞）
        asyncio.create_task(self._reindex())

    async def _reindex(self):
        """重新索引纠错文档"""
        # 先删除旧文档的实体和关系
        await self.lightrag.adelete_by_doc_id("interaction-corrections")
        # 重新注入
        await sync_correction_doc(self.lightrag, self.doc_path)
```

#### 检索时如何利用纠错知识

用户说"浏览新闻"时：

```
  → aquery_data("浏览新闻", mode="hybrid")
  → LLM 提取关键词：low_level=["浏览", "新闻"]
  → entities_vdb 命中 UserIntent:浏览新闻 实体
  → 沿图遍历找到 CORRECT_TOOL → Tool:browser-server/navigate
  → 沿图遍历找到 WRONG_TOOL → Tool:vector-store/search_documents（排除）
  → 返回正确工具
```

**不需要任何特殊逻辑**——LightRAG 的 hybrid 模式天然支持"向量找意图→图遍历找工具"。

#### 与 SkillSync 的统一

纠错文档和 Skills 文件使用**完全相同的管理模式**：

| | Skills | 纠错文档 |
|---|--------|---------|
| 文件 | `memory/skills/*.md` | `~/.niu/interaction-corrections.md` |
| 注入 | `ainsert()` (LLM提取) | `ainsert()` (LLM提取) |
| 更新触发 | watchdog 文件变更 | handler.tool_after_callback |
| 重新索引 | SkillSync 自动 | CorrectionDocManager 自动 |

### 7.4 初始化脚本改造

当前 `scripts/init_vector_db.py` 负责初始化向量库。改造后：

```python
async def init_lightrag(lightrag: LightRAG):
    """初始化 LightRAG：注入所有数据"""

    # 1. 注入 MCP 工具（含 USED_FOR / OFTEN_WITH 关系）— ainsert_custom_kg
    await inject_mcp_tools(lightrag)

    # 2. 注入 Skills（含触发词/标签关系）— ainsert_custom_kg
    await inject_skills(lightrag)

    # 3. 注入系统手册 — ainsert (LLM提取)
    await inject_system_manual(lightrag)

    # 4. 注入纠错文档 — ainsert (LLM提取)
    await sync_correction_doc(lightrag, Path.home() / ".niu" / "interaction-corrections.md")

    # 5. 注入照片数据（人物/场景/地点）— ainsert_custom_kg
    await inject_photos(lightrag)
```

**关键**：只要初始化脚本正确注入了所有数据，后续检索自然走图谱，运行时逻辑基本不用改。

---

## 8. Data Type F: Query Patterns (V8)

### 8.1 Current System

- **Storage**: `vectors.db` with `metadata.category="query_pattern"`, `metadata.is_recursive=True`, `metadata.refined_query="..."`
- **Purpose**: When user query semantically matches a query_pattern, redirect to `refined_query` for better tool retrieval
- **Example**: User says "search photos" -> query_pattern matches -> refined_query = "photo-server/search_photos photo-server/list_photos"
- **Retrieval**: Inside `search_multi()` with `enable_recursion=True`, query_pattern hits trigger a second search with the refined_query

### 8.2 Should This Stay Independent?

**Yes -- query patterns should remain in a separate store, NOT in LightRAG's graph.**

Rationale:
1. **Operational, not knowledge**: Query patterns are search-time redirections, not knowledge entities. They are part of the retrieval infrastructure, not the knowledge base.
2. **Recursion mechanism**: The `is_recursive` + `refined_query` pattern is specific to the `search_multi()` recursion logic. LightRAG has no equivalent.
3. **Exact matching needed**: Query patterns work by semantic similarity between user query and pattern text. This is pure vector search, no graph traversal adds value.
4. **Low cardinality**: Typically <50 query patterns. Not worth the overhead of graph storage.

### 8.3 Implementation

Keep query patterns in the same SQLite `vectors.db` (or dedicated `patterns.db`). The recursion logic in the search adapter should be preserved.

### 8.4 Integration with LightRAG Retrieval

When LightRAG replaces vector-store for the main knowledge retrieval, query patterns still operate as a pre-processing step:

```python
# Pseudo-code for integrated retrieval
async def smart_retrieve(query: str) -> list:
    # Step 1: Check query patterns (SQLite)
    refined = check_query_patterns(query)  # May return refined_query
    effective_query = refined if refined else query

    # Step 2: LightRAG retrieval with effective query
    results = await lightrag.aquery_data(
        effective_query,
        param=QueryParam(mode="hybrid", top_k=10, only_need_context=True)
    )
    return results
```

---

## 9. Unified Retrieval Architecture

### 9.1 How `_inject_dynamic_resources()` Changes

Current flow:
```
user_input -> search_multi() -> {skill, mcp_tool, document, interaction_habit}
```

New flow:
```
user_input -> check_query_patterns() -> effective_query
           -> lightrag.aquery_data(effective_query, mode="hybrid") -> {entities, relationships, chunks}
           -> filter by entity_type -> {skill, mcp_tool, document}
           -> search_interaction_habits(effective_query) -> habits
```

### 9.2 Category Filtering in LightRAG

LightRAG does not natively support category filtering in queries. We have three options:

**Option A: Separate LightRAG instances** (recommended)
- One instance per data type: `lightrag_skills`, `lightrag_tools`, `lightrag_knowledge`
- Clean separation, each with its own graph + vectors
- Query each independently, merge results
- **Drawback**: More storage, no cross-type entity relationships

**Option B: Entity type filtering post-retrieval**
- Single LightRAG instance containing all data types
- Query returns mixed results, filter by `entity_type` field
- Cross-type relationships are possible (e.g., Skill references Tool)
- **Implementation**: After `aquery_data()`, filter the returned entities/chunks by their `entity_type`
- **Drawback**: May need higher `top_k` to get enough results of the desired type

**Option C: Workspace-based separation**
- LightRAG supports workspaces as namespace prefixes
- Use workspace "skills", "tools", "knowledge" to partition data
- Query specific workspace for type-specific results
- **Drawback**: Cross-workspace queries are not natively supported

**Recommendation**: **Option B** for the initial implementation. A single LightRAG instance with post-retrieval filtering by `entity_type`. This preserves cross-type relationships (e.g., a Skill that references an MCP Tool) which are valuable for the hybrid graph traversal.

### 9.3 The `LightRAGAdapter` Class

```python
class LightRAGAdapter:
    """Replaces VectorSearchAdapter + KG server for knowledge retrieval."""

    def __init__(self, lightrag: LightRAG):
        self.lightrag = lightrag
        self.habits_db = VectorSearchAdapter()  # Keep habits separate
        self.patterns_db = VectorSearchAdapter()  # Keep query patterns separate

    async def search_skills(self, query: str, limit: int = 3) -> list[SearchResult]:
        """Search for relevant skills."""
        result = await self.lightrag.aquery_data(
            query, param=QueryParam(mode="local", top_k=limit * 2, only_need_context=True)
        )
        return self._filter_by_entity_type(result, "skill", limit)

    async def search_tools(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Search for relevant MCP tools."""
        result = await self.lightrag.aquery_data(
            query, param=QueryParam(mode="local", top_k=limit * 2, only_need_context=True)
        )
        return self._filter_by_entity_type(result, "mcp_tool", limit)

    async def search_knowledge(self, query: str, limit: int = 20) -> list[SearchResult]:
        """Search for knowledge documents."""
        result = await self.lightrag.aquery_data(
            query, param=QueryParam(mode="hybrid", top_k=limit, only_need_context=True)
        )
        return self._filter_by_entity_type(result, ["photo", "document"], limit)

    async def search_interaction_habits(self, query: str, limit: int = 3) -> list[SearchResult]:
        """Search habits (delegates to separate SQLite store)."""
        return self.habits_db.search_interaction_habits(query, limit=limit)

    async def explore_node(self, entity_id: str, depth: int = 2) -> dict:
        """Explore graph from entity (replaces kg-server/explore_node)."""
        # Use global mode to traverse graph from entity
        result = await self.lightrag.aquery_data(
            entity_id, param=QueryParam(mode="global", top_k=20, only_need_context=True)
        )
        return result

    def _filter_by_entity_type(self, result: dict, entity_types: str | list, limit: int) -> list[SearchResult]:
        """Filter LightRAG results by entity_type."""
        if isinstance(entity_types, str):
            entity_types = [entity_types]
        # Filter entities and their connected chunks by entity_type
        # ... implementation details ...
```

---

## 10. Migration Phases

### Phase 1: Infrastructure Setup
1. Set up LightRAG instance with Neo4J/NetworkX graph backend + vector DB
2. Configure LLM proxy (`page_agent_proxy.py`) for LightRAG's LLM calls
3. Implement `LightRAGAdapter` class
4. Keep current vector-store + kg-server running in parallel

### Phase 2: Structured Data Migration (Skills + MCP Tools)
1. Rewrite `SkillSync` to use `ainsert_custom_kg()` for skills
2. Create `MCPToolSync` to inject tool descriptions via `ainsert_custom_kg()`
3. Verify: query skills/tools through `LightRAGAdapter` returns same results as current system

### Phase 3: Unstructured Data Migration (System Manual + Documents)
1. Inject system manual chapters via `ainsert()` with `split_by_character`
2. Inject existing L2 document content via `ainsert()`
3. Verify: knowledge queries through `LightRAGAdapter` return same or better results

### Phase 4: Photo Migration
1. Rewrite `KGSync._sync_photos_db()` to use `ainsert_custom_kg()` for photos
2. Preserve person entities, MENTIONS edges, co_occurrence edges as custom relationships
3. Verify: person/location queries work through `LightRAGAdapter`

### Phase 5: Retrieval Integration
1. Replace `_inject_dynamic_resources()` to use `LightRAGAdapter`
2. Keep `interaction_habits` and `query_patterns` in separate SQLite stores
3. Replace `kg-server` MCP tools with LightRAG-based equivalents
4. Remove old `vector-store` and `kg-server` dependencies

---

## 11. Summary Table

| Data Type | Injection Method | Entity Strategy | Query Mode | Stays Independent? |
|-----------|-----------------|-----------------|------------|-------------------|
| Skills (V3) | `ainsert_custom_kg()` | Pre-defined: Skill, Trigger, Tag | `local` (primary), `hybrid` (trigger match) | No |
| MCP Tools (V5) | `ainsert_custom_kg()` | Pre-defined: Tool, Server | `local` (search), `global` (co-activation) | No |
| System Manual (V6) | `ainsert()` | LLM-extracted: Technology, Concept, Component | `hybrid` | No |
| Photos (V7) | `ainsert_custom_kg()` | Hybrid: Pre-defined Person/Location, LLM scene | `global` (by person), `local` (by scene), `hybrid` (by location) | No |
| Documents (V7) | `ainsert()` | LLM-extracted | `hybrid` | No |
| Interaction Habits (V4) | `ainsert()` via 纠错文档 | LLM-extracted: UserIntent, CORRECT/WRONG_TOOL | `hybrid` (意图→工具) | No |
| Query Patterns (V8) | N/A | N/A | N/A | **Yes** -- keep in SQLite |

---

## 12. 双路径注入机制

### 核心设计

系统必须明确区分两条注入路径，不能混用：

```
数据源 ──┬── 结构化数据（Skills/MCP Tools/Photos）
         │   → ainsert_custom_kg()  [无LLM调用，精确注入]
         │   → 实体名/关系由代码预定义
         │   → 适合：名称必须精确、关系已知、LLM提取会破坏数据
         │
         └── 非结构化数据（文档/笔记/系统手册）
             → ainsert()  [有LLM调用，自动提取]
             → 实体/关系由LLM从内容中发现
             → 适合：自然语言、实体未知、跨文档关联有价值
```

### 注入调度器

```python
class LightRAGIngester:
    """统一注入入口，自动选择路径"""
    
    async def ingest(self, data_type: str, content: str, metadata: dict):
        if data_type in ("skill", "mcp_tool", "photo"):
            # 路径1：结构化注入，不走LLM
            custom_kg = self._build_custom_kg(data_type, content, metadata)
            await self.lightrag.ainsert_custom_kg(custom_kg)
        elif data_type in ("document", "note", "manual"):
            # 路径2：LLM提取注入
            await self.lightrag.ainsert(content, ids=[metadata["id"]])
        else:
            raise ValueError(f"Unknown data_type: {data_type}")
```

### 两条路径的完整对比

| 维度 | 路径1: ainsert_custom_kg() | 路径2: ainsert() |
|------|---------------------------|-------------------|
| LLM调用 | ❌ 无 | ✅ 每个chunk一次 |
| 实体来源 | 代码预定义 | LLM从文本提取 |
| 实体名精确性 | 100%精确 | 可能偏差 |
| 关系来源 | 代码预定义 | LLM推断 |
| 跨文档关联 | 需手动建立 | 自动发现 |
| 成本 | 仅embedding | embedding + LLM提取 |
| 速度 | 快（无LLM等待） | 慢（等LLM响应） |
| 适用数据 | Skills, MCP Tools, Photos | 文档, 笔记, 系统手册 |

---

## 13. MCP工具的图谱递归替代

### 当前递归机制

```
用户: "浏览新闻"
  → 向量搜索匹配 query_pattern
  → refined_query = "browser_navigate news website"
  → 二次向量搜索
  → 找到 browser_navigate 工具
```

问题：依赖预定义的 query_pattern 改写规则，覆盖面有限。

### 图谱递归替代方案

MCP 工具注入图谱后，通过**实体关系**自然形成递归路径：

```
注入时建立的关系：
  Server:browser-server ──PROVIDES──→ Tool:browser-server/navigate
  Server:browser-server ──PROVIDES──→ Tool:browser-server/click
  Tool:browser-server/navigate ──USED_FOR──→ Concept:网页浏览
  Tool:browser-server/navigate ──OFTEN_WITH──→ Tool:browser-server/click
```

检索时：

```
用户: "浏览新闻"
  → local模式搜索 → 命中 Concept:网页浏览 实体
  → global模式遍历 → 沿 USED_FOR 反向找到 Tool:browser-server/navigate
  → 继续遍历 → 沿 OFTEN_WITH 找到 Tool:browser-server/click（共激活）
```

**关键：不需要预定义 query_pattern，图谱关系本身就是递归路径。**

### 需要在注入时额外建立的关系

除了基本的 `Server ──PROVIDES──→ Tool`，还需要：

1. **`USED_FOR`**：工具 → 使用场景概念
   - 来源：从工具描述中提取关键词（不需要LLM，用规则提取）
   - 例：`browser_navigate` 的描述含"导航到网页" → 建立 `USED_FOR → Concept:网页导航`

2. **`OFTEN_WITH`**：工具 → 经常一起使用的工具
   - 来源：从交互习惯数据挖掘（分析历史 tool_after_callback 记录）
   - 例：`browser_navigate` 后常跟 `browser_click` → `OFTEN_WITH` 关系

3. **`SAME_SERVER`**：同服务器工具之间的隐式关系
   - 已由 `Server ──PROVIDES──→ Tool` 自动形成
   - 查询时：从 Tool → Server → 其他 Tool 即可遍历

### 注入代码示例

```python
def build_mcp_tool_custom_kg(server: str, name: str, description: str,
                              use_concepts: list[str], often_with: list[str]):
    entities = [
        {"entity_name": f"Tool:{server}/{name}", "entity_type": "mcp_tool",
         "description": description, ...},
        {"entity_name": f"Server:{server}", "entity_type": "mcp_server", ...},
        # 使用场景概念
        *[{"entity_name": f"Concept:{c}", "entity_type": "tool_concept",
           "description": f"工具使用场景: {c}", ...} for c in use_concepts],
    ]
    relationships = [
        # Server → Tool
        {"src_id": f"Server:{server}", "tgt_id": f"Tool:{server}/{name}",
         "description": f"提供工具 {name}", "keywords": "provides", "weight": 1.0, ...},
        # Tool → Concept (USED_FOR)
        *[{"src_id": f"Tool:{server}/{name}", "tgt_id": f"Concept:{c}",
           "description": f"用于 {c}", "keywords": f"used_for,{c}", "weight": 0.8, ...}
          for c in use_concepts],
        # Tool → Tool (OFTEN_WITH)
        *[{"src_id": f"Tool:{server}/{name}", "tgt_id": f"Tool:{other}",
           "description": f"常与 {other} 一起使用", "keywords": "often_with",
           "weight": co_occurrence_weight, ...}
          for other in often_with],
    ]
    return {"chunks": [...], "entities": entities, "relationships": relationships}
```

### 递归检索对比

| 维度 | 当前 query_pattern | LightRAG 图谱遍历 |
|------|-------------------|-------------------|
| 覆盖面 | 仅预定义的改写规则 | 所有有关系的实体都可遍历到 |
| 维护成本 | 需手动添加 pattern | 关系在注入时自动建立 |
| 深度 | 1层（query→refined_query） | N层（沿图任意深度） |
| 精确性 | 高（人工定义） | 中（依赖关系质量） |
| 新工具适配 | 需手动添加 pattern | 自动（注入时建关系即可） |

### query_pattern 的去留

**建议保留但降级为补充**：
- 图谱遍历作为主要递归机制
- query_pattern 作为精确改写的补充（某些场景需要强制改写，如"搜索照片"必须映射到 `photo-server/search_photos`）
- 检索流程：先查 query_pattern（精确改写）→ 再用 LightRAG 图谱遍历（关联扩展）

---

## 14. Open Questions

1. **LightRAG deletion API**: LightRAG currently lacks `adelete()`. For skills and tools that are removed, we need to implement deletion directly against the graph storage backend. Need to verify if `BaseGraphStorage` exposes a `delete_node()` method.

2. **Category filtering performance**: Post-retrieval filtering by `entity_type` may require 2-3x `top_k` to get enough results of the desired type. Need to benchmark whether this is acceptable or if separate workspaces are needed.

3. **LLM proxy latency**: LightRAG's `ainsert()` calls the LLM for entity extraction. The `page_agent_proxy.py` must handle these calls efficiently. Batch processing and caching will be important.

4. **Incremental updates**: When a skill file is modified, we need to delete the old entities and re-insert. Without a built-in delete API, this requires direct graph manipulation.

5. **Graph backend choice**: NetworkX (in-memory, simple) vs Neo4J (persistent, scalable). For a personal assistant, NetworkX is likely sufficient, but migration to Neo4J should be possible if the graph grows large.

6. **Query pattern integration**: The current `search_multi()` recursion mechanism (query_pattern -> refined_query -> second search) needs to be adapted to work with `LightRAGAdapter`. The refined query should be passed to `aquery_data()` instead of `search_multi()`.
