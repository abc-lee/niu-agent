# niu 根节点连接清理 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清理所有违反"niu 只与脑区连接"规则的代码和提示词——运行时代码不再自动创建 niu→非脑区实体的关系边，子Agent提示词用脑区连接做示例替代 niu 连接，不相关的子Agent不提及 niu。

**Architecture:** 三层整改：(1) 运行时代码删除自动创建 niu→非脑区实体的逻辑；(2) 提示词用脑区连接替代 niu 连接做示例；(3) 不相关的子Agent删除对 niu 的提及。最小改动原则，不改变功能语义。

**Tech Stack:** Python, Markdown

**大小写规则**：知识图谱所有内容全部转换为小写存储，`niu` 是小写的根节点名。代码中当前用大写 `"Niu"` 是历史遗留，但提示词和文档中必须用小写 `niu`，不要误导大模型使用大写。代码修改中 `old_string` 必须匹配实际代码中的 `"Niu"`（否则无法匹配），但注释和描述统一用小写 `niu`。

---

## 设计决策

### 1. 正确规则（来源：brain_region_prompt.py）

- `niu` 是知识图谱根节点，**只与各脑区连接**
- 禁止对 niu 根节点做任何操作
- 禁止任何节点与 niu 根节点连接
- 启动时 Niu 与默认脑区建立 `brain_region_anchor` 连接是**正常行为**

### 2. 用户指示

- 提示词整改应找到正确的脑区连接做示例和举例，不用 niu
- 不相关的子Agent不需要提到 niu——子Agent无上下文，不提它也不知道
- 知识图谱所有内容都是小写，`niu` 是小写的根节点名，不要在提示词或注释中用大写 `Niu` 误导大模型

### 3. 记忆可达性替代方案

删除 niu→实体 锚边后，记忆实体的可达性由脑区 `_region:contains` 边保证。`store_memory` 存储的记忆实体归入对应脑区（由脑区自动发现算法处理），不需要 niu 锚边。`lightrag_insert_entity` 插入的实体由调用方（file-processor、dream-evolver 等）负责归入脑区。

---

## 文件结构

| 文件 | 职责 | 操作 |
|------|------|------|
| `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` | 删除 `lightrag_insert_entity` 中的 niu 锚边逻辑 + 修改工具描述 | 修改 |
| `niu_api/internal/brain_graph.py` | `store_memory` 不再创建 niu→target 关系 + 记忆召回移除 niu 过滤 | 修改 |
| `niu_api/internal/lightrag_adapter.py` | `upsert_interaction_habit` 删除 "Niu uses X" 语句 | 修改 |
| `agent/injector/dream_writer.py` | 删除语义/情景记忆入库中的 niu 引用 | 修改 |
| `tests/test_dream_writer.py` | 移除 NIU_ENTITY import + niu 相关测试断言 | 修改 |
| `tests/test_lightrag_server.py` | 移除 niu 锚边 relationships 断言 | 修改 |
| `tests/test_brain_graph.py` | 移除 niu 关系断言 + mock 数据改用脑区 src | 修改 |
| `config/agents/dream-evolver.md` | 用脑区连接替代 niu 连接做示例 | 修改 |
| `config/agents/entity-extractor.md` | 删除对 niu 的提及 | 修改 |

---

### Task 1: 删除 `lightrag_insert_entity` 中的 niu 锚边逻辑

**Files:**
- Modify: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py`

**问题**：`lightrag_insert_entity` 函数对 person/skill/concept/tool/preference 类型自动创建 `niu→实体` 锚边，违反"禁止任何节点与 niu 根节点连接"规则。

- [ ] **Step 1: 删除 niu 锚边创建代码（两段独立替换）**

先 Read 文件，找到 `lightrag_insert_entity` 函数中的 niu 锚边相关代码。

**替换1**：删除 `niu_relation_map` 和 `niu_relation` 变量（行 1175-1182）：

old_string:
```python
        niu_relation_map = {
            "person": "remembers",
            "skill": "skilled_in",
            "concept": "knows_about",
            "tool": "uses",
            "preference": "prefers",
        }
        niu_relation = niu_relation_map.get(entity_type.lower() if entity_type else None)

        # Build entity dict for inject_custom_kg
```

new_string:
```python
        # Build entity dict for inject_custom_kg
```

**替换2**：删除 Niu anchor 关系构建块（行 1193-1205），保留 entity dict 不动：

old_string:
```python
        # Build Niu -> entity anchor relationship (only for types that
        # semantically connect to Niu — Person/Skill/Concept/Tool/Preference)
        relationships = []
        if niu_relation:
            anchor_rel = {
                "src_id": "Niu",
                "tgt_id": name,
                "keywords": niu_relation,
                "description": f"Niu {niu_relation} {name}",
                "source_id": file_path,
                "file_path": file_path,
            }
            relationships.append(anchor_rel)
```

new_string:
```python
        # niu anchor edges removed: niu only connects to brain regions,
        # not to individual entities. Entity reachability is provided by
        # brain region _region:contains edges.
        relationships = []
```

- [ ] **Step 2: 修改工具描述**

找到 `lightrag_insert_entity` 工具的 description 字段（行 395）：

当前：
```python
                        "description": "Insert an entity into the knowledge graph using structured injection (ainsert_custom_kg). Entity name and type are preserved exactly — no LLM auto-extraction. Also creates a Niu anchor edge for reachability. Entity names must use natural language (e.g., 'Python', '任飞'), NOT colon-prefix format (e.g., NOT 'skill:Python', NOT 'person:uuid').",
```

替换为（删除 "Also creates a Niu anchor edge for reachability. "）：
```python
                        "description": "Insert an entity into the knowledge graph using structured injection (ainsert_custom_kg). Entity name and type are preserved exactly — no LLM auto-extraction. Entity names must use natural language (e.g., 'Python', '任飞'), NOT colon-prefix format (e.g., NOT 'skill:Python', NOT 'person:uuid').",
```

- [ ] **Step 3: 修改 `lightrag_insert_relation` 工具描述中的 niu 示例**

找到行 416：

当前：
```python
        "description": "Insert a relation between two entities using structured injection (ainsert_custom_kg). Relation src/tgt/keywords are preserved exactly — no LLM auto-extraction. Entity names must use natural language (e.g., 'Niu', 'Python'), NOT colon-prefix format.",
```

替换为（删除 Niu 示例）：
```python
        "description": "Insert a relation between two entities using structured injection (ainsert_custom_kg). Relation src/tgt/keywords are preserved exactly — no LLM auto-extraction. Entity names must use natural language (e.g., 'Python', '任飞'), NOT colon-prefix format.",
```

- [ ] **Step 4: 验证语法**

Run: `cd <repo_root> && python -c "from niu_lightrag_server import lightrag_insert_entity; print('OK')"`

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py
git commit -m "fix: remove niu anchor edge from lightrag_insert_entity — niu only connects to brain regions"
```

---

### Task 2: 修改 `store_memory` 不再创建 niu→target 关系

**Files:**
- Modify: `niu_api/internal/brain_graph.py`

**问题**：`store_memory` 每次存储记忆都创建 `niu→target` 关系边，违反规则。记忆实体的可达性应由脑区保证。

- [ ] **Step 1: 修改 `store_memory` 中的关系构建和 metadata 嵌入**

先 Read 文件，找到 `store_memory` 方法中构建 relationships 的代码（约行 178-216）：

当前代码将 metadata 嵌入到 `description` 变量中（行 178-189），然后传到 `relationships` dict 中。删除 relationships 后，metadata 信息会丢失。需要将 metadata 嵌入移到 `entity_description` 中。

**替换1**：修改 metadata 嵌入位置，从 relation description 移到 entity description

当前（行 178-193）：
```python
        # Build relation description, embedding metadata if present
        description = content[:200]
        if metadata:
            try:
                meta_str = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
                # Skip metadata if too long — truncated JSON is irrecoverable
                if len(meta_str) > 200:
                    logger.debug(f"[BRAIN] metadata too long ({len(meta_str)} chars), skipping")
                else:
                    description = f"{description} [meta:{meta_str}]"
            except (TypeError, ValueError):
                pass  # Non-serializable metadata, skip

        # Build entity description with created_at timestamp only
        created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        entity_description = f"created_at={created_at}<SEP>{content[:200]}"
```

替换为（metadata 嵌入到 entity_description 中）：
```python
        # Build entity description with created_at timestamp + metadata
        created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        entity_description = f"created_at={created_at}<SEP>{content[:200]}"
        if metadata:
            try:
                meta_str = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
                # Skip metadata if too long — truncated JSON is irrecoverable
                if len(meta_str) > 200:
                    logger.debug(f"[BRAIN] metadata too long ({len(meta_str)} chars), skipping")
                else:
                    entity_description = f"{entity_description} [meta:{meta_str}]"
            except (TypeError, ValueError):
                pass  # Non-serializable metadata, skip
```

**替换2**：修改 inject_custom_kg 调用，删除 relationships

当前（行 206-216）：
```python
            relationships=[
                {
                    "src_id": "Niu",
                    "tgt_id": target_name,
                    "keywords": relation_type,
                    "description": description,
                    "weight": weight,
                    "source_id": "brain",
                    "file_path": "brain://memory",
                }
            ],
```

替换为（不再创建 niu→target 关系，只注入实体和 chunk）：
```python
            relationships=[],
```

- [ ] **Step 2: 更新模块文档字符串**

找到模块文档字符串（约行 1-10）：

当前（基于实际文件内容）：
```python
"""
Brain Graph — Memory system on LightRAG knowledge graph.

Memories are stored as weighted relations from Niu to typed entities,
and retrieved via LightRAG query_data(mode="mix").

Core concepts:
- Niu — the "self" entity, all memory relations start from it
```

替换为：
```python
"""
Brain Graph — Memory system on LightRAG knowledge graph.

Memories are stored as typed entities in the knowledge graph,
and retrieved via LightRAG query_data(mode="mix"). Entity reachability
is provided by brain region _region:contains edges, not niu anchors.

Core concepts:
- niu — the root node, only connects to brain regions
```

- [ ] **Step 3: 修改 `_extract_brain_memories_from_structured` — 移除 niu 过滤器**

找到 `_extract_brain_memories_from_structured` 方法中的 Niu 过滤逻辑（约行 331-334）：

当前：
```python
            # Include relations involving Niu or any entity
            is_niu_related = src == "Niu" or tgt == "Niu"
            if not is_niu_related:
                continue
```

替换为（移除 niu 过滤，改为只保留记忆相关的关系边）：
```python
            # Include all memory-relevant relations — no longer filtering by
            # niu since store_memory no longer creates niu→entity edges.
            # Exclude brain region structure edges (not memory content).
            if relation.startswith("_region:"):
                continue
```

同时修改下方 target 提取逻辑（约行 338）：

当前：
```python
                "target": tgt if tgt != "Niu" else src,
```

替换为（保持"返回非 Niu 端点"的语义：如果 tgt 是 Niu 就取 src，否则取 tgt）：
```python
                "target": src if tgt == "Niu" else tgt,
```

- [ ] **Step 4: 修改 `_extract_brain_memories_from_text` — 移除 niu 依赖**

找到 `_extract_brain_memories_from_text` 方法（约行 346-370），将 Niu 相关的匹配逻辑改为通用文本匹配：

当前：
```python
    def _extract_brain_memories_from_text(
        self, text: str, min_weight: float
    ) -> List[Dict[str, Any]]:
        """Extract Niu memory references from query result text."""
        memories = []

        # Match "Niu" as a standalone word (word boundary) to avoid
        # false positives like "Niurou" or other substrings containing "Niu".
        if re.search(r"\bNiu\b", text):
            weight = 0.7  # Default for recalled memories
            if weight >= min_weight:
                memories.append({
                    "target": "Niu",
                    "relation_type": "remembers",
                    "description": text.strip()[:200],
                    "weight": weight,
                })

        if not memories and text.strip():
            memories.append({
                "target": "Niu",
                "relation_type": "remembers",
                "description": text.strip()[:200],
                "weight": 0.5,
```

替换为：
```python
    def _extract_brain_memories_from_text(
        self, text: str, min_weight: float
    ) -> List[Dict[str, Any]]:
        """Extract memory references from query result text."""
        memories = []

        if text.strip():
            weight = 0.7  # Default for recalled memories
            if weight >= min_weight:
                # Extract a meaningful target from the text rather than
                # hardcoding "Niu" — use first 20 chars as identifier
                target = text.strip()[:20].split("。")[0].split("，")[0]
                memories.append({
                    "target": target,
                    "relation_type": "remembers",
                    "description": text.strip()[:200],
                    "weight": weight,
                })

        if not memories and text.strip():
            target = text.strip()[:20].split("。")[0].split("，")[0]
            memories.append({
                "target": target,
                "relation_type": "remembers",
                "description": text.strip()[:200],
                "weight": 0.5,
```

**说明**：`target` 不再设为空字符串或 "niu"，而是从文本中提取第一个短语（20 字符内的第一个句号/逗号前的内容）作为标识符，确保下游 `format_memories_for_prompt` 的 `display_name` 有意义。

- [ ] **Step 5: 更新 `store_memory` 方法 docstring**

找到 `store_memory` 方法的 docstring（约行 149-153）：

当前：
```python
        """Store a memory in the brain graph.

        Creates a target entity and a weighted relation from Niu to it
        in a single atomic inject_custom_kg call, with the entity description
        passed as a chunk so LLM can extract additional relationships.
```

替换为：
```python
        """Store a memory in the brain graph.

        Creates a target entity in the knowledge graph
        in a single atomic inject_custom_kg call, with the entity description
        passed as a chunk so LLM can extract additional relationships.
        Entity reachability is provided by brain region _region:contains edges.
```

- [ ] **Step 6: 更新 BrainGraph 类文档字符串**

找到 `BrainGraph` 类的文档字符串（约行 113）：

当前：
```python
    """Stores memories as relations from Niu to entities.
```

替换为：
```python
    """Stores memories as entities in the knowledge graph.
```

- [ ] **Step 7: 验证语法**

Run: `cd <repo_root> && python -c "from niu_api.internal.brain_graph import BrainGraph; print('OK')"`

- [ ] **Step 8: Commit**

```bash
git add niu_api/internal/brain_graph.py
git commit -m "fix: store_memory no longer creates niu→entity relations — reachability via brain regions"
```

---

### Task 3: 删除 `upsert_interaction_habit` 中的 "Niu uses" 语句

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py`

**问题**：`upsert_interaction_habit` 在入库文本中嵌入 "Niu uses {entity_name}"，LightRAG 的 LLM 提取会自动产生 niu→交互习惯实体的关系边。

- [ ] **Step 1: 删除 "Niu uses" 语句**

先 Read 文件，找到 `upsert_interaction_habit` 中的文本构建代码（约行 1374）：

当前：
```python
            text = f"交互习惯: {entity_name}（类型: interactionhabit），{description}。Niu uses {entity_name}。"
```

替换为：
```python
            text = f"交互习惯: {entity_name}（类型: interactionhabit），{description}。"
```

- [ ] **Step 2: 验证语法**

Run: `cd <repo_root> && python -c "from niu_api.internal.lightrag_adapter import LightRAGAdapter; print('OK')"`

- [ ] **Step 3: Commit**

```bash
git add niu_api/internal/lightrag_adapter.py
git commit -m "fix: remove 'Niu uses' from interaction habit text — prevents niu→entity extraction"
```

---

### Task 4: 删除 `dream_writer.py` 中的 niu 引用

**Files:**
- Modify: `agent/injector/dream_writer.py`

**问题**：`DreamWriter` 通过 `lightrag_insert`（LLM 自动提取）入库时，在文本中嵌入 "Niu {relation} {name}" 和 "Niu experienced {event}" 语句，LightRAG 的 LLM 提取会自动产生 niu→实体的关系边，与 Task 3 同类问题。

- [ ] **Step 1: 删除 `_NIU_RELATION_MAP` 和 `_determine_niu_relation`**

先 Read 文件确认代码。删除 `NIU_ENTITY` 常量、`_NIU_RELATION_MAP` 字典和 `_determine_niu_relation` 方法。

**替换1**：删除 `NIU_ENTITY` 常量（行 33-34）：

old_string:
```python
# Self entity name (anchor point for all semantic entities)
NIU_ENTITY = "Niu"

# Relation keywords for semantic pipeline
```

new_string:
```python
# Relation keywords for semantic pipeline
```

**替换2**：删除 `_NIU_RELATION_MAP` 字典（行 45-52）：

old_string:
```python
# Mapping from entity_type to Niu relation keyword
_NIU_RELATION_MAP = {
    "person": "remembers",
    "skill": "skilled_in",
    "concept": "knows_about",
    "tool": "uses",
    "preference": "prefers",
}
```

new_string:
```python
# _NIU_RELATION_MAP removed — niu only connects to brain regions
```

**替换3**：删除 `_determine_niu_relation` 方法（行 252-268）：
```python
    def _determine_niu_relation(self, entity_type: str) -> str | None:
        """Determine Niu → entity relation type based on entity_type.

        Args:
            entity_type: The entity type string.

        Returns:
            Relation keyword for the Niu → entity relation, or None if
            this entity type should not have a Niu anchor.
            Person → "remembers"
            Skill → "skilled_in"
            Concept → "knows_about"
            Tool → "uses"
            Preference → "prefers"
            Other → None (no Niu anchor)
        """
        return _NIU_RELATION_MAP.get(entity_type.lower() if entity_type else None)
```

new_string:
```python
    # _determine_niu_relation removed — niu only connects to brain regions
```

- [ ] **Step 2: 修改 `write_semantic_entity` — 删除 niu 引用**

old_string:
```python
        niu_relation = self._determine_niu_relation(entity_type)
        if niu_relation:
            text = f"语义记忆: {name}（类型: {entity_type}），{description}。Niu {niu_relation} {name}。"
        else:
            text = f"语义记忆: {name}（类型: {entity_type}），{description}。"
```

new_string:
```python
        text = f"语义记忆: {name}（类型: {entity_type}），{description}。"
```

- [ ] **Step 2b: 修改 `write_semantic_entity` 中的 logger — 移除 `niu_relation` 引用**

删除 `niu_relation` 变量后，logger 中引用 `niu_relation` 会导致 NameError。

当前（行 120-125）：
```python
            logger.info(
                "语义实体入库完成: %s (type=%s, niu_relation=%s)",
                name,
                entity_type,
                niu_relation or "(no anchor)",
            )
```

替换为：
```python
            logger.info(
                "语义实体入库完成: %s (type=%s)",
                name,
                entity_type,
            )
```

- [ ] **Step 3: 修改 `write_episodic_event` — 删除 "Niu experienced" 语句**

old_string:
```python
        text_parts = [f"情景记忆: {event_name}（类型: {experience_type}），{description}。"]
        text_parts.append(f"Niu experienced {event_name}。")
```

new_string:
```python
        text_parts = [f"情景记忆: {event_name}（类型: {experience_type}），{description}。"]
```

- [ ] **Step 4: 验证语法**

Run: `cd <repo_root> && python -c "from agent.injector.dream_writer import DreamWriter; print('OK')"`

- [ ] **Step 5: Commit**

```bash
git add agent/injector/dream_writer.py
git commit -m "fix: remove niu references from DreamWriter — no niu anchors in semantic/episodic text"
```

---

### Task 4a: 更新 `test_dream_writer.py` — 移除 niu 相关测试

**Files:**
- Modify: `tests/test_dream_writer.py`

**问题**：Task 4 删除了 `NIU_ENTITY` 常量、`_NIU_RELATION_MAP` 字典、`_determine_niu_relation` 方法，以及 `write_semantic_entity` 和 `write_episodic_event` 中的 Niu 引用。测试文件需要同步更新。

- [ ] **Step 1: 移除 `NIU_ENTITY` import**

当前（行 21）：
```python
    NIU_ENTITY,
```

替换为（删除该行）：
```python
```

- [ ] **Step 2: 移除 `test_determine_niu_relation` 测试函数**

找到整个 `test_determine_niu_relation` 函数（约行 230-241），删除它。

当前（行 233-241）：
```python
def test_determine_niu_relation(writer: DreamWriter) -> None:
    """Verify relation type mapping."""
    assert writer._determine_niu_relation("Person") == "remembers"
    assert writer._determine_niu_relation("Skill") == "skilled_in"
    assert writer._determine_niu_relation("Concept") == "knows_about"
    assert writer._determine_niu_relation("Tool") == "uses"
    # Default case
    assert writer._determine_niu_relation("UnknownType") == "remembers"
    assert writer._determine_niu_relation("Place") == "remembers"
```

替换为（删除整个函数）：
```python
```

- [ ] **Step 3: 修改 `test_write_semantic_entity` 断言 — 移除 niu 相关检查**

行 63 注释：
```python
    # Text should contain entity name, type, description, and brain:Niu relation
```
替换为：
```python
    # Text should contain entity name, type, description
```

行 67-68：
```python
    assert "brain:Niu" in text
    assert "skilled_in" in text
```
替换为（语义实体文本不再包含 niu anchor 和 skilled_in）：
```python
    assert "brain:Niu" not in text
    assert "skilled_in" not in text
```

- [ ] **Step 4: 修改 `test_write_semantic_entity_default_relation` — 移除 "remembers" 断言**

当前（行 71-83）：
```python
def test_write_semantic_entity_default_relation(
    writer: DreamWriter, mock_ingester: MagicMock
) -> None:
    """Verify default relation is 'remembers' for unknown entity types."""
    writer.write_semantic_entity(
        name="Alice",
        entity_type="UnknownType",
        description="A person",
    )

    call_kwargs = mock_ingester.lightrag_insert.call_args
    text = call_kwargs.kwargs["content"]
    assert "remembers" in text
```

替换为（不再有 niu relation，删除此测试）：
```python
def test_write_semantic_entity_default_relation(
    writer: DreamWriter, mock_ingester: MagicMock
) -> None:
    """Verify semantic entity text without niu anchor for unknown types."""
    writer.write_semantic_entity(
        name="Alice",
        entity_type="UnknownType",
        description="A person",
    )

    call_kwargs = mock_ingester.lightrag_insert.call_args
    text = call_kwargs.kwargs["content"]
    assert "语义记忆" in text
    assert "Alice" in text
    assert "UnknownType" in text
    assert "brain:Niu" not in text
```

- [ ] **Step 5: 修改 `test_write_episodic_event` 断言 — 移除 "brain:Niu experienced" 检查**

当前（行 148）：
```python
    assert "brain:Niu experienced brain:event:tool_x_failed" in text
```

替换为（情景记忆文本不再包含 Niu experienced）：
```python
    assert "brain:Niu experienced" not in text
```

行 130 注释：
```python
    """Verify structured text with event + brain:Niu anchor passed to lightrag_insert."""
```
替换为：
```python
    """Verify structured text with event passed to lightrag_insert."""
```

行 144 注释：
```python
    # Text should contain event name, type, description, and brain:Niu experienced
```
替换为：
```python
    # Text should contain event name, type, description
```

- [ ] **Step 6: 运行测试验证**

Run: `cd <repo_root> && python -m pytest tests/test_dream_writer.py -v 2>&1 | tail -20`

- [ ] **Step 7: Commit**

```bash
git add tests/test_dream_writer.py
git commit -m "test: update dream_writer tests — remove niu anchor assertions"
```

---

### Task 2a: 更新 `test_lightrag_server.py` — 移除 niu 锚边断言

**Files:**
- Modify: `tests/test_lightrag_server.py`

**问题**：Task 1 删除了 `lightrag_insert_entity` 中的 Niu 锚边，测试中 `relationships=[{"src_id": "brain:Niu", ...}]` 断言将失败。

- [ ] **Step 1: 修改 `test_lightrag_insert_entity` 中的 relationships 断言**

当前（行 318-322）：
```python
            relationships=[{
                "src_id": "brain:Niu", "tgt_id": "Python",
                "keywords": "remembers", "description": "Niu remembers Python",
                "source_id": "custom_kg", "file_path": "custom_kg",
            }],
```

替换为（不再创建 Niu 锚边）：
```python
            relationships=[],
```

- [ ] **Step 2: Commit**

```bash
git add tests/test_lightrag_server.py
git commit -m "test: update lightrag_server tests — remove niu anchor edge assertions"
```

---

### Task 2b: 更新 `test_brain_graph.py` — 移除 niu 关系断言 + 修复 metadata 测试

**Files:**
- Modify: `tests/test_brain_graph.py`

**问题**：Task 2 修改了 `store_memory` 不再创建 niu→target 关系（`relationships=[]`），metadata 嵌入从 relation description 移到 entity description。测试需要大幅更新。

**核心变更**：
- `inject_custom_kg` 仍然只调用一次（原子调用），但 `relationships=[]`
- metadata 现在嵌入在 `entities[0]["description"]` 中，而非 `relationships[0]["description"]`
- `call_count == 2` 和 `call_args_list[1]` 的测试模式需要改为 `call_count == 1` 和 `call_args_list[0]`

- [ ] **Step 1: 修改 `test_store_memory_no_type_default`**

当前（行 107-126）：
```python
    def test_store_memory_no_type_default(self):
        """Memory without type should create brain:Niu --remembers--> entity with default weight."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="用户提到了asyncio问题",
        )

        assert result["status"] == "ok"
        assert result["relation_type"] == "remembers"
        assert result["weight"] == 0.7
        # Should have called inject_custom_kg twice (entity + relation)
        assert bg._ingester.inject_custom_kg.call_count == 2
        # Second call has the relation from brain:Niu
        call_kwargs = bg._ingester.inject_custom_kg.call_args_list[1]
        rels = call_kwargs[1]["relationships"]
        assert len(rels) == 1
        assert rels[0]["src_id"] == "brain:Niu"
        assert rels[0]["relation"] == "remembers"
        assert rels[0]["weight"] == 0.7
```

替换为：
```python
    def test_store_memory_no_type_default(self):
        """Memory without type should store entity with default weight."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="用户提到了asyncio问题",
        )

        assert result["status"] == "ok"
        assert result["relation_type"] == "remembers"
        assert result["weight"] == 0.7
        # Single atomic call — no niu→entity relationship
        assert bg._ingester.inject_custom_kg.call_count == 1
        call_kwargs = bg._ingester.inject_custom_kg.call_args
        assert call_kwargs[1]["relationships"] == []
```

- [ ] **Step 2: 修改其他 store_memory 测试 — 统一改为 call_count == 1**

**test_store_l1_memory_prefers**（行 128-142）：

当前：
```python
    def test_store_l1_memory_prefers(self):
        """Memory with type=preferences should create 'prefers' relation."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="偏好暗色主题编码",
            memory_type="preferences",
        )

        assert result["status"] == "ok"
        assert result["relation_type"] == "prefers"
        assert bg._ingester.inject_custom_kg.call_count == 2
        call_kwargs = bg._ingester.inject_custom_kg.call_args_list[1]
        rels = call_kwargs[1]["relationships"]
        assert rels[0]["relation"] == "prefers"
```

替换为：
```python
    def test_store_l1_memory_prefers(self):
        """Memory with type=preferences should store entity with 'prefers' relation_type."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="偏好暗色主题编码",
            memory_type="preferences",
        )

        assert result["status"] == "ok"
        assert result["relation_type"] == "prefers"
        assert bg._ingester.inject_custom_kg.call_count == 1
```

**test_store_memory_skills**（行 144-158）：

当前：
```python
    def test_store_memory_skills(self):
        """Memory with type=skills should create 'skilled_in' relation."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="擅长Web开发",
            memory_type="skills",
        )

        assert result["status"] == "ok"
        assert result["relation_type"] == "skilled_in"
        assert bg._ingester.inject_custom_kg.call_count == 2
        call_kwargs = bg._ingester.inject_custom_kg.call_args_list[1]
        rels = call_kwargs[1]["relationships"]
        assert rels[0]["relation"] == "skilled_in"
```

替换为：
```python
    def test_store_memory_skills(self):
        """Memory with type=skills should store entity with 'skilled_in' relation_type."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="擅长Web开发",
            memory_type="skills",
        )

        assert result["status"] == "ok"
        assert result["relation_type"] == "skilled_in"
        assert bg._ingester.inject_custom_kg.call_count == 1
```

**test_store_memory_experiences**（行 160-174）：

当前：
```python
    def test_store_memory_experiences(self):
        """Memory with type=experiences should create 'remembers' relation."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="从2019年开始用Python做AI/ML",
            memory_type="experiences",
        )

        assert result["status"] == "ok"
        assert result["relation_type"] == "remembers"
        assert bg._ingester.inject_custom_kg.call_count == 2
        call_kwargs = bg._ingester.inject_custom_kg.call_args_list[1]
        rels = call_kwargs[1]["relationships"]
        assert rels[0]["relation"] == "remembers"
```

替换为：
```python
    def test_store_memory_experiences(self):
        """Memory with type=experiences should store entity with 'remembers' relation_type."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="从2019年开始用Python做AI/ML",
            memory_type="experiences",
        )

        assert result["status"] == "ok"
        assert result["relation_type"] == "remembers"
        assert bg._ingester.inject_custom_kg.call_count == 1
```

**test_store_memory_default_weight**（行 176-189）：

当前：
```python
    def test_store_memory_default_weight(self):
        """Memory without type should use default weight 0.7."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="Python的GIL机制导致多线程无法真正并行",
        )

        assert result["status"] == "ok"
        assert result["weight"] == 0.7
        assert bg._ingester.inject_custom_kg.call_count == 2
        call_kwargs = bg._ingester.inject_custom_kg.call_args_list[1]
        rels = call_kwargs[1]["relationships"]
        assert rels[0]["weight"] == 0.7
```

替换为：
```python
    def test_store_memory_default_weight(self):
        """Memory without type should use default weight 0.7."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="Python的GIL机制导致多线程无法真正并行",
        )

        assert result["status"] == "ok"
        assert result["weight"] == 0.7
        assert bg._ingester.inject_custom_kg.call_count == 1
```

- [ ] **Step 3: 修改 metadata 测试 — 从 relationships 改为 entities**

`test_metadata_embedded_in_description`（行 397-412）：
当前断言 `rels[0]["description"]` 中有 `[meta:]`，改为断言 `entities[0]["description"]`：

```python
    def test_metadata_embedded_in_description(self):
        """metadata should be embedded as JSON in the entity description."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="用户提到了asyncio问题",
            metadata={"source": "chat", "turn": 5},
        )

        assert result["status"] == "ok"
        assert bg._ingester.inject_custom_kg.call_count == 1
        call_kwargs = bg._ingester.inject_custom_kg.call_args
        entities = call_kwargs[1]["entities"]
        desc = entities[0]["description"]
        assert "[meta:" in desc
        assert "source" in desc
```

`test_no_metadata_no_bracket`（行 414-427）：同理改为 `entities[0]["description"]`：

```python
    def test_no_metadata_no_bracket(self):
        """Without metadata, entity description should not contain [meta:]."""
        bg = _make_mock_brain_graph()

        result = bg.store_memory(
            content="用户提到了asyncio问题",
        )

        assert result["status"] == "ok"
        assert bg._ingester.inject_custom_kg.call_count == 1
        call_kwargs = bg._ingester.inject_custom_kg.call_args
        entities = call_kwargs[1]["entities"]
        desc = entities[0]["description"]
        assert "[meta:" not in desc
```

`test_metadata_too_long_skipped`（行 446-461）：同理：

```python
    def test_metadata_too_long_skipped(self):
        """Metadata exceeding 200 chars should be skipped entirely."""
        bg = _make_mock_brain_graph()

        big_meta = {"key": "x" * 300}
        result = bg.store_memory(
            content="测试内容",
            metadata=big_meta,
        )

        assert result["status"] == "ok"
        assert bg._ingester.inject_custom_kg.call_count == 1
        call_kwargs = bg._ingester.inject_custom_kg.call_args
        entities = call_kwargs[1]["entities"]
        desc = entities[0]["description"]
        assert "[meta:" not in desc
```

- [ ] **Step 4: 保留 `ensure_niu_entity` 测试不动**

行 286-299 的 `ensure_niu_entity` 测试验证 Niu 实体初始化，这是正常行为（Niu 与脑区连接），**保留不动**。

- [ ] **Step 5: 修改 `_extract_brain_memories_from_text` 回退测试**

`test_recall_extracts_brain_entities_from_text_fallback`（行 271-282）：

当前：
```python
    def test_recall_extracts_brain_entities_from_text_fallback(self):
        """recall_memories text fallback should extract brain: prefixed entities."""
        bg = _make_mock_brain_graph()
        bg._adapter.query_data.return_value = None
        bg._adapter.query.return_value = "brain:concept:Python is a language. brain:skill:Web_Development is useful."

        result = bg.recall_memories(query="编程技能")

        assert len(result) >= 2
        targets = [m["target"] for m in result]
        assert "brain:concept:Python" in targets
        assert "brain:skill:Web_Development" in targets
```

Task 2 Step 4 新逻辑从文本提取 target（`text.strip()[:20].split("。")[0].split("，")[0]`），不再硬编码 "Niu"。此测试的 query 返回值是 `"brain:concept:Python is a language. brain:skill:Web_Development is useful."`，新逻辑会提取 `"brain:concept:Python is a"` 作为 target（20字符内第一个句号前的内容）。

替换为：
```python
    def test_recall_extracts_brain_entities_from_text_fallback(self):
        """recall_memories text fallback should extract entities from text."""
        bg = _make_mock_brain_graph()
        bg._adapter.query_data.return_value = None
        bg._adapter.query.return_value = "brain:concept:Python is a language. brain:skill:Web_Development is useful."

        result = bg.recall_memories(query="编程技能")

        assert len(result) >= 1
        # New logic extracts target from text content, not hardcoded "Niu"
        assert result[0]["relation_type"] == "remembers"
        assert "Python" in result[0]["description"]
```

- [ ] **Step 6: Commit**

```bash
git add tests/test_brain_graph.py
git commit -m "test: update brain_graph tests — remove niu→entity relation assertions, fix metadata tests"
```

---

### Task 5: 修改 dream-evolver.md — 用脑区连接替代 niu 连接

**Files:**
- Modify: `config/agents/dream-evolver.md`

**原则**：用脑区连接（`_region:contains`）做示例和举例，不用 niu。dream-evolver 的"画像更新"任务改为将实体归入脑区，而非连接到 niu。

- [ ] **Step 1: 修正"知识图谱概念"段落中的示例**

先 Read 文件找到第 36 行附近的示例：

当前：
```markdown
例子：Niu --[prefers]--> Python
```

替换为：
```markdown
例子：知识体系脑区 --[_region:contains]--> Python
```

- [ ] **Step 2: 修正"检索模式"段落中的示例**

找到第 50 行附近的检索说明：

当前：
```markdown
→ 主 Agent 会看到：Niu --[prefers]--> Python, 程序记忆脑区 --[_region:contains]--> Python
```

替换为：
```markdown
→ 主 Agent 会看到：知识体系脑区 --[_region:contains]--> Python
```

- [ ] **Step 3: 修正"关系创建"段落中的代码示例**

找到第 58 行附近的代码示例：

当前：
```markdown
lightrag_insert_relation(src_id="Niu", tgt_id="FastAPI", relation="skilled_in")
```

替换为：
```markdown
lightrag_insert_relation(src_id="知识体系脑区", tgt_id="FastAPI", relation="_region:contains")
```

- [ ] **Step 4: 修正"关系创建结果"示例**

找到第 68 行附近的结果示例：

当前：
```markdown
→ 返回：Niu --[skilled_in]--> FastAPI
```

替换为：
```markdown
→ 返回：知识体系脑区 --[_region:contains]--> FastAPI
```

- [ ] **Step 5: 修正"实体类型"表格中的 niu 描述**

找到第 105 行附近的表格：

当前：
```markdown
| `Niu` | 用户画像主节点 | 用户偏好、技能、知识都连到这里 |
```

替换为：
```markdown
| `知识体系脑区` | 知识技能脑区 | 技能、概念等知识实体归入此脑区 |
```

- [ ] **Step 6: 修正"4步精加工工作流"中的"画像更新"任务**

找到第 142-143 行附近的任务4描述：

当前：
```markdown
4. **画像更新**（最后做）：更新 Niu 的偏好和技能
   - `lightrag_insert_relation(src_id="Niu", tgt_id=entity, relation="prefers"/"skilled_in"/"knows_about")`
```

替换为：
```markdown
4. **脑区归入**（最后做）：将实体归入对应脑区
   - `lightrag_insert_relation(src_id="脑区名", tgt_id=entity, relation="_region:contains")`
   - 先用 `lightrag_search_entities` 查找实体应归入哪个脑区
```

- [ ] **Step 7: 同步输出格式报告中的"画像更新"标签**

找到第 255 行：

当前：
```markdown
  - 画像更新：{n4} 条关系
```

替换为：
```markdown
  - 脑区归入：{n4} 条关系
```

- [ ] **Step 8: 验证**

检查文件中不再有 `Niu --[` 或 `src_id="Niu"` 或 `Niu` 作为连接起点的示例：

Run: `grep -n 'Niu.*--\[\|src_id="Niu"' <repo_root>/config/agents/dream-evolver.md || echo "No Niu connection patterns found - OK"`

- [ ] **Step 9: Commit**

```bash
git add config/agents/dream-evolver.md
git commit -m "fix: dream-evolver uses brain region connections instead of niu anchors"
```

---

### Task 6: 修改 entity-extractor.md — 删除对 niu 的不必要提及

**Files:**
- Modify: `config/agents/entity-extractor.md`

**原则**：entity-extractor 不涉及 niu 连接操作（它只通过 `lightrag_insert` 文档注入，不直接操作关系），不需要提及 niu。删除"用户主节点"的描述和不必要的 niu 示例。

- [ ] **Step 1: 删除"用户主节点：Niu"描述**

先 Read 文件，找到第 123 行附近的命名约定：

当前：
```markdown
- 用户主节点：`Niu`（不要写 `brain:Niu`）
```

替换为（entity-extractor 不涉及 niu，改为通用命名规则）：
```markdown
- 根节点名称保留原样：`niu`（不要写 `brain:niu`）
```

- [ ] **Step 2: 删除 "Niu 喜欢暗色主题" 示例**

找到第 132 行附近的书写示例：

当前：
```markdown
- 写"Niu 喜欢暗色主题"，不要写"brain:Niu 喜欢暗色主题"
```

替换为（用通用示例替代）：
```markdown
- 写"Python编程语言"，不要写"concept:Python编程语言"
```

- [ ] **Step 3: Commit**

```bash
git add config/agents/entity-extractor.md
git commit -m "fix: entity-extractor removes unnecessary niu references — sub-agents don't need to know about niu root node"
```

---

### Task 7: 验证

- [ ] **Step 1: 搜索残留的 niu 连接模式**

```bash
cd <repo_root> && grep -rn 'src_id="Niu"\|"Niu".*tgt_id\|Niu.*--\[' config/agents/ agent/injector/ mcp-servers/lightrag-server/src/ niu_api/internal/brain_graph.py niu_api/internal/lightrag_adapter.py docs/kg-dev-dictionary.md 2>/dev/null | grep -v '脑区\|brain_region\|region_anchor' || echo "No Niu connection violations found"
```

预期：无输出（或只有 Niu 与脑区连接的正常模式）。

- [ ] **Step 2: 运行相关测试**

```bash
cd <repo_root> && python -m pytest tests/ -v -k "lightrag or brain" 2>&1 | tail -30
```

- [ ] **Step 3: 更新 SYSTEM_MANUAL.md**

在"工具注入机制"段落的"上下文去重原则"后追加：

```markdown

**niu 根节点规则**：
- `niu` 是知识图谱根节点，只与脑区连接，不与普通实体直接连接
- 运行时代码（`lightrag_insert_entity`、`store_memory`）不创建 niu→实体锚边
- 实体可达性由脑区 `_region:contains` 边保证
```

- [ ] **Step 4: Commit**

```bash
git add docs/SYSTEM_MANUAL.md
git commit -m "docs: add niu root node rules to SYSTEM_MANUAL"
```

---

## 自查清单

### 1. Spec 覆盖度

| 问题 | 对应 Task | 处理方式 |
|------|-----------|---------|
| `lightrag_insert_entity` 自动创建 niu→实体锚边 | Task 1 | 删除锚边逻辑 + 修改工具描述 + 删除 niu 示例 |
| `store_memory` 创建 niu→target 关系 | Task 2 | 删除关系构建 + metadata 嵌入移至 entity_description + 移除记忆召回 niu 过滤 + 更新文档 |
| `upsert_interaction_habit` 嵌入 "Niu uses" | Task 3 | 删除 "Niu uses" 语句 |
| `dream_writer.py` 嵌入 "Niu {relation}" 和 "Niu experienced" | Task 4 | 删除 niu 引用 + 删除 `_NIU_RELATION_MAP` + 修复 logger |
| `test_dream_writer.py` 引用 NIU_ENTITY 和 _determine_niu_relation | Task 4a | 移除 import + 删除测试函数 + 修改断言 |
| `test_lightrag_server.py` 断言 niu 锚边 | Task 2a | 移除 relationships 断言 |
| `test_brain_graph.py` 断言 niu 关系 | Task 2b | 移除关系断言 + metadata 测试改用 entities + mock 数据改用脑区 src |
| dream-evolver 用 niu 做连接示例 | Task 5 | 改为脑区连接示例 |
| entity-extractor 不必要提及 niu | Task 6 | 删除"用户主节点"描述 + 替换示例 |

### 2. Placeholder 扫描

无 TBD/TODO。所有 old_string 和 new_string 都基于实际文件内容。

### 3. 类型一致性

- `_region:contains` 关系方向：脑区→实体（与 `brain_region_prompt.py` 一致）
- `relationships=[]` 替代 `relationships=[{...}]` 保持 `inject_custom_kg` 调用签名不变
- metadata 嵌入从 relation `description` 移至 `entity_description`，`format_memories_for_prompt` 仍通过 `description` 字段 strip `[meta:]`（metadata 在 entity description 中，`_extract_brain_memories_from_text` 返回的 description 来自文本而非 entity dict，故不受影响）
