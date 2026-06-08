# KG 开发字典

> 基于 2026-05-21 实测结果更新。LLM 代理可用，通过 API 代理 /llm/v1 端点调用真实 LLM。
> 所有后续开发直接参考此字典，无需再做额外测试。

## 测试结果摘要

测试环境：生产 LightRAG 实例（`~/.niu/lightrag_storage/`），LLM 代理可用（deepseek-v3-2412 via 火山引擎 ark）。

### 核心发现（2026-05-09 实测）

| # | 发现 | 实测证据 |
|---|------|---------|
| 1 | **`person:{uuid}` 格式 LLM 不识别** | LLM 看到 `person:20196f76...` 不认为这是人名，遇到"任飞"会创建独立实体 |
| 2 | **人名作为 entity_name 可合并** | 用"任飞"作为 entity_name，ainsert 后 LLM 正确合并，不创建独立实体 |
| 3 | **UUID 不需要进图谱** | UUID 只是 photos.db 的标识；LLM 自己遇到 UUID 不会建实体；我们主动建 UUID 实体后 ainsert 也不破坏 |
| 4 | **文件路径 LLM 不建实体** | LLM 自己遇到文件路径不会建实体；我们主动建文件路径实体后 ainsert 也不破坏 |
| 5 | **"未命名人物_1" LLM 不识别为人物** | 纯 ainsert 时 LLM 不提取"未命名人物_1"为人物实体；但 inject_custom_kg 注入后 ainsert 不会创建独立变体 |
| 6 | **amerge_entities 可改名** | `amerge_entities(["未命名人物_1"], "任飞")` 成功，旧实体消失，新实体出现，描述保留 |
| 7 | **全路径做节点名 LLM 识别不了** | LLM 遇到 `photo:REDACTED_WIN_PATH/.../file.jpg` 无法识别为同一照片实体，创建碎片；改用短名 `photo:20090603_092316` 后可正确识别合并 |

### LightRAG 防重复实体机制

LightRAG 通过 **entity_name 的小写匹配** 防止重复：
1. LLM 提取实体时输出 `(entity_name, entity_type, description)`
2. 程序在已有图谱中查找同名实体（`entity_name.lower()`）
3. 同名 → 合并描述（调用 LLM 或直接 `<SEP>` 拼接）
4. 不同名 → 新建实体

**因此：entity_name 必须与 LLM 自然提取的格式一致，才能自动合并。**

---

## 1. 文档入库

### `lightrag_insert` — 非结构化文档入库（LLM 自动提取）

```
参数:  content: str             — 文档文本
       doc_id: str | None       — 去重 ID（可选）
       file_path: str | None    — 文件路径引用（可选）
返回:  {"status": "ok", "track_id": str} | {"status": "error", "message": str}
注意:  LightRAG 自动提取实体/关系/合并同名实体
       file_path 不设则产生 "unknown_source"
       实测：聊天记录精炼文档自动提取 12 实体 + 18 关系
陷阱:  LLM 不可用时直接报错，不会部分写入
```

### `lightrag_insert_file` — 文件入库（LightRAG 自己读文件）

```
参数:  file_path: str           — 文件路径（支持 DOCX/PDF/PPTX/XLSX/txt/md）
       doc_id: str | None       — 去重 ID（可选）
返回:  {"status": "ok", "track_id": str} | {"status": "error", "message": str}
注意:  原文件不被修改/移动，使用临时副本
       入队后 file_path 会被 patch 回原始路径
陷阱:  文件不存在返回 error；必须让 LightRAG 自己读文件，禁止提取文本再传入
```

---

## 2. 照片入库（3步流程：结构化 + LLM 语义连接）

### 为什么需要 LLM 参与（ainsert）

**结构化注入的实体是"死"的** — 它们不会和图谱中已有的其他实体产生任何语义连接。
只有让 LLM 参与（ainsert），才能从照片描述文本中提取出与已有实体的关联（如地点、相机型号、活动等），建立真正的知识网络。

**因此照片入库必须走3步流程**，不能只用 custom_kg 绕过 LLM。

### 3步流程

```python
# Step 1: 结构化注入实体+关系（保证照片/人物实体精确存在 + features等边精确）
# 关键：entities/relationships/chunks 必须在同一个 custom_kg 调用中传入
# 这样 chunk_to_source_map 才有映射，relationships 的 source_id 才不会变成 UNKNOWN
inject_custom_kg(
    entities=[
        {"entity_name": "photo:{normalized_stem}", "entity_type": "Photo",
         "description": "{abstract}", "file_path": "{file_path}",
         "source_id": "{normalized_path}"},
        {"entity_name": "{person_name_or_未命名人物_n}", "entity_type": "person",
         "description": "{person_name}，出现在照片{file_path}中"},
    ],
    relationships=[
        {"src_id": "photo:{normalized_stem}", "tgt_id": "{person_name}",
         "keywords": "features", "description": "照片中出现了{person_name}"},
        # 多人同框:
        {"src_id": "{name_a}", "tgt_id": "{name_b}",
         "keywords": "co_occurs_with", "description": "{name_a}和{name_b}同框出现"},
    ],
    chunks=[{
        "content": chunk_text,
        "source_id": "{normalized_path}",
        "file_path": "{normalized_path}",
    }],
    source_id="photo:{normalized_stem}",
)

# Step 2: ainsert 让 LLM 处理文本（建立语义连接）
# 关键：chunk_text 中必须明确引用 Step 1 的实体名，让 LLM 能识别并合并
lightrag_insert(
    content=chunk_text,  # 包含照片描述 + 实体名引用
    file_path="{normalized_path}",  # 避免 unknown_source
    doc_id=f"doc-{normalized_stem}",
)

# Step 3: 清理碎片实体（LLM 可能创建的额外实体）
# 如果 ainsert 产生了与 Step 1 实体名不同的碎片实体，用 merge_entities 合并
```

### chunk_text 构造要点

chunk_text 中**必须明确引用 Step 1 的实体名**，这样 LLM 在提取实体时能识别到已有实体并合并，而不是创建碎片实体：

```python
# 正确：明确引用实体名
chunk_text = (
    f"照片 {normalized_stem}：{abstract}\n"
    f"实体：{', '.join(entity_names)}\n"  # 明确列出所有实体名
    f"人物：{', '.join(person_names)}\n"
)

# 错误：只给摘要，不引用实体名 → LLM 可能提取出不同名字的碎片实体
chunk_text = abstract  # LLM 从"未命名人物_1合影"中可能提取出"合影"等无关实体
```

### 为什么不能只用 custom_kg（绕过 LLM）

| 方案 | 实体精确 | 语义连接 | 碎片化 | 结论 |
|------|---------|---------|--------|------|
| 只用 custom_kg + chunks | ✅ 精确 | ❌ 无连接（死实体） | ✅ 无碎片 | **错误方案** — 实体变成孤岛 |
| 3步流程（custom_kg + ainsert） | ✅ 精确 | ✅ LLM 建立连接 | ⚠️ 可能碎片 | **正确方案** — Step 3 清理碎片 |

### source_id 映射机制（重要）

LightRAG 的 `ainsert_custom_kg` 中，entity/relationship 的 source_id 通过 `chunk_to_source_map` 映射：

```
chunk_to_source_map[chunk.source_id] = chunk_id
entity.source_id = chunk_to_source_map.get(entity.source_id, "UNKNOWN")
```

**关键**：entities 和 relationships 必须与 chunks 在**同一次 custom_kg 调用**中传入，否则：
- 分开调用时，第二次调用 chunks=[] → chunk_to_source_map 为空 → source_id 变成 UNKNOWN
- UNKNOWN source_id 导致实体不可通过向量搜索检索

**陷阱**：不要把 Step 1 拆成"先注入实体，再注入关系"两次调用！必须一次性传入 entities + relationships + chunks。

```
参数:  entities: list[dict]       — 见上方模板
       relationships: list[dict]  — 见上方模板
       chunks: list[dict]         — custom_kg 的 chunks 参数（照片入库不用，走 ainsert）
       source_id: str = "custom_kg"
返回:  {"status": "ok", "entities": N, "relationships": N, "chunks": N}
注意:  无 chunks 时不触发 LLM，100% 可靠（Step 1/2 用）
       有 chunks 时会触发 LLM 提取，但 LLM 失败后实体/关系仍写入
       照片入库的语义连接由 Step 3 的 ainsert 提供，不是 custom_kg chunks
陷阱:  keywords 是必需字段（LightRAG 直接访问 rel["keywords"]，无 fallback）
       file_path 默认 "custom_kg"，照片实体必须显式设置
       source_id 默认 "custom_kg"，不设会产生 "UNKNOWN source_id" 警告
       多次 inject 同名实体会用 <SEP> 追加描述
       co_occurs_with 双向关系可能被 LLM 合并减少（实测 3人6条→2条）
       ainsert 可能产生碎片实体 — Step 4 用 merge_entities 清理
```

---

## 3. 人物命名

### 方案：amerge_entities 改名（推荐）

```python
# 用户给"未命名人物_1"命名为"任飞"
# 直接用 amerge_entities，一步完成：旧实体删除 + 新实体创建 + 边迁移
amerge_entities(
    source_entities=["未命名人物_1"],
    target_entity="任飞",
    target_entity_data={"description": "任飞，用户的朋友", "entity_type": "person"},
)
```

```
注意:  一步完成改名：旧实体消失，新实体出现，所有边迁移到新实体
       target_entity_data 可覆盖描述和类型
       实测：合并后 ainsert 包含"任飞"的文本，LLM 正确合并到已有实体
```

### 备选：inject_custom_kg 更新描述（不改名）

```python
inject_custom_kg(
    entities=[{"entity_name": "任飞", "entity_type": "person",
               "description": "任飞，用户的朋友"}],
    relationships=[{"src_id": "情感偏好脑区", "tgt_id": "任飞",
                    "keywords": "_region:contains", "description": "认识任飞"}],
    chunks=[],  # 无 chunks → 不触发 LLM → 100%可靠
    source_id="person:rename",
)
```

---

## 4. 人物合并

### `lightrag_merge_entities` — 合并实体+迁移边

```python
# 场景：发现两个 UUID 指向同一个人，需要合并
# 步骤1: 更新目标实体描述（inject_custom_kg, chunks=[]）
inject_custom_kg(
    entities=[{"entity_name": "{target_name}", "entity_type": "person",
               "description": "{merged_description}"}],
    relationships=[], chunks=[], source_id="merge:{target_name}",
)

# 步骤2: 合并实体（迁移边+删除旧实体）
merge_entities(
    source_entities=["{old_name}"],   # 只含被合并的，不含 target
    target_entity="{target_name}",
)
```

```
参数:  source_entities: list[str] — 被合并的实体名列表
       target_entity: str         — 合并目标实体名
返回:  {"status": "ok", "target_entity": str, "result": str}
注意:  source_entities 中的实体及其所有边迁移到 target，然后旧实体被删除
陷阱:  source_entities 不能包含 target_entity，否则报错
       合并前先用 inject_custom_kg 更新 target 描述，避免描述丢失
       旧版本 LightRAG 不支持会返回 AttributeError
```

---

## 5. 图谱查询

### `lightrag_query` — 文本查询

```
参数:  query: str                — 查询文本
       mode: str = "mix"         — naive|local|global|hybrid|mix|bypass
       only_need_context: bool = True  — True 返回上下文，False 让 LLM 生成回答
       top_k: int = 5            — 检索数量
       response_type: str = "Multiple Paragraphs"
       keywords: list[str] | None — 预提供关键词，跳过 LLM 提取（近即时返回）
返回:  str | None（only_need_context=True 返回上下文文本）
注意:  需要 LLM 可用（keywords 提取 + 可选的回答生成）
       提供 keywords 时跳过 LLM 提取，近即时返回
       实测：hybrid 模式查询"谁出现在海滩照片里？"返回 6846 字符上下文
陷阱:  LLM 不可用时返回 None 或空字符串
       返回 fail_response 文本时 adapter 会过滤为 ""
       不提供 keywords 时 LightRAG 需调 LLM 提取关键词（5-30秒），LLM 不可用或返回格式不合规时查询失败
       Agent 自身即为大模型，应自行提取关键词传入 keywords 参数，避免 LightRAG 重复调用 LLM
       reranker 未配置时会有 WARNING 但不影响结果
```

### `lightrag_query_data` — 结构化数据查询

```
参数:  query: str                — 查询文本
       mode: str = "local"       — 推荐 local（实体聚焦）
       keywords: list[str] | None — 预提供关键词，跳过 LLM 提取
       top_k: int = 10
返回:  dict | None — {data: {entities, relationships, chunks}}
注意:  提供 keywords 时跳过 LLM 提取，近即时返回
       不提供 keywords 时 LLM 提取需 5-30s
陷阱:  无结果返回 {"status": "no_results"}，不是空 dict
```

### `lightrag_search_entities` — 实体搜索

```
参数:  query: str                — 搜索文本
       entity_type: str = ""     — 过滤类型（空=不过滤）
       top_k: int = 10
       keywords: list[str] | None — 预提供关键词，跳过 LLM 提取（近即时返回）
返回:  {"status": "ok", "data": [entity_dict]} | {"status": "no_results"}
注意:  内部调用 query_data(mode="local")
       entity_type 过滤是大小写不敏感的
       提供 keywords 时跳过 LLM 提取，近即时返回；推荐 Agent 自行提取关键词传入
```

### `lightrag_get_graph` — 子图探索

```
参数:  action: str = "explore"   — explore|snapshot
       entity_name: str = ""     — explore 必需
       depth: int = 2            — BFS 深度（1-5）
       limit: int = 200          — snapshot 专用
       edge_types: list[str] | None — 边类型过滤
返回:  {"center": {...}, "nodes": [...], "edges": [...], "stats": {...}}
       node: {id, name, type, description, file_path, source_id}
       edge: {source, target, relation, description, weight}
注意:  edge 的键名是 "relation"（不是 "keywords"）
       depth 被限制在 1-5 范围
       snapshot 调用 get_graph_snapshot(limit)
```

### `lightrag_timeline_query` — 时间线查询

```
参数:  query: str = ""
       start_entities: list[str] | None
       direction: str = "backward" — backward|forward
       max_depth: int = 2
       top_k: int = 5
       max_results: int = 10
返回:  {"status": "ok", "timeline": [...]} | {"status": "error"}
注意:  向量匹配后遍历时间链关系
```

---

## 6. 实体增删

### `lightrag_insert_entity` — 单实体插入（走 ainsert）

```
参数:  name: str, entity_type: str, description: str = ""
       source_id: str = "custom_kg"（已废弃）, file_path: str = "custom_kg"
       skip_llm_extraction: bool = False（已废弃）
返回:  {"status": "ok", "track_id": str} | {"status": "error"}
注意:  内部构造文本调用 lightrag_insert（ainsert），自动提取实体/关系
       自动包含 Niu → entity 锚定关系
       Person→brain_region_anchor, Skill→brain_region_anchor, Concept→brain_region_anchor, Tool→brain_region_anchor
陷阱:  走 LLM 提取，不适合精确控制；照片/人物应使用 inject_custom_kg
```

### `lightrag_insert_relation` — 单关系插入（走 ainsert）

```
参数:  src_id: str, tgt_id: str, relation: str, description: str = ""
       source_id: str = "custom_kg"（已废弃）, file_path: str = "custom_kg"
返回:  {"status": "ok", "track_id": str} | {"status": "error"}
注意:  内部构造文本 "语义关系: {src_id} —[{relation}]→ {tgt_id}" 调用 ainsert
陷阱:  走 LLM 提取，不适合精确控制
```

### `lightrag_delete_entity` — 删除实体+所有关系

```
参数:  entity_name: str
返回:  {"status": "ok", "entity_name": str, "result": str} | {"status": "error"}
注意:  调用 adelete_by_entity，级联删除该实体的所有边
```

### `lightrag_list_entities` — 列出实体/文档/标签

```
参数:  list_type: str = "entities" — entities|documents|labels
       entity_type: str = ""       — 按 entity_type 过滤（仅 entities 模式）
       limit: int = 50
返回:  {"status": "ok", "data": [...]} | {"status": "error"}
       entities: [{id, entity_type, description}]
       documents: processed 文档列表
       labels: 图标签列表
陷阱:  返回的实体键名是 "id"，不是 "entity_name"，需手动映射
       entity_type 过滤是大小写不敏感的（源码 .lower() 比较）
       无过滤时用 get_knowledge_graph("*")，limit 控制节点数
```

---

## 7. 实体/关系编辑与精确查询

### `lightrag_edit_entity` — 编辑实体

```
参数:  entity_name: str          — 实体名（必填）
       description: str          — 新描述（覆盖式）
       entity_type: str          — 新类型
       new_name: str             — 新实体名（需 allow_rename=True）
       allow_rename: bool        — 允许改名（默认 False）
       allow_merge: bool         — 允许合并到已存在实体（默认 False）
返回:  {"status": "ok", "message": str, "data": dict}
注意:  allow_rename 有风险，改名后可能影响已有关系。allow_merge=True 时，如果 new_name 已存在，会合并两个实体。
示例:  disk("/lightrag/lightrag_edit_entity 'Python' --description '一种编程语言'")
       disk("/lightrag/lightrag_edit_entity '旧名' --new-name '新名' --allow-rename true")
```

### `lightrag_edit_relation` — 编辑关系

```
参数:  source_entity: str        — 源实体名（必填）
       target_entity: str        — 目标实体名（必填）
       keywords: str             — 当前关键词（用于定位关系）
       new_keywords: str         — 新关键词
       new_description: str      — 新描述
       new_weight: float         — 新权重
返回:  {"status": "ok", "message": str, "data": dict}
注意:  关系是无向的，source/target 顺序不影响结果
示例:  disk("/lightrag/lightrag_edit_relation 'Niu' 'Python' --new_description 'Niu精通Python'")
```

### `lightrag_delete_relation` — 删除关系

```
参数:  source_entity: str        — 源实体名（必填）
       target_entity: str        — 目标实体名（必填）
       keywords: str             — 关系关键词（可选，不指定则删除两实体间所有关系）
返回:  {"status": "ok", "message": str}
注意:  只删关系，不删实体
示例:  disk("/lightrag/lightrag_delete_relation 'Niu' 'Python'")
```

### `lightrag_get_entity_info` — 查询实体详情

```
参数:  entity_name: str          — 实体名（必填）
       include_vector_data: bool — 包含向量数据（默认 False）
返回:  {"status": "ok", "data": {"entity_name": str, "source_id": str, "graph_data": dict}}
注意:  graph_data 包含 description、entity_type 等属性
示例:  disk("/lightrag/lightrag_get_entity_info 'Python'")
```

### `lightrag_get_relation_info` — 查询关系详情

```
参数:  source_entity: str        — 源实体名（必填）
       target_entity: str        — 目标实体名（必填）
       include_vector_data: bool — 包含向量数据（默认 False）
返回:  {"status": "ok", "data": {"src_entity": str, "tgt_entity": str, "graph_data": dict}}
注意:  关系是无向的，source/target 顺序不影响结果
示例:  disk("/lightrag/lightrag_get_relation_info 'Niu' 'Python'")
```

### `lightrag_create_entity` — 创建实体（严格模式）

```
参数:  entity_name: str          — 实体名（必填，必须唯一）
       entity_type: str          — 实体类型（必填）
       description: str          — 描述
       source_id: str            — 来源 chunk ID
       file_path: str            — 文件路径引用
返回:  {"status": "ok", "message": str, "data": dict}
注意:  实体已存在则失败（返回 skipped=True）。与 lightrag_insert_entity 的区别：insert 是 upsert，create 是严格新建
示例:  disk("/lightrag/lightrag_create_entity '新概念' --type 'Concept' --description '描述'")
```

### `lightrag_create_relation` — 创建关系（严格模式）

```
参数:  source_entity: str        — 源实体名（必填）
       target_entity: str        — 目标实体名（必填）
       keywords: str             — 关系关键词（必填）
       description: str          — 描述
       weight: float             — 权重（默认 1.0）
       source_id: str            — 来源 chunk ID
       file_path: str            — 文件路径引用
返回:  {"status": "ok", "message": str, "data": dict}
注意:  任一实体不存在则失败。关系已存在则失败（返回 skipped=True）
示例:  disk("/lightrag/lightrag_create_relation 'Niu' 'Python' --keywords 'skilled_in'")
```

---

## 8. 脑区管理

### `lightrag_insert_custom_kg` — 注入脑区实体+锚定关系

```python
# 确保 Niu 存在（启动时幂等调用）
inject_custom_kg(
    entities=[{"entity_name": "Niu", "entity_type": "Niu",
               "description": "Self entity — all memory relations start from here"}],
    relationships=[], chunks=[], source_id="brain",
)

# 注入脑区实体（自然语言命名）
inject_custom_kg(
    entities=[{"entity_name": "{label}脑区", "entity_type": "BrainRegion",
               "description": "{region_description}"}],
    relationships=[{"src_id": "Niu", "tgt_id": "{label}脑区",
                    "keywords": "brain_region_anchor", "description": "拥有脑区{label}"}],
    chunks=[], source_id="brain",
)
```

---

## 9. 内容提取入库

### `lightrag_insert` — 聊天记录精炼文档入库

```
参数:  content: str, doc_id: str | None, file_path: str | None
返回:  {"status": "ok", "track_id": str} | {"status": "error"}
注意:  LLM 自动提取实体/关系/合并同名实体
       适合自然语言内容，如聊天记录精炼后的文档
```

### `lightrag_insert_custom_kg` — 精加工（事件/关系补充）

```
用于补充 LLM 自动提取遗漏的结构化信息：
- 补充事件实体和参与关系
- 补充脑区锚定关系
- 补充 co_occurs_with 等关系
- chunks=[] 确保 100% 可靠
```

---

## 10. 文档管理

### `lightrag_document_status` — 文档处理状态

```
参数:  无
返回:  {pending: N, processing: N, processed: N, failed: N}
```

### `lightrag_get_document` — 获取文档内容

```
参数:  doc_id: str
返回:  {"status": "ok", "doc_id": str, "content": str, "doc_status": str}
       | {"status": "not_found", "doc_id": str}
```

### `lightrag_delete_document` — 级联删除

```
参数:  doc_id: str
返回:  {"status": "ok", "doc_id": str, "result": str} | {"status": "error"}
注意:  调用 adelete_by_doc_id，级联删除文档+chunks+entities+relationships
```

---

## 11. 脑区管理（Brain Region）

### `RegionActivationManager` — 脑区激活/衰减管理器

```python
from niu_api.internal.region_activation import RegionActivationManager, BrainRegionState

manager = RegionActivationManager()

# 初始化：从 BrainRegionInfo 列表创建状态
from niu_api.internal.region_manager import BrainRegionInfo
regions = [
    BrainRegionInfo(name="编程开发脑区", label="编程开发", community_id="c1",
                    description="描述", size=5, representative="代表实体",
                    members=["实体1", "实体2"], updated_at=0.0),
]
manager.initialize_from_regions(regions)

# 激活脑区（通过命中实体 → 映射到脑区）
hit_entities = ["Python"]
entity_to_region = {"Python": "community_3"}
activated = manager.activate_regions(hit_entities, entity_to_region)
# 返回: set[str] — 被激活的 region_id 集合

# 工具使用强化
region_id = manager.reinforce_by_tool_use("lightrag_insert", {"lightrag_insert": "community_3"})
# 返回: str | None — 被强化的 region_id

# 手动激活/调暗
manager.manual_activate(["编程开发"])   # region_labels: list[str]
manager.manual_dim(["编程开发"])         # region_labels: list[str]

# 衰减（每轮调用一次）
manager.decay_all()  # factor=0.92, threshold=0.1

# 查询
region_map = manager.get_region_map()     # → list[BrainRegionState]
active = manager.get_active_regions()     # → list[BrainRegionState] (activation > 0.3)
light = manager.get_status_light(0.85)    # → "🟢" | "🟡" | "⚫"
```

```
参数:  initialize_from_regions(regions: list[BrainRegionInfo])
       activate_regions(hit_entities: list[str], entity_to_region: dict[str,str])
       reinforce_by_tool_use(tool_name: str, tool_to_region: dict[str,str])
       manual_activate(region_labels: list[str])
       manual_dim(region_labels: list[str])
       decay_all()
       get_region_map() → list[BrainRegionState]
       get_active_regions() → list[BrainRegionState]
       get_status_light(activation: float) → str
返回:  见上方各方法
注意:  线程安全（threading.RLock）
       衰减因子=0.92, 激活阈值=0.3, spillover_factor=0.3
       tool_reinforce_value=0.85
       co-activation 追踪（用于合并候选）
陷阱:  activate_regions 的第一个参数是 hit_entities（实体名），不是 region_labels
       get_status_light 接受 activation float 值，不是 region_id
       initialize_from_regions 需要 BrainRegionInfo（不是 BrainRegionState）
```

### `RegionManager` — 脑区图谱管理器

```python
from niu_api.internal.region_manager import RegionManager, BrainRegionInfo
from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester

adapter = LightRAGAdapter()
ingester = LightRAGIngester()
manager = RegionManager(adapter, ingester)

# 获取所有脑区
regions = manager.get_all_regions()  # → list[BrainRegionInfo]

# 获取脑区成员
members = manager.get_region_members("编程开发脑区")  # → list[str]

# 创建脑区节点（在图谱中创建 {label}脑区 实体 + _region:contains 边）
manager.create_region_nodes(partition_result)

# 更新脑区摘要
manager.update_region_summaries(region_names)

# 清理过时脑区
manager.cleanup_stale_regions(current_partition)

# 解散萎缩脑区
manager.dissolve_shrunk_regions()

# 增量更新（社区检测 + 创建节点 + 清理 + 更新摘要 + 衰减边）
result = manager.incremental_update()

# 边权重衰减（_region: 和 _session: 前缀的边）
disconnected = manager._decay_structural_edges(regions)
# 返回: int — 断开的边数

# 创建默认脑区（6个：Core 3 + Category 3）
from niu_api.internal.region_manager import create_default_regions
result = create_default_regions(adapter, ingester)
```

```
参数:  __init__(adapter: LightRAGAdapter, ingester: LightRAGIngester)
       get_all_regions() → list[BrainRegionInfo]
       get_region_members(region_name: str) → list[str]
       create_region_nodes(partition_result: CommunityDetectionResult)
       update_region_summaries(region_names: list[str])
       cleanup_stale_regions(current_partition: CommunityDetectionResult)
       dissolve_shrunk_regions(shrink_threshold: int = 100, shrink_rounds: int = 3)
       incremental_update() → dict  # 已完整实现
       _decay_structural_edges(regions: list[BrainRegionInfo]) → int
返回:  见上方各方法
注意:  BELONGS_TO_RELATION = "_region:contains"（旧版: "belongs_to"）
       _decay_structural_edges: decay_factor=0.5, threshold=0.1
       _summarize_region: 启发式（非 LLM）— 用第一个实体名做 label
       BrainRegionInfo: name, label, community_id, description, size, representative, members, updated_at
       create_default_regions 是模块级函数（不是 RegionManager 方法）
       签名: create_default_regions(adapter, ingester, include_category=True)
       创建6个默认脑区：Core(3)+Category(3)
陷阱:  构造函数需要 (adapter, ingester)，不是 (rag)
       incremental_update 已完整实现（不是 pass）
       _summarize_region 是启发式，不是 LLM 生成
       _decay_structural_edges 只处理 _region: 和 _session: 前缀的边
```

### `RegionSync` — 脑区后台同步守护线程

```python
from agent.injector.region_sync import RegionSync

sync = RegionSync(sync_interval=86400)  # 默认24小时
sync.start_background_sync()  # 启动后台守护线程
```

```
参数:  sync_interval: int = 86400  — 同步间隔（秒）
返回:  无（守护线程）
注意:  8步流程: LightRAG检查 → 社区检测 → 创建节点 → 清理过时 → 更新摘要
       → 刷新激活管理器 → 合并+解散 → 保存状态
       后台守护线程，polling readiness check
陷阱:  Leiden 社区检测需要 leidenalg 包（未在 requirements.txt 中）
```

### `brain_region_prompt` — 脑区上下文注入

```python
from niu_api.internal.brain_region_prompt import inject_brain_region_context, is_lightrag_extraction_request

# 检查是否为 LightRAG 提取请求
messages = [
    {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
    {"role": "user", "content": "提取实体"},
]
assert is_lightrag_extraction_request(messages)  # True

# 注入脑区上下文（单参数，内部通过 get_brain_regions() 读取 NetworkX 内存图）
augmented = inject_brain_region_context(messages)
# 返回: list[dict] — 增强后的 messages
```

```
参数:  inject_brain_region_context(messages: list[dict])
       is_lightrag_extraction_request(messages: list[dict]) → bool
返回:  增强后的 messages 列表
注意:  只对包含 "Knowledge Graph Specialist" 的 messages 生效
       静态提示: 脑区架构 + 命名约定（未命名人物临时命名 + 同名实体不重复创建）
       动态提示: 直接读 NetworkX 内存图 via get_brain_regions()，避免事件循环死锁
       注入内容长度约 69000 字符
陷阱:  只在 LightRAG 提取请求时注入，普通对话不触发
```

### `brain_tools` — 脑区 MCP 工具

```python
from agent.brain_tools import (
    handle_brain_region_activate,   # 手动激活脑区
    handle_brain_region_dim,        # 手动调暗脑区
    handle_brain_region_status,     # 获取脑区状态
    reinforce_on_tool_use,          # 工具使用时强化脑区
)
```

```
参数:  handle_brain_region_activate(regions: list[str], reason: str = "")
       handle_brain_region_dim(regions: list[str])
       handle_brain_region_status(include_dark: bool = False) → str
       reinforce_on_tool_use(tool_name: str, reinforce_delta: float = 0.15)
返回:  各工具返回格式不同
注意:  reinforce_on_tool_use 调用 manager.reinforce_by_tool_use + _reinforce_edge_weight
       _reinforce_edge_weight: 对 _region: 前缀的边 weight += 0.15, max=2.0
陷阱:  边初始 weight=0.5，reinforce delta=0.15，max=2.0（REINFORCE_DELTA=0.15, MAX_EDGE_WEIGHT=2.0）
```

---

## 实体命名规范

| 类型 | 格式 | 示例 | 说明 |
|------|------|------|------|
| 人物 | `{人名}` | `任飞` | **LLM 自然格式**，未命名时用 `未命名人物_{n}` |
| 照片 | `photo:{normalized_stem}` | `photo:20090603_092316` | 照片实体，短名=文件名stem（不含扩展名），file_path放metadata存完整路径，与人物实体通过 features 关系连接 |
| 脑区 | `{label}脑区` | `聊天历史脑区` | 自然语言命名，label为可读名称 |
| 自身 | `Niu` | `Niu` | 根节点，脑区锚点 |
| 事件 | `{name}事件` | `海滩日落事件` | 自然语言命名，禁止 `event:` 前缀 |
| 交互习惯 | `habit:{type}:{tool}` | `habit:tool_dialect:kg-server` | 交互习惯 |
| 记忆 | 自然语言 | `Python偏好` | 记忆实体，自然语言命名 |

### 人物实体命名规则（重要）

- **已命名人物**：entity_name = 人名（如"任飞"），与 LLM 自然提取格式一致
- **未命名人物**：entity_name = `未命名人物_{n}`（如"未命名人物_1"），这是临时名
- **用户命名时**：调用 `amerge_entities(["未命名人物_1"], "任飞")` 改名
- **UUID 不进图谱**：UUID 只是 photos.db 的标识，图谱中不需要
- **禁止 `person:{uuid}` 格式**：LLM 不认识这种格式，会导致实体分裂

## 关系类型规范

| 关键词 | 方向 | 语义 |
|--------|------|------|
| `features` | photo→person | 照片中出现了某人 |
| `brain_region_anchor` | Niu→脑区 | Niu 拥有脑区（Niu 只与脑区连接，不与普通实体连接） |
| `co_occurs_with` | person→person | 同框出现 |
| `participated` | event→person | 参加了某事件 |
| `classmate` | person→person | 同学关系 |
| `_region:contains` | 脑区→成员 | 脑区包含实体 |
| `brain_region_anchor` | Niu→脑区 | Niu 拥有脑区 |

## 已知陷阱速查

| # | 陷阱 | 规避 |
|---|------|------|
| 1 | **`person:{uuid}` 格式 LLM 不识别** | 人物实体必须用人名作为 entity_name，禁止 `person:{uuid}` 格式 |
| 2 | **UUID 不需要进图谱** | UUID 只在 photos.db 中维护，图谱中不需要 |
| 3 | **"未命名人物_1" LLM 不识别为人物** | 纯 ainsert 时 LLM 不提取为人物实体；需通过 inject_custom_kg 注入 |
| 4 | chunks 触发 LLM 提取 | 无 chunks 时不触发 LLM；有 chunks 时 LLM 失败仍写入实体/关系 |
| 5 | merge_entities source 含 target | source_entities 只放被合并实体 |
| 6 | 同名实体描述追加 `<SEP>` | 语义上是追加而非替换；如需替换需先 delete 再 inject |
| 7 | keywords 必需 | relationships 中每个 dict 必须有 keywords 键 |
| 8 | file_path 默认 "custom_kg" | 照片实体必须显式设 file_path，否则前端无缩略图 |
| 9 | source_id 默认 "custom_kg" | 不设会产生 "UNKNOWN source_id" 警告 |
| 10 | list_entities 键名是 "id" | 不是 "entity_name"，需手动映射 |
| 11 | explore_node edge 键名是 "relation" | 不是 "keywords" |
| 12 | insert_entity 走 ainsert | 触发 LLM 提取，不适合精确控制；照片/人物用 inject_custom_kg |
| 13 | lightrag_insert 产生 "unknown_source" | 必须传 file_path 参数 |
| 14 | co_occurs_with 双向边被 LLM 合并 | 3人同框理论6条双向边，LLM 可能合并为2条；显式注入更可靠 |
| 15 | LLM 提取额外实体 | ainsert 可能产生碎片实体（与 Step 1 实体名不同），Step 4 用 merge_entities 清理 |
| 16 | **只用 custom_kg 绕过 LLM = 死实体** | 结构化注入的实体不会和已有实体产生语义连接，必须走3步流程（custom_kg + ainsert） |
| 17 | **ainsert 碎片化根因** | LLM 从 chunk_text 提取的实体名与 Step 1 不同 → 无法合并 → 碎片；解决：chunk_text 中明确引用 Step 1 实体名 + Step 3 清理 |
| 18 | **全路径做节点名 LLM 识别不了** | 照片实体名用短名 `photo:{stem}`（如 `photo:20090603_092316`），不用全路径 |
| 19 | **custom_kg 分开调用导致 source_id UNKNOWN** | entities/relationships/chunks 必须在同一次 custom_kg 调用中传入；分开调用时第二次 chunks=[] → chunk_to_source_map 为空 → source_id=UNKNOWN |
| 20 | reranker 未配置 WARNING | 不影响查询结果，但日志会有 WARNING |
| 21 | **边初始 weight=0.5** | LightRAG 创建的边 weight 默认 0.5，reinforce +0.15 后 min(2.0, 0.5+0.15)=0.65 有变化 |
| 22 | **_reinforce_edge_weight delta=0.15** | brain_tools REINFORCE_DELTA=0.15, MAX_EDGE_WEIGHT=2.0（旧值 delta=0.1, max=1.0 已修正） |
| 23 | ~~spillover 激活不工作~~ | **已修复**：BUG 3 fix 实现了 build_neighbor_map()，spillover_factor=0.3 现在生效 |
| 24 | ~~brain_region_prompt 用 person:{uuid}~~ | **已修复**：静态提示已简化为命名约定（未命名人物临时命名 + 同名实体不重复创建），不再强制 person:{uuid} 格式 |
| 25 | ~~incremental_update 未实现~~ | **已修复**：RegionManager.incremental_update() 已完整实现（社区检测 + 创建节点 + 清理 + 更新摘要 + 衰减边） |
| 26 | **_decay_structural_edges 从未运行** | 只处理 _region: 前缀边，但当前图中无此类边，返回 0 |
| 27 | **leidenalg 未在 requirements.txt** | 社区检测需要此包，但未声明依赖 |
| 28 | **_summarize_region 是启发式** | 用第一个实体名做 label，不是 LLM 生成 |
| 29 | **brain_region_prompt 只在提取请求时注入** | 普通对话不触发，只在 LightRAG ainsert 时注入 |
| 30 | **`lightrag_create_entity` 实体已存在会失败** | 先用 `lightrag_get_entity_info` 检查，或直接用 `lightrag_insert_entity`（upsert 模式） |
| 31 | **`lightrag_create_relation` 关系已存在会失败** | 先用 `lightrag_get_relation_info` 检查，或直接用 `lightrag_insert_relation`（upsert 模式） |
| 32 | **`lightrag_edit_entity` 改名可能破坏关系** | 谨慎使用 `allow_rename=True`，改名后检查相关关系 |
| 33 | **`lightrag_delete_relation` 不指定 keywords 会删除所有关系** | 如需精确删除，务必指定 keywords 参数 |

## 待测试项

| # | 测试内容 | 优先级 | 状态 |
|---|---------|--------|------|
| 1 | 照片实体 `photo:{normalized_stem}` 短名格式，LLM 是否识别为照片？ainsert 包含短名的文本时 LLM 如何处理？ | 高 | ✅ 通过：ainsert 后没有创建重复实体，短名比全路径更易识别 |
| 2 | 提示词注入：在 brain_region_prompt 中明确告诉 LLM "未命名人物_X 是人物实体的临时名字"，LLM 能否识别？ | 高 | ✅ 通过：inject_custom_kg 注入后 ainsert 合并到已有实体，无独立变体 |
| 3 | 人物改名后，照片实体与人物实体的 features 关系是否正确迁移？ | 高 | ✅ 通过：amerge_entities 后所有关系正确迁移，包括 features |
| 4 | 多张照片包含同一人物，人物实体是否保持唯一？ | 中 | ✅ 通过：两次 inject_custom_kg 同名人物 + ainsert，实体始终唯一，描述用 `<SEP>` 追加 |
| 5 | 两个未命名人物合并为一个（发现是同一人），amerge_entities 是否正确？ | 中 | ✅ 通过：amerge_entities(["未命名人物_2"], "任飞") 成功，旧实体消失，边迁移正确 |
| 6 | ainsert 长文档（多次提到同一人名）时，人物实体是否无重复？ | 低 | ✅ 通过：长文档多次提到"任飞"，LLM 正确合并到已有实体，无独立变体 |

---

## 测试详情（2026-05-09 实测）

### 测试4：多张照片包含同一人物 — 实体唯一性

**步骤**：
1. inject_custom_kg: photo1 + 任飞 → 任飞实体创建
2. inject_custom_kg: photo2 + 任飞（同名） → 描述追加 `<SEP>`，实体唯一
3. ainsert 包含"任飞"的新文本 → LLM 合并到已有实体，无独立变体

**结果**：任飞实体始终唯一（1个），两张照片实体独立存在，关系正确

### 测试5：两个未命名人物合并为一个

**步骤**：
1. inject_custom_kg: photo1 + 未命名人物_1 → 创建
2. inject_custom_kg: photo2 + 未命名人物_2 → 创建
3. amerge_entities(["未命名人物_1"], "任飞") → 未命名人物_1消失，任飞出现，边迁移
4. amerge_entities(["未命名人物_2"], "任飞") → 未命名人物_2消失，边迁移到任飞

**结果**：两个未命名人物都成功合并为任飞，所有关系（features, brain_region_anchor）正确迁移

### 测试6：ainsert 长文档人物去重

**步骤**：
1. inject_custom_kg: 任飞实体（描述："任飞，用户的朋友，喜欢摄影"）
2. ainsert 长文本（旅行日记，6次提到"任飞"）
3. 检查：任飞实体是否分裂

**结果**：任飞实体唯一，LLM 正确合并。描述追加 `<SEP>` + LLM 合并后的新描述：
`"任飞，用户的朋友，喜欢摄影<SEP>真实姓名为任飞，对应人物ID为20196f76-adfb-49ca-8f99-4402fb84b1d5，是摄影爱好者，参与了2024年夏天的西柏坡之旅"`

**注意**：LLM 在合并描述时，从 brain_region_prompt 注入的上下文中提取了 UUID 信息。这证明提示词注入有效。

---

## 全部测试结论

**核心结论**：照片入库必须走3步流程（结构化注入 + LLM 语义连接 + 碎片清理），不能只用 custom_kg 绕过 LLM。

**为什么需要 LLM**：结构化注入的实体是"死"的，不会和图谱中已有实体产生语义连接。只有 ainsert 让 LLM 处理文本，才能从照片描述中提取出与已有实体（地点、相机、活动等）的关联，建立真正的知识网络。

**碎片化对策**：ainsert 可能产生与 Step 1 实体名不同的碎片实体。解决方法：
1. chunk_text 中明确引用 Step 1 的实体名，让 LLM 能识别并合并
2. Step 3 用 merge_entities 清理残留碎片实体

| 格式 | LLM 识别 | ainsert 合并 | 结论 |
|------|---------|-------------|------|
| `person:{uuid}` | ❌ 不识别 | ❌ 创建独立实体 | **禁止使用** |
| `photo:{full_path}` | ❌ 不识别（路径太长含特殊字符） | ❌ 创建碎片实体 | **禁止使用** |
| `photo:{stem}`（如 `photo:20090603_092316`） | ✅ 识别为照片 | ✅ 合并到已有实体 | **推荐使用** |
| `{人名}`（如"任飞"） | ✅ 识别 | ✅ 合并到已有实体 | **推荐使用** |
| `未命名人物_{n}` | ❌ 纯 ainsert 不提取 | ✅ inject 后 ainsert 不分裂 | **临时格式，需 amerge 改名** |

| 方案 | 实体精确 | 语义连接 | 碎片化 | 结论 |
|------|---------|---------|--------|------|
| 只用 custom_kg（绕过 LLM） | ✅ 精确 | ❌ 无连接（死实体） | ✅ 无碎片 | **错误** — 实体变成孤岛 |
| 3步流程（custom_kg + ainsert + 清理） | ✅ 精确 | ✅ LLM 建立连接 | ⚠️ 可能碎片（可清理） | **正确** |

**生产代码修改方向**：
1. `sync_photo_to_kg`：3步流程 — Step1 用 custom_kg 一次性注入实体+关系+chunks（保证 source_id 映射），Step2 用 ainsert（chunk_text 引用短名实体名），Step3 清理碎片；照片实体名用短名 `photo:{stem}`，file_path 仍存完整路径
2. `name_person`：调用 `amerge_entities(["未命名人物_{n}"], "{新名字}")` 改名
3. `merge_persons`：调用 `amerge_entities(["{旧名}"], "{目标名}")` 合并
4. 禁止 `person:{uuid}` 格式进入图谱
5. ainsert 必须传 file_path 参数，避免 unknown_source
