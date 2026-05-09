# KG 开发字典

> 基于 2026-05-09 实测结果更新。LLM 代理可用，通过 API 代理 /llm/v1 端点调用真实 LLM。
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

## 2. 照片入库（结构化注入）

### `lightrag_insert_custom_kg` — 照片+人物+关系一次性注入

```python
inject_custom_kg(
    entities=[
        {"entity_name": "photo:{normalized_path}", "entity_type": "Photo",
         "description": "{abstract}", "file_path": "{file_path}"},
        # 人物实体：用 LLM 自然格式（人名），不用 person:{uuid}
        {"entity_name": "{person_name}", "entity_type": "person",
         "description": "{person_name}，出现在照片{file_path}中"},
        # 未命名人物：用临时名字
        # {"entity_name": "未命名人物_{n}", "entity_type": "person",
        #  "description": "一个尚未命名的人物"},
    ],
    relationships=[
        {"src_id": "photo:{normalized_path}", "tgt_id": "{person_name}",
         "keywords": "features", "description": "照片中出现了{person_name}"},
        {"src_id": "brain:Niu", "tgt_id": "{person_name}",
         "keywords": "remembers", "description": "认识{person_name}"},
        {"src_id": "brain:Niu", "tgt_id": "photo:{normalized_path}",
         "keywords": "remembers", "description": "拥有这张照片"},
        # 多人同框:
        {"src_id": "{name_a}", "tgt_id": "{name_b}",
         "keywords": "co_occurs_with", "description": "{name_a}和{name_b}同框出现"},
    ],
    chunks=[],  # 无 chunks → 不触发 LLM → 100%可靠
    source_id="photo:{normalized_path}",
)
```

```
参数:  entities: list[dict]       — 见上方模板
       relationships: list[dict]  — 见上方模板
       chunks: list[dict]         — 见上方模板
       source_id: str = "custom_kg"
返回:  {"status": "ok", "entities": N, "relationships": N, "chunks": N}
注意:  无 chunks 时不触发 LLM，100% 可靠
       有 chunks 时会触发 LLM 提取，但 LLM 失败后实体/关系仍写入
陷阱:  keywords 是必需字段（LightRAG 直接访问 rel["keywords"]，无 fallback）
       file_path 默认 "custom_kg"，照片实体必须显式设置
       source_id 默认 "custom_kg"，不设会产生 "UNKNOWN source_id" 警告
       多次 inject 同名实体会用 <SEP> 追加描述
       co_occurs_with 双向关系可能被 LLM 合并减少（实测 3人6条→2条）
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
    relationships=[{"src_id": "brain:Niu", "tgt_id": "任飞",
                    "keywords": "remembers", "description": "认识任飞"}],
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
返回:  str | None（only_need_context=True 返回上下文文本）
注意:  需要 LLM 可用（keywords 提取 + 可选的回答生成）
       实测：hybrid 模式查询"谁出现在海滩照片里？"返回 6846 字符上下文
陷阱:  LLM 不可用时返回 None 或空字符串
       返回 fail_response 文本时 adapter 会过滤为 ""
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
返回:  {"status": "ok", "data": [entity_dict]} | {"status": "no_results"}
注意:  内部调用 query_data(mode="local")
       entity_type 过滤是大小写不敏感的
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
       自动包含 brain:Niu → entity 锚定关系
       Person→remembers, Skill→skilled_in, Concept→knows_about, Tool→uses
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

## 7. 脑区管理

### `lightrag_insert_custom_kg` — 注入脑区实体+锚定关系

```python
# 确保 brain:Niu 存在（启动时幂等调用）
inject_custom_kg(
    entities=[{"entity_name": "brain:Niu", "entity_type": "Niu",
               "description": "Self entity — all memory relations start from here"}],
    relationships=[], chunks=[], source_id="brain",
)

# 注入脑区实体
inject_custom_kg(
    entities=[{"entity_name": "brain:{region_name}", "entity_type": "BrainRegion",
               "description": "{region_description}"}],
    relationships=[{"src_id": "brain:Niu", "tgt_id": "brain:{region_name}",
                    "keywords": "remembers", "description": "拥有脑区{region_name}"}],
    chunks=[], source_id="brain:{region_name}",
)
```

---

## 8. 内容提取入库

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

## 9. 文档管理

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

## 10. 脑区管理（Brain Region）

### `RegionActivationManager` — 脑区激活/衰减管理器

```python
from niu_api.internal.region_activation import RegionActivationManager, BrainRegionState

manager = RegionActivationManager()

# 初始化：从 BrainRegionInfo 列表创建状态
from niu_api.internal.region_manager import BrainRegionInfo
regions = [
    BrainRegionInfo(name="brain:region:xxx", label="标签", community_id="c1",
                    description="描述", size=5, representative="代表实体",
                    members=["实体1", "实体2"], updated_at=0.0),
]
manager.initialize_from_regions(regions)

# 激活脑区（通过命中实体 → 映射到脑区）
hit_entities = ["Python"]
entity_to_region = {"Python": "brain:region:编程"}
activated = manager.activate_regions(hit_entities, entity_to_region)
# 返回: set[str] — 被激活的 region_id 集合

# 工具使用强化
region_id = manager.reinforce_by_tool_use("lightrag_insert", {"lightrag_insert": "brain:region:编程"})
# 返回: str | None — 被强化的 region_id

# 手动激活/调暗
manager.manual_activate(["brain:region:编程"])   # region_labels: list[str]
manager.manual_dim(["brain:region:编程"])         # region_labels: list[str]

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
members = manager.get_region_members("brain:region:编程")  # → list[str]

# 创建脑区节点（在图谱中创建 brain:region:xxx 实体 + belongs_to 边）
manager.create_region_nodes(regions)

# 更新脑区摘要
manager.update_region_summaries(regions)

# 清理过时脑区（成员 < 2 的脑区）
manager.cleanup_stale_regions(regions)

# 解散萎缩脑区
manager.dissolve_shrunk_regions(regions)

# 增量更新（Leiden 社区检测后）
manager.incremental_update(old_regions, new_regions)

# 边权重衰减（_region: 和 _session: 前缀的边）
disconnected = manager._decay_structural_edges(regions)
# 返回: int — 断开的边数

# 创建默认脑区（3个：聊天历史、文档库、知识体系）
manager.create_default_regions()
```

```
参数:  __init__(adapter: LightRAGAdapter, ingester: LightRAGIngester)
       get_all_regions() → list[BrainRegionInfo]
       get_region_members(region_name: str) → list[str]
       create_region_nodes(regions: list[BrainRegionInfo])
       update_region_summaries(regions: list[BrainRegionInfo])
       cleanup_stale_regions(regions: list[BrainRegionInfo])
       dissolve_shrunk_regions(regions: list[BrainRegionInfo])
       incremental_update(old: list, new: list)
       _decay_structural_edges(regions: list[BrainRegionInfo]) → int
       create_default_regions()
返回:  见上方各方法
注意:  BELONGS_TO_RELATION = "_region:contains"（旧版: "belongs_to"）
       _decay_structural_edges: decay_factor=0.5, threshold=0.1
       _summarize_region: 启发式（非 LLM）— 用第一个实体名做 label
       BrainRegionInfo: name, label, community_id, description, size, representative, members, updated_at
陷阱:  构造函数需要 (adapter, ingester)，不是 (rag)
       incremental_update 未实现（pass）
       _summarize_region 是启发式，不是 LLM 生成
       _decay_structural_edges 只处理 _region: 和 _session: 前缀的边
```

### `RegionSync` — 脑区后台同步守护线程

```python
from agent.injector.region_sync import RegionSync

sync = RegionSync(sync_interval=86400)  # 默认24小时
sync.start()  # 启动后台守护线程
```

```
参数:  sync_interval: int = 86400  — 同步间隔（秒）
返回:  无（守护线程）
注意:  8步流程: LightRAG检查 → 社区检测 → 创建节点 → 清理过时 → 更新摘要
       → 刷新激活管理器 → 合并+解散 → 保存状态
       后台守护线程，polling readiness check
陷阱:  邻居映射为空 — spillover 激活不工作
       Leiden 社区检测需要 leidenalg 包（未在 requirements.txt 中）
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

# 注入脑区上下文
from niu_api.internal.lightrag_adapter import LightRAGAdapter
adapter = LightRAGAdapter()
augmented = inject_brain_region_context(messages, adapter)
# 返回: list[dict] — 增强后的 messages
```

```
参数:  inject_brain_region_context(messages: list[dict], adapter: LightRAGAdapter)
       is_lightrag_extraction_request(messages: list[dict]) → bool
返回:  增强后的 messages 列表
注意:  只对包含 "Knowledge Graph Specialist" 的 messages 生效
       静态提示: 脑区架构 + 命名约定（未命名人物临时命名 + 同名实体不重复创建）
       动态提示: 从图谱查询当前脑区（mode="local", only_need_context=True）
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
参数:  handle_brain_region_activate(region_labels: list[str])
       handle_brain_region_dim(region_labels: list[str])
       handle_brain_region_status() → dict
       reinforce_on_tool_use(tool_name: str, tool_to_region: dict[str,str])
返回:  各工具返回格式不同
注意:  reinforce_on_tool_use 调用 manager.reinforce_by_tool_use + _reinforce_edge_weight
       _reinforce_edge_weight: 对 _region: 前缀的边 weight += 0.1, max=1.0
陷阱:  _reinforce_edge_weight 的 delta=0.1 与 RegionActivationManager 的 tool_reinforce_value=0.85 不一致
       边默认 weight=1.0，reinforce 无可见效果（min(1.0, 1.0+0.1)=1.0）
```

---

## 实体命名规范

| 类型 | 格式 | 示例 | 说明 |
|------|------|------|------|
| 人物 | `{人名}` | `任飞` | **LLM 自然格式**，未命名时用 `未命名人物_{n}` |
| 照片 | `photo:{normalized_path}` | `photo:e:/photos/2024/beach.jpg` | 照片实体，路径归一化（正斜杠+小写），与人物实体通过 features 关系连接 |
| 脑区 | `brain:{name}` | `brain:Niu` | 脑区锚点 |
| 事件 | `event:{name}` | `event:beach_sunset` | 事件实体 |
| 交互习惯 | `habit:{type}:{tool}` | `habit:tool_dialect:kg-server` | 交互习惯 |
| 记忆 | `brain:{type}:{label}` | `brain:Preference:python` | 记忆实体 |

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
| `remembers` | brain:Niu→实体 | 认识/拥有/知道 |
| `co_occurs_with` | person→person | 同框出现 |
| `participated` | event→person | 参加了某事件 |
| `classmate` | person→person | 同学关系 |
| `skilled_in` | brain:Niu→Skill | 技能 |
| `knows_about` | brain:Niu→Concept | 知识 |
| `uses` | brain:Niu→Tool | 工具使用 |

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
| 15 | LLM 提取额外实体 | inject_custom_kg 带 chunks 时 LLM 会提取额外实体/关系并合并；显式数据优先 |
| 16 | reranker 未配置 WARNING | 不影响查询结果，但日志会有 WARNING |
| 17 | **边默认 weight=1.0** | LightRAG 创建的边 weight 默认 1.0，reinforce +0.1 后 min(1.0,1.1)=1.0 无变化 |
| 18 | **_reinforce_edge_weight delta 不一致** | brain_tools delta=0.1 vs RegionActivationManager tool_reinforce_value=0.85 |
| 19 | **spillover 激活不工作** | RegionSync 邻居映射为空，spillover_factor=0.3 从未生效 |
| 20 | ~~brain_region_prompt 用 person:{uuid}~~ | **已修复**：静态提示已简化为命名约定（未命名人物临时命名 + 同名实体不重复创建），不再强制 person:{uuid} 格式 |
| 21 | **incremental_update 未实现** | RegionManager.incremental_update() 是 pass |
| 22 | **_decay_structural_edges 从未运行** | 只处理 _region: 前缀边，但当前图中无此类边，返回 0 |
| 23 | **leidenalg 未在 requirements.txt** | 社区检测需要此包，但未声明依赖 |
| 24 | **_summarize_region 是启发式** | 用第一个实体名做 label，不是 LLM 生成 |
| 25 | **brain_region_prompt 只在提取请求时注入** | 普通对话不触发，只在 LightRAG ainsert 时注入 |

## 待测试项

| # | 测试内容 | 优先级 | 状态 |
|---|---------|--------|------|
| 1 | 照片实体 `photo:{normalized_path}` 格式，LLM 是否识别为照片？ainsert 包含文件路径的文本时 LLM 如何处理？ | 高 | ✅ 通过：ainsert 后没有创建重复实体 |
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

**结果**：两个未命名人物都成功合并为任飞，所有关系（features, remembers）正确迁移

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

**核心结论**：人物实体必须使用 LLM 自然格式（人名）作为 entity_name，才能与 ainsert 自动提取的实体正确合并。

| 格式 | LLM 识别 | ainsert 合并 | 结论 |
|------|---------|-------------|------|
| `person:{uuid}` | ❌ 不识别 | ❌ 创建独立实体 | **禁止使用** |
| `{人名}`（如"任飞"） | ✅ 识别 | ✅ 合并到已有实体 | **推荐使用** |
| `未命名人物_{n}` | ❌ 纯 ainsert 不提取 | ✅ inject 后 ainsert 不分裂 | **临时格式，需 amerge 改名** |

**生产代码修改方向**：
1. `sync_photo_to_kg`：人物实体用 `{人名}` 或 `未命名人物_{n}` 作为 entity_name，UUID 放在描述里
2. `name_person`：调用 `amerge_entities(["未命名人物_{n}"], "{新名字}")` 改名
3. `merge_persons`：调用 `amerge_entities(["{旧名}"], "{目标名}")` 合并
4. 禁止 `person:{uuid}` 格式进入图谱
