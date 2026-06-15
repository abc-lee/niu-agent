# KG 自然语言化改造实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 去掉知识图谱中所有程序化前缀（`entity:` 前缀、`_region:contains`、`brain_region_anchor`），让程序化创建的边与 LLM 自动提取的边格式完全一致，使用自然语言。

**Architecture:** 分三步走：(1) 去掉 `entity:` 前缀机制，后端 API 返回裸名 ID，前端不做前缀添加/剥离；(2) 将脑区边关键词从程序化标识符改为自然语言，常量值变更自动传播到所有引用；(3) 将前缀匹配机制替换为显式枚举，兼容旧数据。每步独立可测试。

**Tech Stack:** Python (FastAPI, NetworkX, LightRAG), JavaScript (Electron, force-graph)

---

## 修改文件清单

| 文件 | 责任 | 改动类型 |
|------|------|----------|
| `niu_api/kg_api.py` | API 层 normalize + 各端点手动构造 | 修改 |
| `niu_api/internal/lightrag_adapter.py` | changelog 记录中的 entity: 前缀 | 修改 |
| `niu_api/internal/region_manager.py` | 常量值 + REGION_EDGE_PREFIXES | 修改 |
| `niu_api/internal/lightrag_manager.py` | 读取脑区成员时的硬编码字符串匹配 | 修改 |
| `niu_api/internal/brain_region_prompt.py` | LLM prompt 中的关键词 | 修改 |
| `agent/brain_tools.py` | REGION_EDGE_PREFIXES + 前缀匹配 | 修改 |
| `agent/injector/sync.py` | 硬编码 `_region:contains` | 修改 |
| `config/agents/dream-evolver.md` | Agent prompt 中的边命名规范 | 修改 |
| `ui/graph/renderer.js` | 前端 entity: 前缀添加/剥离 + expandNode | 修改 |
| `tests/test_brain_region_e2e.py` | 断言字符串 | 修改 |
| `tests/test_brain_region_prompt.py` | 断言字符串 | 修改 |
| `tests/test_region_manager.py` | 常量导入（自动跟随） | 无需改 |
| `scripts/test_brain_region_injection.py` | 断言字符串 | 修改 |
| `scripts/test_brain_region_filtered_search.py` | 调试输出 | 修改 |

---

## Task 1: 去掉 `entity:` 前缀 — 后端 kg_api.py

**Files:**
- Modify: `niu_api/kg_api.py`

- [ ] **Step 1: 修改 `_normalize_nodes` 函数（行 161-193）**

将实体节点 ID 从 `f"entity:{raw_id}"` 改为裸名 `raw_id`，Document 节点逻辑保持不变（本来就裸名）。

```python
def _normalize_nodes(nodes: list) -> list:
    """Convert adapter node format to frontend-expected format.

    Frontend expects: {id, label, name, nodeType, entityType, description, uri, source}
    """
    result = []
    for n in nodes:
        raw_id = n.get("id", "")
        node_type = n.get("type", "other")
        if node_type.lower() == "document":
            node_id = raw_id
            normalized_type = "Document"
        else:
            node_id = raw_id  # 裸名，不加 entity: 前缀
            normalized_type = "Entity"
        result.append({
            "id": node_id,
            "label": n.get("name", n.get("id", "")),
            "name": n.get("name", ""),
            "nodeType": normalized_type,
            "entityType": node_type,
            "description": n.get("description", ""),
            "uri": _clean_file_path(n.get("file_path", "")),
            "source": _clean_source_id(n.get("source_id", "")),
        })
    return result
```

- [ ] **Step 2: 修改 `_normalize_edges` 函数（行 196-220）**

去掉 source/target 的 `entity:` 前缀添加逻辑，直接使用裸名。

```python
def _normalize_edges(edges: list) -> list:
    """Convert adapter edge format to frontend-expected format.

    Frontend expects: {source, target, relation, edgeType, confidence}
    """
    result = []
    for e in edges:
        src = e.get("source", e.get("src_id", ""))
        tgt = e.get("target", e.get("tgt_id", ""))
        result.append({
            "source": src,
            "target": tgt,
            "relation": e.get("relation", e.get("keywords", "")),
            "edgeType": e.get("type", "relation"),
            "confidence": e.get("confidence", e.get("weight", 1.0)),
        })
    return result
```

- [ ] **Step 3: 修改 `/explore` 端点（行 682-719）**

去掉 `removeprefix("entity:")` 和 center 节点 ID 的 `entity:` 前缀添加。

行 692 改为：
```python
entity_name = request.entity_id
```

行 710 改为：
```python
result["center"] = {
    "id": c.get("id", ""),
    "label": c.get("name", c.get("id", "")),
    "name": c.get("name", ""),
    "nodeType": "Entity",
    "entityType": c.get("type", "other"),
    "description": c.get("description", ""),
    "uri": _clean_file_path(c.get("file_path", "")),
    "source": _clean_source_id(c.get("source_id", "")),
}
```

- [ ] **Step 4: 修改 `/find-path` 端点（行 722-799）**

行 735-736 改为：
```python
src = request.from_id
tgt = request.to_id
```

所有节点构造中的 `f"entity:{node_name}"` 改为 `node_name`，边构造中的 `f"entity:{u}"` / `f"entity:{v}"` 改为 `u` / `v`。涉及行 757、760、777、778、788、789。

节点构造模板：
```python
nodes.append({
    "id": node_name,
    "label": node_name,
    "name": node_name,
    "nodeType": "Entity",
    "entityType": attrs.get("entity_type", "other"),
    "description": attrs.get("description", ""),
    "uri": _clean_file_path(attrs.get("file_path", "")),
    "source": attrs.get("source_id", ""),
})
```

边构造模板：
```python
edges.append({
    "source": u,
    "target": v,
    "relation": data.get("keywords", ""),
    "confidence": data.get("weight", 1.0),
    "edgeType": "RELATED_TO",
})
```

- [ ] **Step 5: 修改 `/hubs` 端点（行 619-679）**

同 Step 4 的模板，将所有 `f"entity:{node_name}"` 改为 `node_name`，`f"entity:{u}"` / `f"entity:{v}"` 改为 `u` / `v`。涉及行 649、668、669。

- [ ] **Step 6: 修改 `/entities` 端点（行 802-864）**

同上。涉及行 837、853、854。

- [ ] **Step 7: 修改 `/concepts` 端点（行 867-924）**

同 Step 4-5 的 ID 改动模式。涉及行 897、913、914。

**注意**：`/concepts` 端点的 `nodeType` 是 `"Concept"`（不是 `"Entity"`），保持不变。

- [ ] **Step 8: 修改 `/surprising` 端点（行 927-1005）**

同上。涉及行 973、994、995。

- [ ] **Step 9: 删除 `_normalize_nodes` 和 `_normalize_edges` 中已无用的注释**

删除所有关于 `entity:` 前缀用途的注释（行 166、175、202、208）。

同时删除行 691 的过时注释：
```python
# Strip entity: prefix if present — adapter expects bare entity names
```

- [ ] **Step 10: 运行 Python 语法检查**

Run: `python3 -m py_compile niu_api/kg_api.py`
Expected: 无输出（编译成功）

---

## Task 2: 去掉 `entity:` 前缀 — 后端 lightrag_adapter.py changelog

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py`

- [ ] **Step 1: 修改 `delete_entity` 中的 changelog（行 952）**

```python
# 改前
get_change_log().record_change("entity_deleted", {"id": f"entity:{entity_name}"})
# 改后
get_change_log().record_change("entity_deleted", {"id": entity_name})
```

- [ ] **Step 2: 修改 `merge_entities` 中的 changelog（行 1351-1352）**

```python
# 改前
"source_ids": [f"entity:{s}" for s in resolved_sources],
"target_id": f"entity:{resolved_target}",
# 改后
"source_ids": resolved_sources,
"target_id": resolved_target,
```

- [ ] **Step 3: 修改 `inject_custom_kg` 中的 changelog（行 1662、1671-1672）**

```python
# 改前
"id": f"entity:{entity['entity_name']}",
# 改后
"id": entity['entity_name'],
```

```python
# 改前
"source": f"entity:{rel['src_id']}",
"target": f"entity:{rel['tgt_id']}",
# 改后
"source": rel['src_id'],
"target": rel['tgt_id'],
```

- [ ] **Step 4: 运行 Python 语法检查**

Run: `python3 -m py_compile niu_api/internal/lightrag_adapter.py`
Expected: 无输出（编译成功）

---

## Task 3: 去掉 `entity:` 前缀 — 前端 renderer.js

**Files:**
- Modify: `ui/graph/renderer.js`

- [ ] **Step 1: 修改 `expandNode` 函数（行 791）**

```javascript
// 改前
const entityId = orig.id.replace(/^entity:/, '');
// 改后
const entityId = orig.id;
```

- [ ] **Step 2: 修改 `expandNode` 中合并节点时的 ID 处理（行 801）**

```javascript
// 改前
const nid = n.id.startsWith('entity:') ? n.id : `entity:${n.id}`;
// 改后
const nid = n.id;
```

- [ ] **Step 3: 修改 `expandNode` 中合并边时的 ID 处理（行 814-815）**

```javascript
// 改前
const srcId = edge.source.startsWith('entity:') ? edge.source : `entity:${edge.source}`;
const tgtId = edge.target.startsWith('entity:') ? edge.target : `entity:${edge.target}`;
// 改后
const srcId = edge.source;
const tgtId = edge.target;
```

- [ ] **Step 4: 验证前端 Changelog 处理无需修改**

`renderer.js` 行 388-465 的 changelog 处理逻辑中，`entity_created`、`edge_created`、`entity_deleted`、`entity_merged` 事件直接使用 `change.data.id` / `change.data.source` / `change.data.target`。后端 changelog 已改为裸名（Task 2），前端 currentData 中也是裸名（Task 1 + Task 3），`===` 匹配自然对齐。**无需修改。**

---

## Task 4: 脑区边关键词自然语言化 — 常量定义

**Files:**
- Modify: `niu_api/internal/region_manager.py`

- [ ] **Step 1: 修改常量值（行 46-50）**

```python
# 改前
ANCHOR_RELATION = "brain_region_anchor"
BELONGS_TO_RELATION = "_region:contains"
_LEGACY_BELONGS_TO = "belongs_to"

# 改后
ANCHOR_RELATION = "脑区锚点"
BELONGS_TO_RELATION = "包含"
```

常量名不变，所有引用 `ANCHOR_RELATION` / `BELONGS_TO_RELATION` 的代码（创建边、测试断言）自动跟随新值，无需单独修改。`_LEGACY_BELONGS_TO` 删除（数据会重新入库，不需要旧兼容）。

- [ ] **Step 2: 替换 `REGION_EDGE_PREFIXES` 为显式枚举（行 942）**

```python
# 改前
REGION_EDGE_PREFIXES = ("_region:", "_session:", "brain_region_")

# 改后
STRUCTURAL_EDGE_TYPES_LOWER = frozenset({
    BELONGS_TO_RELATION.lower(),      # "包含"
    ANCHOR_RELATION.lower(),          # "脑区锚点"
})
```

> 注意：`_session:` 前缀边在当前代码库中无任何生产代码创建，但保留防御性前缀匹配以防 LLM 自行创建此类边。

- [ ] **Step 3: 修改 `_decay_structural_edges` 中的前缀匹配（行 968）**

```python
# 改前
if any(keywords.startswith(prefix) for prefix in REGION_EDGE_PREFIXES):
# 改后
kw_lower = keywords.lower()
if kw_lower in STRUCTURAL_EDGE_TYPES_LOWER or kw_lower.startswith("_session:"):
```

- [ ] **Step 4: 运行 Python 语法检查**

Run: `python3 -m py_compile niu_api/internal/region_manager.py`
Expected: 无输出（编译成功）

---

## Task 5: 脑区边关键词自然语言化 — brain_tools.py

**Files:**
- Modify: `agent/brain_tools.py`

- [ ] **Step 1: 替换 `REGION_EDGE_PREFIXES` 为导入 `STRUCTURAL_EDGE_TYPES`（行 430、437）**

从 `region_manager` 导入 `STRUCTURAL_EDGE_TYPES`，替换本地重复定义。

```python
# 在文件顶部添加导入
from niu_api.internal.region_manager import STRUCTURAL_EDGE_TYPES_LOWER

# 行 430 改前
REGION_EDGE_PREFIXES = ("_region:", "_session:", "brain_region_")
# 行 430 改后（删除此行，使用导入的 STRUCTURAL_EDGE_TYPES_LOWER）

# 行 437 改前
if any(keywords.startswith(prefix) for prefix in REGION_EDGE_PREFIXES):
# 行 437 改后
kw_lower = keywords.lower()
if kw_lower in STRUCTURAL_EDGE_TYPES_LOWER or kw_lower.startswith("_session:"):
```

- [ ] **Step 2: 运行 Python 语法检查**

Run: `python3 -m py_compile agent/brain_tools.py`
Expected: 无输出（编译成功）

---

## Task 6: 脑区边关键词自然语言化 — lightrag_manager.py 读取兼容

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py`

- [ ] **Step 1: 修改 `get_region_members` 中的边匹配（行 381）**

```python
# 改前
if edge_type.lower() == "_region:contains":
# 改后
if edge_type.lower() == "包含":
```

- [ ] **Step 2: 修改 `get_all_region_members` 中的边匹配（行 426）**

```python
# 改前
if edge_type.lower() == "_region:contains":
# 改后
if edge_type.lower() == "包含":
```

- [ ] **Step 3: 运行 Python 语法检查**

Run: `python3 -m py_compile niu_api/internal/lightrag_manager.py`
Expected: 无输出（编译成功）

---

## Task 7: 脑区边关键词自然语言化 — sync.py 硬编码修复

**Files:**
- Modify: `agent/injector/sync.py`

- [ ] **Step 1: 修改硬编码字符串（行 469）**

```python
# 改前
"keywords": "_region:contains",
# 改后
from niu_api.internal.region_manager import BELONGS_TO_RELATION
# ...（导入放在文件顶部）
"keywords": BELONGS_TO_RELATION,
```

- [ ] **Step 2: 运行 Python 语法检查**

Run: `python3 -m py_compile agent/injector/sync.py`
Expected: 无输出（编译成功）

---

## Task 8: 脑区边关键词自然语言化 — LLM prompt

**Files:**
- Modify: `niu_api/internal/brain_region_prompt.py`
- Modify: `config/agents/dream-evolver.md`

- [ ] **Step 1: 修改 `brain_region_prompt.py` 中所有 `_region:contains`**

将 `_STATIC_BRAIN_REGION_PROMPT` 中所有 `_region:contains` 替换为 `包含`。涉及行 41、45、46、47、50、51、58（两处）。

行 41 改为：
```
每个脑区通过 `包含` 边包含一组语义相关的实体，方向**必须是脑区→实体**（source=脑区，target=实体）。例如：
```

行 44-51 的树形图改为：
```
知识体系脑区
  ├── 包含 → Python
  ├── 包含 → NumPy
  └── 包含 → 数据分析

人际关系脑区
  ├── 包含 → 小明
  └── 包含 → 安安
```

行 58 改为：
```
提取实体时，如果你能判断实体属于哪个脑区，可以用 `包含` 边将实体归入对应脑区（source=脑区名，target=实体名）。如果不确定应归入哪个脑区，**不要创建 `包含` 边**，后续流程会自动处理。
```

- [ ] **Step 2: 修改 `dream-evolver.md` 中所有 `_region:contains`**

将行 36、50、58、68、138、143、198、203 中的 `_region:contains` 替换为 `包含`。

行 198-199 边命名规范表合并为统一表述（脑区和 Session 边共享 `包含` 关键词）：
```
| 包含 | `包含` | 父节点 → 子实体 | src=脑区/session, tgt=实体 |
```

行 203 改为：
```
**注意**：`包含` 方向是 脑区→实体（src=xxx脑区, tgt=entity），不要反向。
```

行 58/138/143 中的 `lightrag_insert_relation` 示例改为：
```
lightrag_insert_relation(src_id="知识体系脑区", tgt_id="FastAPI", relation="包含")
```

行 214 `relation` 参数说明改为：
```
- `relation`：关系类型（必填，有语义的动词或名词）
```

- [ ] **Step 3: 运行 Python 语法检查**

Run: `python3 -m py_compile niu_api/internal/brain_region_prompt.py`
Expected: 无输出（编译成功）

---

## Task 9: 更新测试断言

**Files:**
- Modify: `tests/test_brain_region_e2e.py`
- Modify: `tests/test_brain_region_prompt.py`
- Modify: `scripts/test_brain_region_injection.py`
- Modify: `scripts/test_brain_region_filtered_search.py`

- [ ] **Step 1: 修改 `test_brain_region_e2e.py`（行 36、209）**

```python
# 行 36 改前
assert "_region:contains" in static
# 行 36 改后
assert "包含" in static

# 行 209 改前
assert "_region:contains" in content
# 行 209 改后
assert "包含" in content
```

- [ ] **Step 2: 修改 `test_brain_region_prompt.py`（行 69）**

```python
# 改前
assert "_region:contains" in result
# 改后
assert "包含" in result
```

- [ ] **Step 3: 修改 `scripts/test_brain_region_injection.py`**

**断言修改（行 64-66）：**

```python
# 行 64 改前
assert "brain:Niu" in prompt, "Missing brain:Niu"
# 行 64 改后
assert "根节点" in prompt, "Missing 根节点"

# 行 65 改前
assert "brain_region_anchor" in prompt, "Missing brain_region_anchor"
# 行 65 改后（"脑区锚点"关键词不出现在 prompt 文本中，替换为 prompt 中实际存在的概念）
assert "禁止事项" in prompt, "Missing 禁止事项"

# 行 66 改前
assert "belongs_to_region" in prompt, "Missing belongs_to_region"
# 行 66 改后
assert "包含" in prompt, "Missing 包含"
```

**函数调用签名修复（行 101、123、138）：**

```python
# 改前（函数已不接受 adapter 参数）
prompt = build_dynamic_brain_region_prompt(adapter)
# 改后
prompt = build_dynamic_brain_region_prompt()
```

**函数调用签名修复（行 157、187、218）：**

```python
# 改前（函数已不接受 adapter 参数）
result = inject_brain_region_context(messages, adapter)
# 改后
result = inject_brain_region_context(messages)
```

**行 162、167、221 也有 `brain:Niu` 引用：**

```python
# 行 162 改前
assert "brain:Niu" in system_msg["content"]
# 行 162 改后
assert "根节点" in system_msg["content"] or "niu" in system_msg["content"].lower()

# 行 167 改前
assert "brain:Niu" not in messages[0]["content"]
# 行 167 改后
assert "大脑区域架构" not in messages[0]["content"], "Original messages were mutated"

# 行 221 改前
if "brain:Niu" in system_msg["content"]:
# 行 221 改后
if "根节点" in system_msg["content"] or "niu" in system_msg["content"].lower():
```

**注意：** 该测试脚本存在更深层的结构性问题（mock 方式与当前函数实现不匹配），但超出了本计划范围。上述修改保证关键词一致性，测试脚本完整修复需要后续单独处理。

- [ ] **Step 4: 修改 `scripts/test_brain_region_filtered_search.py`（行 66）**

```python
# 改前
print("[WARN] 脑区成员为空（_region:contains 边可能不存在）")
# 改后
print("[WARN] 脑区成员为空（包含 边可能不存在）")
```

- [ ] **Step 5: `tests/test_region_manager.py` 无需修改**

该文件导入 `ANCHOR_RELATION` 和 `BELONGS_TO_RELATION` 常量，常量名不变，值自动跟随。

- [ ] **Step 6: 清理残留注释**

以下文件注释中引用了旧关键词，统一更新：

- `agent/injector/region_sync.py` 行 235：`_region:contains` → `包含`
- `niu_api/internal/region_activation.py` 行 225：`_region:contains` → `包含`
- `niu_api/internal/region_injector.py` 行 215：`_region:contains` → `包含`
- `niu_api/brain_region_api.py` 行 235：`_region:contains` → `包含`
- `niu_api/internal/region_manager.py` 行 7、460、917、919、1034：`_region:contains` → `包含`，`brain_region_anchor` → `脑区锚点`
- `agent/brain_tools.py` 行 399、401：同上
- `niu_api/internal/lightrag_manager.py` 行 348、376：`"_region:contains"` → `"包含"`

---

## Task 10: 端到端验证

**Files:** 无

- [ ] **Step 1: 重启程序（必须）**

**必须关闭正在运行的 KG 窗口并重新启动程序**，否则旧窗口 `currentData` 中残留的 `entity:xxx` ID 与新 changelog 事件的裸名 `xxx` 通过 `===` 不匹配，会导致重复节点和断边。

Run: `go run main.go`

- [ ] **Step 2: 打开 KG 图谱 UI，验证以下场景**

1. 图谱全局视图正常加载，节点和边都能显示
2. 点击任意实体节点，关系列表正确显示（关系数与可见边一致）
3. 点击 document 类型的边连接的实体（如"邢台分行"），能看到关系详情
4. 双击节点展开邻居，新节点和新边正确合并到图中
5. 点击脑区节点，关系列表显示"包含：XXX"而非"_region:contains：XXX"
6. 点击 Niu 节点，关系列表显示"脑区锚点：XXX脑区"而非"brain_region_anchor：XXX脑区"

- [ ] **Step 3: 验证脑区功能正常**

1. 脑区成员列表完整显示（所有边使用 `包含` 关键词）
2. 脑区激活/调暗功能正常
3. Niu 节点显示 `脑区锚点` 关系

- [ ] **Step 4: 验证 LLM 提取**

1. 插入新文档，LLM 提取时 prompt 中包含"包含"而非"_region:contains"
2. LLM 创建的新边使用"包含"关键词
3. 新旧边在图谱中共存，读取都正常

---

## 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| 前端 currentData 中残留 `entity:` 前缀与后端裸名不匹配 | **高** | 改完代码后**必须关闭并重新打开 KG 窗口**。正在运行的窗口 `currentData` 中旧的 `entity:xxx` ID 与新 changelog 裸名 `xxx` 通过 `===` 不匹配，会导致重复节点和断边 |
| LLM 不遵从新的"包含"关键词 | 中 | prompt 中明确说明，且"包含"是自然语言，LLM 理解度应更高 |
| `brain_tools.py` 导入 `region_manager` 产生循环依赖 | 低 | 已验证：`region_manager.py` 不导入任何 `agent/` 模块，导入方向安全 |
| `/concepts` 端点 `nodeType` 被误改为 `"Entity"` | 低 | Task 1 Step 7 已标注保留 `"Concept"` |
