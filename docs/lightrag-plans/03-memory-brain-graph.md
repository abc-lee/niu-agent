# Memory Brain Graph — LightRAG Integration Plan

> 最后更新：2026-04-22
> 状态：✅ 方案已讨论确认

## 1. Executive Summary

This document designs a "brain graph" architecture that replaces the current flat vector-based memory system (L0/L1/L2 in `vectors.db`) with a structured knowledge graph built on LightRAG. The core insight: memories are not isolated records but **associations between entities**. A brain graph stores people, concepts, skills, and events as entities, and memories as relations between them. Retrieval follows graph paths (association-based recall) rather than relying solely on cosine similarity.

**Current system (C1):** Memory server stores snippets as vector records in `vectors.db` (shared with vector-store), with level tags L0/L1/L2. Retrieval is purely cosine similarity + level filter.

**Target system (C2):** A brain graph inside LightRAG where:
- **主实体 `brain:Niu`** — 所有记忆关系从它出发，类似人脑的"自我"
- Entities = people, concepts, skills, events, projects
- Relations = memories connecting `brain:Niu` to entities (with descriptions, timestamps, weight)
- Retrieval = 直接使用 LightRAG 的 aquery(mode="mix")，不自建检索逻辑
- **上下文注入** — 脑图检索结果注入到 Agent 系统提示词，让 Agent "显得聪明"

### 讨论确认的关键决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | 主实体 `brain:Niu`，所有记忆从它出发 | 类似人脑"自我"节点，所有偏好/技能/经验都是它与实体的关系 |
| D2 | 去掉 Preference 实体类型 | 偏好不是独立实体，是 `brain:Niu --prefers--> X` 关系 |
| D3 | 实体类型精简为5种 | Person/Concept/Skill/Event/Project 足够，Place/Resource/Emotion/Goal 用例太少 |
| D4 | 检索直接用 LightRAG aquery | 不自建 association/vector/graph_walk 三套检索，LightRAG 天然做"点亮" |
| D5 | 共享 LightRAG 实例，不独立 | 脑图节点和文档实体需要连接，独立实例会断开连接 |
| D6 | 无数据迁移 | 没有历史数据，直接替换代码 |
| D7 | 核心价值在上下文注入 | 脑图的意义不是存储，是让 Agent 在对话时"点亮"足够的历史记忆 |

---

## 2. Entity Schema

### 2.1 Entity Types

精简为5种核心类型。Preference 不再是独立实体，改为 `brain:Niu --prefers--> X` 关系。Place/Resource/Emotion/Goal 用例太少，遇到时用 Concept 代替。

| Type | Description | Key Properties | Example |
|------|-------------|----------------|---------|
| `Niu` | 主实体，所有记忆从它出发 | （唯一实例） | `brain:Niu` |
| `Person` | 用户提及或交互的人 | `role`, `relationship` | `brain:person:LiLei` |
| `Concept` | 抽象概念、技术、领域（含地点、资源等） | `domain`, `expertise_level` | `brain:concept:Rust_Programming` |
| `Skill` | 用户能力和熟练度 | `level` (beginner/intermediate/expert) | `brain:skill:Web_Development` |
| `Event` | 时间绑定的事件 | `timestamp`, `significance` | `brain:event:2026_04_LightRAG_Integration` |
| `Project` | 用户参与的工作项目 | `status`, `role`, `tech_stack` | `brain:project:AI_Bot` |

### 2.2 Entity Property Schema

Every brain graph entity MUST include these base properties:

```python
{
    "entity_name": "brain:person:LiLei",        # Namespaced name
    "entity_type": "Person",                     # Entity type from table above
    "description": "Software engineer, project owner of ai-bot",
    "source_id": "brain",                        # Distinguishes from document entities
    "brain_meta": {                              # Brain-graph-specific metadata
        "created_at": "2026-04-22T10:00:00Z",
        "updated_at": "2026-04-22T10:00:00Z",
        "access_count": 3,                       # How often recalled
        "last_accessed": "2026-04-22T12:00:00Z",
        "confidence": 0.9,                       # 0.0-1.0, how confident we are
        "origin_level": "L1",                    # Which L-tier this came from
    }
}
```

### 2.3 Namespace Convention

All brain graph entities use the prefix `brain:` to separate them from document-extracted entities:

```
brain:{type}:{normalized_name}
```

Examples:
- `brain:Niu` （主实体，唯一）
- `brain:person:LiLei`
- `brain:concept:Knowledge_Graph`
- `brain:skill:Web_Development`
- `brain:event:2026_04_LightRAG_Integration`
- `brain:project:AI_Bot`

**Normalization rules:**
- Replace spaces with underscores
- Remove special characters except underscore and hyphen
- Capitalize first letter of each word (PascalCase within the name segment)
- Keep names under 64 characters

### 2.4 Distinguishing Brain Graph Entities from Document Entities

Three-layer separation strategy:

1. **Name prefix**: `brain:*` vs document entities which have no prefix (e.g., `Python` from a PDF)
2. **`source_id` property**: Brain entities have `source_id = "brain"`, document entities have `source_id` pointing to the document chunk
3. **`entity_type` property**: Brain entities use the typed schema above; document entities use LightRAG's default types (typically untyped or auto-extracted)

This allows the same LightRAG instance to hold both brain graph and document knowledge without collision.

---

## 3. Relation Schema

### 3.1 Relation Types

Relations represent **memories** — the connections between `brain:Niu` and entities that encode what the system remembers. 所有记忆关系都从 `brain:Niu` 出发。

| Relation Type | Direction | Description | Example |
|---------------|-----------|-------------|---------|
| `remembers` | Niu -> Any | 事实性记忆 | Niu -> Python: "从2019年开始用Python做AI/ML" |
| `prefers` | Niu -> Concept | 偏好（替代原 Preference 实体） | Niu -> Dark_Mode: "编码时偏好暗色主题" |
| `skilled_in` | Niu -> Skill | 能力及熟练度 | Niu -> Web_Dev: "expert级别" |
| `learned_from` | Niu -> Concept/Event | 知识来源 | Niu -> Rust_Book: "通过这本书学了ownership模型" |
| `participated_in` | Niu -> Event/Project | 参与 | Niu -> AI_Bot: "作为主开发者" |
| `located_at` | Niu -> Concept | 位置 | Niu -> Beijing: "远程工作" |
| `associated_with` | Any -> Any | 实体间自由关联 | Python -> Data_Science: "via NumPy ecosystem" |
| `related_to` | Niu -> Any | L0 原始记忆的泛关联 | Niu -> AsyncIO: "用户提到asyncio问题" |
| `evolved_from` | Concept -> Concept | 知识进化 | L0_Snippet -> L1_Summary: "记忆巩固" |

### 3.2 Relation Property Schema

Every brain graph relation carries metadata for memory management:

```python
{
    "source_id": "brain:person:LiLei",
    "target_id": "brain:concept:Python",
    "relation_type": "remembers",
    "description": "Has been using Python since 2019, primarily for AI/ML projects",
    "weight": 0.85,                              # Memory strength: 0.0-1.0
    "keywords": ["python", "ai", "ml", "2019"],  # For keyword search
    "brain_meta": {
        "created_at": "2026-04-22T10:00:00Z",
        "updated_at": "2026-04-22T10:00:00Z",
        "origin_level": "L1",                    # L0, L1, or L2
        "access_count": 5,                       # Times recalled
        "last_accessed": "2026-04-22T12:00:00Z",
        "confidence": 0.9,                       # How confident in this memory
        "decay_rate": 0.01,                      # Per-day decay factor
        "reinforced_by": [                       # What interactions reinforced this
            "conversation:2026-04-20:01",
            "conversation:2026-04-22:03"
        ]
    }
}
```

### 3.3 Memory Strength (Weight)

The `weight` field encodes how strongly a memory should be recalled:

| Weight Range | Meaning | Retrieval Behavior |
|-------------|---------|-------------------|
| 0.9 - 1.0 | Core memory (identity, strong preferences) | Always recalled in relevant context |
| 0.7 - 0.9 | Active memory (recent, frequently accessed) | High-priority recall |
| 0.5 - 0.7 | Established memory (confirmed, stable) | Normal recall |
| 0.3 - 0.5 | Fading memory (infrequently accessed) | Recalled when explicitly relevant |
| 0.1 - 0.3 | Weak memory (old, rarely accessed) | Only recalled on direct query |
| 0.0 - 0.1 | Nearly forgotten (candidates for removal) | Archived or removed |

Weight updates:
- **Reinforcement**: When a relation is recalled or re-mentioned in conversation, weight increases by `+0.1` (capped at 1.0)
- **Decay**: Every 24 hours without access, weight decreases by `decay_rate` (default 0.01)
- **Consolidation**: When L0 memories merge into L1, the resulting relation gets weight = max(constituent weights) + 0.1

---

## 4. Memory Injection — Converting L0/L1/L2 to Graph Entities/Relations

### 4.1 Injection Strategy

Use `ainsert_custom_kg()` (LightRAG's method for inserting pre-structured entity/relation data) rather than `ainsert()`, because:
- Brain graph memories are already structured (we know the entities and relations)
- `ainsert()` would re-extract entities via LLM, which is wasteful and may distort our schema
- `ainsert_custom_kg()` accepts `KnowledgeGraph` objects with explicit `Entity` and `Relation` lists

### 4.2 L0 (Raw Conversation Snippets) -> Graph

L0 memories are short-term, raw conversation fragments. They map to **low-weight, `related_to` relations** between existing or newly extracted entities.

**Conversion process:**

```python
async def inject_l0_memory(content: str, metadata: dict, rag: LightRAG):
    """
    Convert an L0 raw snippet into brain graph entities and relations.

    L0 memories are ephemeral. We extract key entities and create
    lightweight 'related_to' relations with low weight (0.3).
    """
    # Step 1: Extract entities from the snippet using LLM
    entities = await extract_brain_entities(content)  # Returns list of typed entities

    # Step 2: Create or find entities in the graph
    kg = KnowledgeGraph()
    for ent in entities:
        kg.entities.append(Entity(
            entity_name=f"brain:{ent.type}:{ent.name}",
            entity_type=ent.type,
            description=ent.description,
            source_id="brain",
        ))

    # Step 3: Create 'related_to' relations between entities
    for i, src in enumerate(entities):
        for tgt in entities[i+1:]:
            kg.relations.append(Relation(
                source_id=f"brain:{src.type}:{src.name}",
                target_id=f"brain:{tgt.type}:{tgt.name}",
                relation_type="related_to",
                description=content[:200],  # Truncate raw snippet
                weight=0.3,  # Low weight for L0
                keywords=extract_keywords(content),
            ))

    # Step 4: Also connect to the User entity
    for ent in entities:
        kg.relations.append(Relation(
            source_id="brain:person:User",  # The user's anchor entity
            target_id=f"brain:{ent.type}:{ent.name}",
            relation_type="remembers",
            description=content[:200],
            weight=0.3,
        ))

    await rag.ainsert_custom_kg(kg)
```

**L0 characteristics in the brain graph:**
- Weight: 0.3 (low)
- Relation type: `related_to` (generic, unstructured)
- Description: raw snippet text (truncated to 200 chars)
- Decay rate: 0.05 (fast decay — L0 memories fade quickly)
- Keywords: extracted for searchability

### 4.3 L1 (Summarized Memories) -> Graph

L1 memories are medium-term, summarized/abstracted. They map to **typed relations with moderate weight**.

**Conversion process:**

```python
async def inject_l1_memory(summary: str, metadata: dict, rag: LightRAG):
    """
    Convert an L1 summary into brain graph entities and typed relations.

    L1 memories are structured. We use LLM to identify:
    - Specific entity types (Person, Concept, Preference, etc.)
    - Specific relation types (prefers, skilled_in, learned_from, etc.)
    """
    # Step 1: Use LLM to extract structured entities and relations
    extraction = await extract_structured_memory(summary)
    # Returns: {entities: [...], relations: [...]}

    kg = KnowledgeGraph()
    for ent in extraction.entities:
        kg.entities.append(Entity(
            entity_name=f"brain:{ent.type}:{ent.name}",
            entity_type=ent.type,
            description=ent.description,
            source_id="brain",
        ))

    for rel in extraction.relations:
        kg.relations.append(Relation(
            source_id=f"brain:{rel.source_type}:{rel.source_name}",
            target_id=f"brain:{rel.target_type}:{rel.target_name}",
            relation_type=rel.relation_type,  # Typed: prefers, skilled_in, etc.
            description=rel.description,
            weight=0.7,  # Moderate weight for L1
            keywords=rel.keywords,
        ))

    await rag.ainsert_custom_kg(kg)
```

**L1 characteristics in the brain graph:**
- Weight: 0.7 (moderate)
- Relation type: typed (`prefers`, `skilled_in`, `learned_from`, etc.)
- Description: summarized/abstracted text
- Decay rate: 0.01 (slow decay)
- Keywords: semantically meaningful

### 4.4 L2 (Deep Knowledge/Insights) -> Graph

L2 memories are long-term, deep knowledge and insights. They map to **high-weight, multi-connected entities** with rich relations.

**Conversion process:**

```python
async def inject_l2_memory(insight: str, metadata: dict, rag: LightRAG):
    """
    Convert an L2 deep insight into brain graph with rich connectivity.

    L2 memories are core knowledge. They:
    - Create entities with high confidence
    - Create multiple relation types to connect widely
    - Get high weight (0.9) because they represent deep understanding
    """
    extraction = await extract_structured_memory(insight, depth="deep")

    kg = KnowledgeGraph()
    for ent in extraction.entities:
        kg.entities.append(Entity(
            entity_name=f"brain:{ent.type}:{ent.name}",
            entity_type=ent.type,
            description=ent.description,
            source_id="brain",
        ))

    # L2 creates MORE relations — connecting to existing entities
    for rel in extraction.relations:
        kg.relations.append(Relation(
            source_id=f"brain:{rel.source_type}:{rel.source_name}",
            target_id=f"brain:{rel.target_type}:{rel.target_name}",
            relation_type=rel.relation_type,
            description=rel.description,
            weight=0.9,  # High weight for L2
            keywords=rel.keywords,
        ))

    # Additionally, create cross-connections to existing brain entities
    existing = await find_related_brain_entities(extraction.entities, rag)
    for match in existing:
        kg.relations.append(Relation(
            source_id=match.entity_name,
            target_id=f"brain:{extraction.entities[0].type}:{extraction.entities[0].name}",
            relation_type="associated_with",
            description=f"Related via insight: {insight[:100]}",
            weight=0.6,
        ))

    await rag.ainsert_custom_kg(kg)
```

**L2 characteristics in the brain graph:**
- Weight: 0.9 (high)
- Relation type: multiple typed relations + `associated_with` cross-connections
- Description: deep insight text
- Decay rate: 0.002 (very slow decay — core knowledge)
- Keywords: rich semantic keywords

### 4.5 Injection Summary

| Level | Weight | Relation Types | Decay Rate | Entity Creation | Cross-connections |
|-------|--------|----------------|------------|-----------------|-------------------|
| L0 | 0.3 | `related_to` only | 0.05/day | Extract key entities | To User entity only |
| L1 | 0.7 | Typed (`prefers`, `skilled_in`, etc.) | 0.01/day | Extract + type entities | To mentioned entities |
| L2 | 0.9 | Typed + `associated_with` | 0.002/day | Rich entity profiles | Wide cross-connections |

---

## 5. Memory Retrieval — 直接使用 LightRAG

### 5.1 核心原则：不自建检索逻辑

脑图的检索直接使用 LightRAG 的 `aquery(mode="mix")`，不自建 association/vector/graph_walk 三套检索。

LightRAG 天然做的是：**向量找种子实体 → 图遍历扩展 → 返回相关实体+关系+文档**。这就是"点亮"——和人脑按场景激活关联区域的机制一致。

```python
async def recall_brain(query: str, rag: LightRAG, top_k: int = 10) -> str:
    """
    脑图检索：直接用 LightRAG 的 mix 模式。
    mix = local(实体) + global(关系) + chunks(文档块)，最全面。
    """
    result = await rag.aquery(query, mode="mix", only_need_context=True, top_k=top_k)
    return result
```

### 5.2 上下文注入（核心价值）

脑图的价值不在于存储，而在于**注入到 Agent 的系统提示词中**，让 Agent "显得聪明"。

当前 `_inject_dynamic_resources()` 注入4类：skill / mcp_tool / document / interaction_habit

**脑图替换后**，注入变为：

```python
def _inject_dynamic_resources(self, context: str) -> tuple[str, dict[str, int]]:
    """
    动态注入相关资源（重构后）

    注入顺序：
    1. LightRAG aquery(context, mode="mix")  ← 一次查询，结果包含：
       ├── 实体（人、概念、工具...）
       ├── 关系（偏好、技能、经验...）
       └── 文档块（知识、手册...）
    2. 从结果中分离：
       ├── Skills → 格式化为"相关技能"
       ├── MCP工具 → 提取分数（不注入提示词）
       ├── 知识文档 → 格式化为"参考知识"
       └── brain:Niu 的关系 → 格式化为"记忆"  ← 新增！
    3. 注入到系统提示词
    """
    # 一次 LightRAG 查询替代多次向量检索
    result = call_async(self.rag.aquery(context, mode="mix", only_need_context=True))

    # 分离结果
    skills = extract_skills(result)
    mcp_tools = extract_mcp_tools(result)
    knowledge = extract_knowledge(result)
    memories = extract_brain_memories(result)  # 新增：提取 brain:Niu 的关系

    # 格式化
    parts = []
    if skills:
        parts.append(format_resources_for_prompt(skills, "相关技能"))
    if knowledge:
        parts.append(format_resources_for_prompt(knowledge, "参考知识"))
    if memories:
        parts.append(format_memories_for_prompt(memories))  # 新增
    ...
```

**"记忆"部分的注入格式**，比当前简单的向量匹配丰富得多：

```
### [记忆]
- 你擅长 Web Development（expert），偏好 Python
- 你从2019年开始用Python做AI/ML项目
- 你在参与 AI_Bot 项目，是主开发者
- 你偏好 Dark_Mode 编码环境
- 你最近在学 Rust，通过 Rust_Book 学习了 ownership 模型
- 你在北京远程工作
```

这些信息让 Agent 知道"你是谁、你擅长什么、你在做什么"，对话时就不显得"傻"了。

### 5.3 与文档知识的交叉点亮

`brain:Niu` 的关系和文档提取的实体在同一个图谱中。用户提到"Python"时，LightRAG 同时找到：
- `brain:Niu --skilled_in--> Python`（记忆）
- 照片中出现的 Python 相关场景（文档知识）
- 系统手册中 Python 相关章节（文档知识）

一起返回，这才是联想式回忆。

### 5.4 brain:Niu 关系提取

从 LightRAG aquery 结果中提取 brain:Niu 的关系：

```python
def extract_brain_memories(query_result: str, graph: nx.Graph) -> list[dict]:
    """
    从 aquery 结果中提取 brain:Niu 的关系作为记忆。
    """
    memories = []
    # 遍历 brain:Niu 的所有出边
    if "brain:Niu" in graph:
        for neighbor, data in graph["brain:Niu"].items():
            rel_type = data.get("relation_type", "")
            description = data.get("description", "")
            weight = data.get("weight", 0.5)
            if weight < 0.3:  # 低于阈值的记忆不注入
                continue
            memories.append({
                "target": neighbor,
                "relation_type": rel_type,
                "description": description,
                "weight": weight,
            })
    return sorted(memories, key=lambda m: m["weight"], reverse=True)
```

---

## 6. Memory Consolidation — Forgetting and Reinforcement

### 6.1 Weight Decay (Forgetting Curve)

Inspired by Ebbinghaus's forgetting curve, brain graph relation weights decay over time unless reinforced.

**Daily decay job:**

```python
async def decay_brain_memories(rag: LightRAG, days_inactive: int = 1):
    """
    Decay all brain graph relation weights based on time since last access.

    Runs as a scheduled daily task (using the existing scheduler infrastructure).

    Decay formula:
        new_weight = weight * (1 - decay_rate * days_inactive)

    Relations below MIN_WEIGHT (0.1) are candidates for removal.
    """
    graph = rag.chunk_entity_relation_graph
    to_remove = []

    for u, v, key, data in graph.edges(keys=True, data=True):
        if not u.startswith("brain:") and not v.startswith("brain:"):
            continue  # Skip non-brain edges

        weight = data.get("weight", 0.5)
        decay_rate = data.get("brain_meta", {}).get("decay_rate", 0.01)
        last_accessed = data.get("brain_meta", {}).get("last_accessed")

        if last_accessed:
            days = (datetime.now() - parse_date(last_accessed)).days
        else:
            days = days_inactive

        new_weight = max(0.0, weight * (1 - decay_rate * days))

        if new_weight < 0.1:
            to_remove.append((u, v, key))
        else:
            data["weight"] = new_weight

    # Remove forgotten memories
    for u, v, key in to_remove:
        graph.remove_edge(u, v, key)
        # If this leaves orphan entities (no edges), remove those too
        if graph.degree(u) == 0 and u.startswith("brain:"):
            graph.remove_node(u)
        if graph.degree(v) == 0 and v.startswith("brain:"):
            graph.remove_node(v)
```

**Decay rates by L-tier:**

| Origin Level | Default Decay Rate | Time to 0.1 weight |
|-------------|-------------------|---------------------|
| L0 | 0.05/day | ~45 days |
| L1 | 0.01/day | ~230 days |
| L2 | 0.002/day | ~1150 days (~3 years) |

### 6.2 Reinforcement on Recall

When a memory is recalled (used in a conversation), its weight increases:

```python
async def reinforce_memory(entity_name: str, relation_key: str, rag: LightRAG):
    """
    Reinforce a brain graph memory when it's recalled.

    Reinforcement rules:
    - Weight increases by +0.1 (capped at 1.0)
    - access_count incremented
    - last_accessed updated to now
    - decay_rate slightly decreased (memory becomes more stable)
    """
    graph = rag.chunk_entity_relation_graph
    # Find the edge and update its data
    ...
```

### 6.3 Memory Consolidation (L0 -> L1 -> L2)

The existing L0->L1->L2 promotion logic translates to graph consolidation:

**L0 -> L1 consolidation:**

```python
async def consolidate_l0_to_l1(rag: LightRAG):
    """
    Periodically consolidate L0 raw memories into L1 structured memories.

    Algorithm:
    1. Find all L0 relations (relation_type == "related_to", weight > 0.25)
    2. Cluster them by shared entities
    3. For each cluster, use LLM to generate a summary
    4. Create new L1 typed relations with the summary
    5. Remove the original L0 relations (or reduce weight to 0.1)
    """
    graph = rag.chunk_entity_relation_graph

    # Step 1: Collect L0 relations
    l0_relations = []
    for u, v, key, data in graph.edges(keys=True, data=True):
        if (data.get("relation_type") == "related_to"
            and data.get("brain_meta", {}).get("origin_level") == "L0"
            and data.get("weight", 0) > 0.25):
            l0_relations.append((u, v, key, data))

    # Step 2: Cluster by shared entities
    clusters = cluster_by_entities(l0_relations)

    # Step 3-4: For each cluster, generate summary and create L1 relations
    for cluster in clusters:
        descriptions = [r[3]["description"] for r in cluster]
        summary = await llm_summarize(descriptions)

        # Extract structured entities/relations from summary
        extraction = await extract_structured_memory(summary)
        kg = KnowledgeGraph()
        # ... (same as inject_l1_memory)
        await rag.ainsert_custom_kg(kg)

    # Step 5: Remove old L0 relations
    for u, v, key, data in l0_relations:
        graph.remove_edge(u, v, key)
```

**L1 -> L2 consolidation:**

```python
async def consolidate_l1_to_l2(rag: LightRAG):
    """
    Periodically consolidate L1 memories into L2 deep knowledge.

    Trigger conditions:
    - L1 relation has been accessed >= 5 times
    - L1 relation weight >= 0.8 (reinforced through repeated recall)
    - At least 3 L1 relations connect the same entity cluster

    Algorithm:
    1. Find L1 relations meeting criteria
    2. Group by entity neighborhood
    3. Use LLM to synthesize deep insights
    4. Create L2 relations with cross-connections
    5. Keep L1 relations (they serve as pathways to L2)
    """
    ...
```

### 6.4 Entity Deduplication

When new memories are extracted, they may create duplicate entities:

```python
async def deduplicate_brain_entities(rag: LightRAG):
    """
    Find and merge duplicate brain graph entities.

    Deduplication strategy:
    1. For each brain: entity, compute embedding of its description
    2. Find pairs with cosine similarity > 0.9
    3. Merge using LightRAG's entity merging (amerge_entities_if_similar)
    4. Merged entity gets:
       - Combined description
       - Max confidence of the pair
       - All relations from both entities
       - Updated access_count = sum of both
    """
    ...
```

LightRAG already has entity merging capability via `amerge_entities_if_similar()`. We leverage this with a brain-graph-specific similarity threshold (0.9 for high-confidence dedup).

### 6.5 Promotion of Frequently Accessed Memories

Relations with high access counts get promoted automatically:

```python
def check_promotion(relation_data: dict) -> bool:
    """
    Check if a relation should be promoted to a higher tier.

    Promotion rules:
    - L0 -> L1: access_count >= 3 AND weight >= 0.5
    - L1 -> L2: access_count >= 10 AND weight >= 0.85
    """
    level = relation_data.get("brain_meta", {}).get("origin_level")
    access_count = relation_data.get("brain_meta", {}).get("access_count", 0)
    weight = relation_data.get("weight", 0)

    if level == "L0" and access_count >= 3 and weight >= 0.5:
        return True  # Promote to L1
    if level == "L1" and access_count >= 10 and weight >= 0.85:
        return True  # Promote to L2
    return False
```

---

## 7. 与文档知识共享图谱

### 7.1 策略：共享实例，命名空间约定

脑图和文档知识**共享同一个 LightRAG 实例**。`brain:` 前缀只是命名约定，不是隔离墙。

理由：
- **交叉点亮**：`brain:Niu --skilled_in--> Python` 和照片中提取的 `Python` 实体需要连接
- **联想式回忆**：用户提到"Python"时，LightRAG 同时返回记忆和文档知识
- **共享 Embedding**：同一模型，节省内存
- **更简单的部署**：一个 LightRAG 实例

### 7.2 命名空间约定

| 层 | 机制 | 用途 |
|---|------|------|
| 名称 | `brain:` 前缀 | 防止与文档实体名称冲突 |
| 属性 | `source_id = "brain"` | 查询时可过滤 |
| 类型 | 脑图实体类型 vs 文档提取类型 | Schema 级区分 |

### 7.3 Query-Time Separation

When querying, we need to optionally restrict results to brain-only or document-only:

```python
async def brain_only_query(query: str, rag: LightRAG, mode: str = "local") -> str:
    """
    Query the brain graph specifically, excluding document knowledge.

    Implementation options:
    1. Use aquery() and post-filter the text results
    2. Use direct NetworkX graph traversal for precise control
    3. Use aquery() with carefully crafted queries that target brain: entities

    Recommended: Option 2 for brain-only, Option 3 for combined.
    """
    # Option 2: Direct graph query
    graph = rag.chunk_entity_relation_graph
    # Find brain: entities matching the query
    # Traverse their neighborhoods
    # Return formatted results
    ...

async def combined_query(query: str, rag: LightRAG, mode: str = "hybrid") -> str:
    """
    Query both brain graph and document knowledge.

    Uses LightRAG's standard aquery() — results include both brain:
    and document entities, which is desirable for comprehensive answers.
    """
    return await rag.aquery(query, mode=mode)
```

### 7.4 When to Use Which

| Scenario | Query Type | Rationale |
|----------|-----------|-----------|
| "What do I know about Python?" | Combined | Want both personal memories and document knowledge |
| "What are my preferences?" | Brain-only | Only personal preferences matter |
| "Remind me what I worked on last week" | Brain-only | Personal memory only |
| "Explain how async/await works" | Document-only | Factual knowledge, not personal memory |
| "How does our project use async?" | Combined | Need both personal context and technical docs |

---

## 8. Implementation Plan

无历史数据迁移，直接替换代码。

### Phase 0: Create brain-server Module

1. Create `mcp-servers/brain-server/` with TOOL_SCHEMAS
2. Implement `brain_remember`, `brain_recall`, `brain_consolidate`, `brain_decay`
3. Register in `mcp-servers.yaml` and `config/agents/niu.md`
4. Initialize `brain:Niu` entity on first startup

### Phase 1: Replace memory-server

1. Replace `remember`/`recall` tool calls with `brain_remember`/`brain_recall`
2. Update `handler.py` tool dispatch
3. Update `_inject_dynamic_resources()` to include brain memory injection
4. Remove memory-server from `mcp-servers.yaml`

### Phase 2: Remove Legacy Code

1. Remove `mcp-servers/memory-server/` directory
2. Remove memory-server references from `config/agents/niu.md`
3. Clean up `vectors.db` memory-related tables (if any)

---

## 9. Implementation Architecture

### 9.1 Brain Server Module Structure

```
mcp-servers/brain-server/
├── src/
│   └── niu_brain_server/
│       ├── __init__.py          # MCP tool definitions + TOOL_SCHEMAS
│       ├── __main__.py          # Entry point
│       ├── schema.py            # Entity and relation schema definitions
│       ├── extractor.py         # LLM-based entity/relation extraction
│       ├── recall.py            # Recall strategies (association, vector, graph-walk)
│       ├── consolidation.py     # L0->L1->L2 consolidation + decay
│       └── migration.py         # Vectors.db -> brain graph migration
└── pyproject.toml
```

### 9.2 Integration Points

```
brain-server
  ├── Uses LightRAG instance (shared with kg-server)
  │   ├── rag.ainsert_custom_kg() for injection
  │   ├── rag.aquery() for recall
  │   └── rag.chunk_entity_relation_graph for direct graph access
  ├── Uses ToolRegistry for MCP tool registration
  │   └── brain_remember, brain_recall, brain_consolidate, etc.
  └── Uses shared vector-store embedding model
      └── For entity description embeddings
```

### 9.3 Scheduled Tasks

Leverage the existing scheduler infrastructure (`docs/feature-scheduled-tasks.md`):

| Task | Schedule | Description |
|------|----------|-------------|
| `brain_decay` | Daily at 03:00 | Decay relation weights |
| `brain_consolidate_l0_to_l1` | Daily at 04:00 | Promote L0 memories to L1 |
| `brain_consolidate_l1_to_l2` | Weekly (Sunday 05:00) | Promote L1 memories to L2 |
| `brain_deduplicate` | Weekly (Sunday 06:00) | Merge duplicate entities |

### 9.4 Configuration

Add to `~/.niu/preferences.json`:

```json
{
  "brain_graph": {
    "enabled": true,
    "lightrag_working_dir": "~/.niu/brain_graph",
    "recall_mode": "hybrid",
    "default_decay_rate_l0": 0.05,
    "default_decay_rate_l1": 0.01,
    "default_decay_rate_l2": 0.002,
    "min_weight": 0.1,
    "reinforcement_boost": 0.1,
    "max_recall_depth": 2,
    "consolidation_min_access_l0_to_l1": 3,
    "consolidation_min_access_l1_to_l2": 10,
    "dedup_similarity_threshold": 0.9
  }
}
```

---

## 10. API Design — Brain Graph MCP Tools

### 10.1 `brain_remember`

```python
TOOL_SCHEMAS = {
    "brain_remember": {
        "description": "Store a new memory in the brain graph. Automatically extracts entities and relations from the content.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The memory content to store"
                },
                "level": {
                    "type": "string",
                    "enum": ["L0", "L1", "L2"],
                    "description": "Memory level: L0=raw, L1=summary, L2=insight. Default: L0"
                },
                "metadata": {
                    "type": "object",
                    "description": "Additional metadata (source, context, etc.)"
                }
            },
            "required": ["content"]
        }
    }
}
```

### 10.2 `brain_recall`

```python
"brain_recall": {
    "description": "Recall memories from the brain graph using association, vector, or graph-walk search.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The recall query"
            },
            "mode": {
                "type": "string",
                "enum": ["association", "vector", "graph_walk", "hybrid"],
                "description": "Recall mode. Default: hybrid"
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum results to return. Default: 10"
            },
            "min_weight": {
                "type": "number",
                "description": "Minimum memory weight to include. Default: 0.3"
            },
            "entity_filter": {
                "type": "string",
                "description": "Only return memories involving this entity (e.g., 'brain:person:LiLei')"
            },
            "relation_filter": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only return these relation types (e.g., ['prefers', 'skilled_in'])"
            }
        },
        "required": ["query"]
    }
}
```

### 10.3 `brain_consolidate`

```python
"brain_consolidate": {
    "description": "Run memory consolidation: promote L0->L1 or L1->L2 memories based on access patterns.",
    "parameters": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["L0_to_L1", "L1_to_L2"],
                "description": "Consolidation direction"
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, report what would be consolidated without making changes. Default: false"
            }
        },
        "required": ["direction"]
    }
}
```

---

## 11. Risk Analysis and Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Brain graph grows too large, slowing queries | High | Medium | Decay/forgetting removes unused relations; max_depth limits on graph walks |
| LLM extraction errors create wrong entities | Medium | High | Confidence scoring; deduplication; human-in-the-loop for L2 |
| LightRAG instance instability affects both document and brain knowledge | High | Low | Separate `working_dir` for brain graph; can fallback to separate instance |
| Migration loses memories | Critical | Low | Dual-write phase; vectors.db never deleted; export/import backup |
| Namespace collision (`brain:` prefix not enforced) | Medium | Low | Validation in `brain_remember` tool; schema enforcement |
| Performance regression in recall | Medium | Medium | Benchmark before each phase; cache frequent queries |

---

## 12. Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Recall relevance | > 80% of recalled memories are relevant to query | Human evaluation on 50 sample queries |
| Recall latency | < 500ms for association recall, < 2s for hybrid recall | Automated benchmark |
| Migration completeness | 100% of L1+L2 memories migrated | Count comparison pre/post migration |
| Memory consolidation accuracy | > 90% of L1 summaries accurately represent L0 sources | Human evaluation |
| Brain graph size | < 10,000 entities, < 30,000 relations after 6 months | Graph statistics |
| Forgetting precision | < 5% of recalled memories are stale/irrelevant | User feedback |
