# LightRAG 属性值大小写统一 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一 LightRAG 图谱中 entity_type 和 keywords 的大小写处理，消除大小写不一致导致的重复实体、Counter 投票分裂和脑区识别失败问题。

**Architecture:** 原则是"写入时统一 .lower() 存储，查询时统一 .lower() 比较"。不硬编码具体的 entity_type 字符串去逐个匹配，而是靠 .lower() 规范化和 .lower() 比较来保证一致性。程序代码中只保留系统内部定义的特殊类型常量（如 "brainregion"），其余 LLM 动态输出的类型不做硬编码。

**Tech Stack:** Python, LightRAG (fork), NetworkX

---

## 核心原则

1. **写入时 .lower()** — 所有写入图谱的 entity_type 和 keywords 都经过 .lower() 规范化，这样图谱里只有小写
2. **查询时 .lower() 比较，但返回原值** — 比较时用 .lower() 确保匹配到数据（兼容存量大小写不一致的旧数据），但返回给调用方的数据原样返回，不做转换
3. **不硬编码 LLM 输出的类型** — entity_type 的值是 LLM 动态输出的，程序不应该穷举这些字符串
4. **绝不把读出来的数据转小写后返回给大模型** — 如果转了，大模型就会用小写建边，但图中存的可能还是大写（存量数据），就会创建重复实体

---

## 执行顺序

1. **提交1**：Task 1 + 2 + 3（LightRAG fork 写入侧 .lower() 规范化）
2. **提交2**：Task 4（查询侧 .lower() 匹配）
3. **提交3**：Task 5（dict 查找键 .lower() + LLM 提示词改小写）
4. **提交4**：Task 6（0实体脑区修复）

---

### Task 1: LightRAG fork — 写入侧 entity_type 统一 .lower()

**Files:**
- `REDACTED_USER_PATH/tools/LightRAG/lightrag/lightrag.py` — ainsert_custom_kg 入口
- `REDACTED_USER_PATH/tools/LightRAG/lightrag/operate.py` — Counter 投票、_rebuild、UNKNOWN 默认值
- `REDACTED_USER_PATH/tools/LightRAG/lightrag/utils_graph.py` — acreate_entity 默认值
- `REDACTED_USER_PATH/tools/LightRAG/lightrag/utils.py` — 实体格式化默认值

- [ ] **Step 1: ainsert_custom_kg 入口加 .lower()**

```python
# lightrag.py:2434 修改前:
entity_type = entity_data.get("entity_type", "UNKNOWN")

# 修改后:
entity_type = str(entity_data.get("entity_type", "unknown") or "unknown").replace(" ", "").lower()
```

- [ ] **Step 2: ainsert_custom_kg keywords 加 .lower()（两处）**

```python
# lightrag.py:2523 修改前:
"keywords": relationship_data["keywords"],
# 修改后:
"keywords": (relationship_data["keywords"] or "").lower(),

# lightrag.py:2535 修改前:
"keywords": relationship_data["keywords"],
# 修改后:
"keywords": (relationship_data["keywords"] or "").lower(),
```

- [ ] **Step 3: 缺失节点 entity_type 默认值改为 "unknown"**

```python
# lightrag.py:2510 修改前:
"entity_type": "UNKNOWN",
# 修改后:
"entity_type": "unknown",
```

- [ ] **Step 4: Counter 投票前统一 .lower()**

```python
# operate.py:1756-1762 修改前:
entity_type = sorted(
    Counter(
        [dp["entity_type"] for dp in nodes_data] + already_entity_types
    ).items(),
    key=lambda x: x[1],
    reverse=True,
)[0][0]

# 修改后:
entity_type = sorted(
    Counter(
        [str(dp.get("entity_type", "unknown") or "unknown").replace(" ", "").lower() for dp in nodes_data]
        + [str(et).replace(" ", "").lower() for et in already_entity_types]
    ).items(),
    key=lambda x: x[1],
    reverse=True,
)[0][0]
```

- [ ] **Step 5: _rebuild_single_entity 投票前统一 .lower()**

```python
# operate.py:1244 修改前:
entity_type = current_entity.get("entity_type", "UNKNOWN")
# 修改后:
entity_type = str(current_entity.get("entity_type", "unknown") or "unknown").replace(" ", "").lower()

# operate.py:1263 修改前:
entity_types.append(entity_data["entity_type"])
# 修改后:
entity_types.append(str(entity_data.get("entity_type", "unknown") or "unknown").replace(" ", "").lower())

# operate.py:1301 修改前:
current_entity.get("entity_type", "UNKNOWN")
# 修改后:
str(current_entity.get("entity_type", "unknown") or "unknown").replace(" ", "").lower()
```

- [ ] **Step 6: 所有 "UNKNOWN" 默认值改为 "unknown" + non_empty[0] 加 .lower()**

需要修改的位置（统一搜索替换 `"UNKNOWN"` → `"unknown"`，仅在 entity_type 上下文中）：
- operate.py:1517, 1544, 1652, 1658, 2269, 2299, 2315, 2406, 3837
- lightrag.py:2510
- utils_graph.py:962
- utils.py:3189, 3201

额外修改：
```python
# operate.py:1658 修改前:
entity_type = non_empty[0]
# 修改后:
entity_type = non_empty[0].strip().lower()
```

- [ ] **Step 7: utils_graph.py 独立写入路径加 .lower()**

`acreate_entity`、`acreate_relation`、`_edit_entity_impl` 是绕过 `ainsert_custom_kg` 的独立写入路径，必须单独加 .lower()。

```python
# utils_graph.py:962 修改前:
"entity_type": entity_data.get("entity_type", "UNKNOWN"),
# 修改后:
"entity_type": str(entity_data.get("entity_type", "unknown") or "unknown").replace(" ", "").lower(),
```

```python
# utils_graph.py:1094 修改前:
"keywords": relation_data.get("keywords", ""),
# 修改后:
"keywords": (relation_data.get("keywords") or "").lower(),
```

```python
# utils_graph.py:306 修改前:
new_node_data = {**node_data, **updated_data}
new_node_data["entity_id"] = new_entity_name

# 修改后:
new_node_data = {**node_data, **updated_data}
new_node_data["entity_id"] = new_entity_name
if "entity_type" in new_node_data:
    new_node_data["entity_type"] = str(new_node_data["entity_type"] or "unknown").replace(" ", "").lower()
```

```python
# utils_graph.py:794 aedit_relation 修改前:
new_edge_data = {**edge_data, **updated_data}

# 修改后:
new_edge_data = {**edge_data, **updated_data}
if "keywords" in new_edge_data:
    new_edge_data["keywords"] = (new_edge_data["keywords"] or "").lower()
```

```python
# utils_graph.py:1269 _merge_entities_impl 修改前:
for key, value in target_entity_data.items():
    merged_entity_data[key] = value

# 修改后:
for key, value in target_entity_data.items():
    merged_entity_data[key] = value
if "entity_type" in merged_entity_data:
    merged_entity_data["entity_type"] = str(merged_entity_data["entity_type"] or "unknown").replace(" ", "").lower()
```

```python
# utils_graph.py:1383 _merge_entities_impl 关系写入前 修改前:
for rel_data in relation_updates.values():
    await chunk_entity_relation_graph.upsert_edge(
        rel_data["graph_src"], rel_data["graph_tgt"], rel_data["data"]
    )

# 修改后:
for rel_data in relation_updates.values():
    if "keywords" in rel_data["data"]:
        rel_data["data"]["keywords"] = (rel_data["data"]["keywords"] or "").lower()
    await chunk_entity_relation_graph.upsert_edge(
        rel_data["graph_src"], rel_data["graph_tgt"], rel_data["data"]
    )
```

- [ ] **Step 8: keywords 写入路径统一 .lower()**

```python
# operate.py:517-520 修改前:
edge_keywords = edge_keywords.replace("，", ",")
# 修改后:
edge_keywords = edge_keywords.replace("，", ",").lower()

# operate.py:1420 _rebuild_single_relationship 修改前:
keywords.append(rel_data["keywords"])
# 修改后:
keywords.append((rel_data["keywords"] or "").lower())

# operate.py:1456-1460 _rebuild_single_relationship 修改前:
combined_keywords = (
    ", ".join(set(keywords))
    if keywords
    else current_relationship.get("keywords", "")
)
# 修改后:
combined_keywords = (
    ", ".join(set(keywords))
    if keywords
    else (current_relationship.get("keywords") or "").lower()
)

# operate.py:2106-2107 修改前:
k.strip() for k in keyword_str.split(",") if k.strip()
# 修改后:
k.strip().lower() for k in keyword_str.split(",") if k.strip()

# operate.py:2113-2114 修改前:
k.strip() for k in edge["keywords"].split(",") if k.strip()
# 修改后:
k.strip().lower() for k in edge["keywords"].split(",") if k.strip()
```

- [ ] **Step 9: 语法检查**

Run: `python -m py_compile REDACTED_USER_PATH/tools/LightRAG/lightrag/lightrag.py && python -m py_compile REDACTED_USER_PATH/tools/LightRAG/lightrag/operate.py && python -m py_compile REDACTED_USER_PATH/tools/LightRAG/lightrag/utils_graph.py && python -m py_compile REDACTED_USER_PATH/tools/LightRAG/lightrag/utils.py`
Expected: 无输出（编译通过）

- [ ] **Step 10: 提交**

```bash
git add REDACTED_USER_PATH/tools/LightRAG/lightrag/lightrag.py REDACTED_USER_PATH/tools/LightRAG/lightrag/operate.py REDACTED_USER_PATH/tools/LightRAG/lightrag/utils_graph.py REDACTED_USER_PATH/tools/LightRAG/lightrag/utils.py
git commit -m "fix(lightrag): normalize entity_type and keywords to lowercase in all write paths"
```

---

### Task 2: 查询侧 — 严格匹配改为 .lower() 比较

**原则：** 不硬编码具体 entity_type 字符串去逐个匹配，而是统一用 .lower() 比较。

**Files:**
- `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/lightrag_manager.py:187-189` — get_brain_regions()
- `REDACTED_USER_PATH/tools/ai-bot/niu_api/kg_api.py:177` — node_type == "Document"
- `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/lightrag_adapter.py:1082` — "Other" 默认值

- [ ] **Step 1: get_brain_regions() 改为 .lower() 比较**

```python
# 修改前 (line 187-189):
brain_regions = [
    name for name, data in snapshot.nodes(data=True)
    if data.get("entity_type") == "BrainRegion"
]

# 修改后:
brain_regions = [
    name for name, data in snapshot.nodes(data=True)
    if data.get("entity_type", "").lower() == "brainregion"
]
```

注：使用字符串 `"brainregion"` 而不是 `REGION_ENTITY_TYPE.lower()`，因为 `REGION_ENTITY_TYPE` 定义在 `region_manager.py`，未在 `lightrag_manager.py` 中导入，避免引入不必要的循环依赖。

- [ ] **Step 2: kg_api.py 的 "Document" 比较改为 .lower()**

```python
# 修改前 (line 174-179):
node_type = n.get("type", "Other")
if node_type == "Document":

# 修改后:
node_type = n.get("type", "other")
if node_type.lower() == "document":
```

注：`normalized_type` 是前端展示用的标签，保持 "Document" 不变。

- [ ] **Step 3: lightrag_adapter.py:1082 默认值改为 "other"**

```python
# 修改前:
nt = node_data.get("entity_type", "Other")
# 修改后:
nt = node_data.get("entity_type", "other")
```

注：`nt` 会被作为 `"entity_type": nt` 返回给前端，改小写后前端也需要处理。但前端已经在做分类展示，entity_type 是小写不影响功能。

- [ ] **Step 4: kg_api.py 中其他 "Other" 默认值改为 "other"**

kg_api.py 中约7处 `"Other"` 默认值（line 174, 631, 692, 739, 819, 879, 955）统一改为 `"other"`。

- [ ] **Step 5: lightrag_adapter.py:737 has_edge() keywords 比较改为 .lower()**

写入侧 keywords 统一 .lower() 后，has_edge() 的严格 `==` 比较会导致匹配失败（旧数据可能是大写 keywords），从而创建重复边。

```python
# 修改前 (line 736-737):
edge_data = nx_graph.get_edge_data(src, tgt)
return edge_data.get("keywords") == keywords

# 修改后:
edge_data = nx_graph.get_edge_data(src, tgt)
return edge_data.get("keywords", "").lower() == (keywords or "").lower()
```

- [ ] **Step 7: 语法检查**

Run: `python -m py_compile niu_api/internal/lightrag_manager.py niu_api/kg_api.py niu_api/internal/lightrag_adapter.py`
Expected: 无输出（编译通过）

- [ ] **Step 8: 提交**

```bash
git add niu_api/internal/lightrag_manager.py niu_api/kg_api.py niu_api/internal/lightrag_adapter.py
git commit -m "fix: use .lower() comparison for entity_type in all query paths"
```

---

### Task 3: dict 查找键 + LLM 提示词 — 查找时 .lower() 或键改为小写

**Files:**
- `REDACTED_USER_PATH/tools/ai-bot/agent/injector/dream_writer.py:46-52,268`
- `REDACTED_USER_PATH/tools/ai-bot/mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py:1175-1181,1182`
- `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/brain_region_prompt.py:39,71`
- `REDACTED_USER_PATH/tools/ai-bot/mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py:400,611`
- `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/lightrag_adapter.py:1324`
- `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/lightrag_manager.py:601-606`

- [ ] **Step 1: _NIU_RELATION_MAP 键改为小写 + 查找时 .lower()**

```python
# 修改前 (line 46-52):
_NIU_RELATION_MAP = {
    "Person": "remembers",
    "Skill": "skilled_in",
    "Concept": "knows_about",
    "Tool": "uses",
    "Preference": "prefers",
}

# 修改后:
_NIU_RELATION_MAP = {
    "person": "remembers",
    "skill": "skilled_in",
    "concept": "knows_about",
    "tool": "uses",
    "preference": "prefers",
}
```

```python
# 修改前 (line 268):
return _NIU_RELATION_MAP.get(entity_type)

# 修改后:
return _NIU_RELATION_MAP.get(entity_type.lower() if entity_type else None)
```

注：键改小写 + 查找时 .lower()，双重保险。不管 entity_type 是 "Person" 还是 "person"，都能正确匹配。

- [ ] **Step 2: lightrag-server niu_relation_map 键改为小写 + 查找时 .lower()**

```python
# 修改前 (line 1175-1181):
niu_relation_map = {
    "Person": "remembers",
    "Skill": "skilled_in",
    "Concept": "knows_about",
    "Tool": "uses",
    "Preference": "prefers",
}

# 修改后:
niu_relation_map = {
    "person": "remembers",
    "skill": "skilled_in",
    "concept": "knows_about",
    "tool": "uses",
    "preference": "prefers",
}
```

```python
# 修改前 (line 1182):
niu_relation = niu_relation_map.get(entity_type)

# 修改后:
niu_relation = niu_relation_map.get(entity_type.lower() if entity_type else None)
```

同理，键改小写 + 查找时 .lower()。

- [ ] **Step 3: CUSTOM_ENTITY_TYPES 改为小写**

```python
# 修改前 (line 601-606):
CUSTOM_ENTITY_TYPES = [
    "Person", "Organization", "Technology", "Concept",
    "Location", "Event", "Document", "Photo", "Video",
    "Note", "Chat", "Skill", "Tool", "Knowledge",
    "InteractionHabit", "EpisodicEvent", "BrainRegion", "Other",
]

# 修改后:
CUSTOM_ENTITY_TYPES = [
    "person", "organization", "technology", "concept",
    "location", "event", "document", "photo", "video",
    "note", "chat", "skill", "tool", "knowledge",
    "interactionhabit", "episodicevent", "brainregion", "other",
]
```

注：这个列表被注入到 LLM 提示词中，改小写后 LLM 输出小写，operate.py:441 再 .lower() 是幂等的，不会冲突。

- [ ] **Step 4: brain_region_prompt.py 中 BrainRegion 改为小写**

```python
# 修改前 (line 39):
脑区是图谱中的特殊实体节点，名称格式为 `XXX脑区`，类型为 `BrainRegion`。

# 修改后:
脑区是图谱中的特殊实体节点，名称格式为 `XXX脑区`，类型为 `brainregion`。
```

```python
# 修改前 (line 71):
- 不要创建照片(Photo)类型的实体。

# 修改后:
- 不要创建照片(photo)类型的实体。
```

- [ ] **Step 5: lightrag_adapter.py:1324 upsert_interaction_habit 文本改为小写**

```python
# 修改前:
text = f"交互习惯: {entity_name}（类型: InteractionHabit），{description}。Niu uses {entity_name}。"

# 修改后:
text = f"交互习惯: {entity_name}（类型: interactionhabit），{description}。Niu uses {entity_name}。"
```

- [ ] **Step 6: MCP schema 描述改为小写**

```python
# lightrag-server/__init__.py:400 修改前:
"Entity type (e.g., 'Person', 'Concept', 'Skill', 'Tool')"
# 修改后:
"Entity type (e.g., 'person', 'concept', 'skill', 'tool')"

# lightrag-server/__init__.py:611 修改前:
"Entity type (e.g., Person, Concept, Skill, Tool)"
# 修改后:
"Entity type (e.g., person, concept, skill, tool)"
```

- [ ] **Step 7: 语法检查**

Run: `python -m py_compile agent/injector/dream_writer.py mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py niu_api/internal/brain_region_prompt.py niu_api/internal/lightrag_adapter.py niu_api/internal/lightrag_manager.py`
Expected: 无输出（编译通过）

- [ ] **Step 8: 提交**

```bash
git add agent/injector/dream_writer.py mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py niu_api/internal/brain_region_prompt.py niu_api/internal/lightrag_adapter.py niu_api/internal/lightrag_manager.py
git commit -m "fix: use .lower() for dict lookups, lowercase in LLM prompts and schemas"
```

---

### Task 4: 系统内部常量改为小写 + 0实体脑区修复

**Files:**
- `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_manager.py` — REGION_ENTITY_TYPE, MIN_COMMUNITY_SIZE, create_region_nodes()

- [ ] **Step 1: REGION_ENTITY_TYPE 改为小写**

```python
# 修改前 (line 38):
REGION_ENTITY_TYPE = "BrainRegion"

# 修改后:
REGION_ENTITY_TYPE = "brainregion"
```

注：这是系统内部定义的特殊类型，不是 LLM 输出的，必须和写入侧 .lower() 后的值一致。

- [ ] **Step 2: 添加 MIN_COMMUNITY_SIZE 常量**

在 `region_manager.py` 的常量区域（line 57 附近）添加：

```python
# Minimum community size to create a brain region (must match region_detector default)
MIN_COMMUNITY_SIZE = 100
```

- [ ] **Step 3: 修改 create_region_nodes() 的成员数检查**

```python
# 修改前 (line 200-205):
if not members:
    logger.debug(
        "社区 %d 无有效成员（全为 brain:region:* 节点），跳过",
        partition.region_id,
    )
    continue

# 修改后:
if not members or len(members) < MIN_COMMUNITY_SIZE:
    logger.debug(
        "社区 %d 成员数 %d < %d，跳过",
        partition.region_id,
        len(members),
        MIN_COMMUNITY_SIZE,
    )
    continue
```

- [ ] **Step 4: lightrag_adapter.py 中所有 "Other" 默认值改为 "other" + "InteractionHabit" 默认值改为 "interactionhabit"**

`lightrag_adapter.py` 中约10处 entity_type 相关的 `"Other"` 默认值改为 `"other"`（line 43, 512, 524, 806, 1082, 1108, 1246, 1252, 1532, 1569）。

额外修改：
```python
# lightrag_adapter.py:1386 修改前:
entity_type = target_node.properties.get("entity_type", "InteractionHabit")  # noqa: F841
# 修改后:
entity_type = target_node.properties.get("entity_type", "interactionhabit")  # noqa: F841
```

注意区分：line 1503 和 1531 的 `"Other"` 是 entity_name 的默认值，不是 entity_type，不需要修改。

- [ ] **Step 5: 语法检查**

Run: `python -m py_compile niu_api/internal/region_manager.py niu_api/internal/lightrag_adapter.py`
Expected: 无输出（编译通过）

- [ ] **Step 6: 提交**

```bash
git add niu_api/internal/region_manager.py niu_api/internal/lightrag_adapter.py
git commit -m "fix: lowercase REGION_ENTITY_TYPE, Other defaults, and add MIN_COMMUNITY_SIZE check"
```

---

### Task 5: 验证

- [ ] **Step 1: 派审查Agent检查所有修改**

审查要点：
1. LightRAG fork 写入侧是否所有 entity_type 和 keywords 都经过 .lower()
2. 查询侧是否所有比较都经过 .lower()
3. dict 查找是否都经过 .lower()
4. LLM 提示词是否统一小写
5. 没有"硬编码具体类型字符串去逐个匹配"的错误模式
6. kg_api.py 的 "Document" 比较是否改为 .lower()

- [ ] **Step 2: 修复审查发现的问题（如有）**

- [ ] **Step 3: 最终提交**

```bash
git add -A
git commit -m "fix: address review findings for case normalization"
```

---

## 后续清理（不在本次范围内）

以下文件包含大写 entity_type 值，但因为未使用或已通过写入侧 .lower() 间接覆盖，不在本次修改范围：

1. **`niu_api/internal/brain_graph.py`**:
   - `ENTITY_TYPES` 集合（line 26）— 未使用，可后续改为小写或删除
   - `make_entity_name()` 的 `entity_type == "Niu"` 比较（line 86）— 死代码，`_infer_entity_type()` 从不返回 "Niu"
   - `_infer_entity_type()` 返回大写值（line 293-302）— 通过 `inject_custom_kg` → `ainsert_custom_kg` 传递，写入侧已加 .lower()

2. **`niu_api/internal/lightrag_adapter.py`** 的 `ENTITY_TYPES` 集合（line 39-44）— 未使用，可后续改为小写以与 `CUSTOM_ENTITY_TYPES` 保持一致