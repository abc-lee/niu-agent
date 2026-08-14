# LightRAG 知识检索运维手册

> 本文档从 SYSTEM_MANUAL.md 拆分而来，说明 LightRAG 知识检索的架构、数据结构、检索模式和运维操作。

## 一、架构概述

LightRAG 统一了知识图谱 + 语义检索，取代了旧的 vector-store + kg-server 双存储架构。

| 项目 | 说明 |
|------|------|
| 存储位置 | `~/.niu/lightrag_storage/`（固定路径，不依赖 workspace.path） |
| 核心文件 | `graph_chunk_entity_relation.graphml`（知识图谱）、`vdb_*.json`（向量索引）、`kv_store_*.json`（文档存储） |
| LLM 调用 | 通过 `/llm/v1/` 代理路由到用户配置的模型（LiteLLM -> user-config.json） |
| Embedding 调用 | 直接 Python 函数调用（`niu_api.internal.embedding`），零 HTTP 开销 |
| Reranker | 直接 Python 函数调用（`niu_api.internal.reranker`），可选 |
| 默认向量模型 | `bge-base-zh-v1.5`（768 维，512 tokens，中文优化） |

**LightRAG 实例管理**（`niu_api.internal.lightrag_manager`）：
- 懒初始化：首次 `get_lightrag()` 调用时创建
- 独立守护线程运行 asyncio 事件循环，`call_async()` 桥接同步调用
- 初始化失败时记录时间戳，60 秒内不重试（避免重试风暴）
- `wait_lightrag_ready()` 供其他线程等待初始化完成

**自定义实体类型约束**（`lightrag_manager._create_lightrag_instance`）：

```python
CUSTOM_ENTITY_TYPES = [
    "person", "organization", "technology", "concept",
    "location", "event", "document", "photo", "video",
    "note", "chat", "skill", "tool", "knowledge",
    "interactionhabit", "episodicevent", "brainregion", "other",
]
```

LLM 提取实体时被约束为上述类型，确保前端分类按钮与图谱数据一致。不匹配的实体归为 "other"。

**大小写统一规范**：所有 entity_type 和 keywords 在写入图谱时统一 `.lower()` 存储，查询时 `.lower()` 比较。此规范消除了大小写不一致导致的重复实体和 Counter 投票分裂问题。

## 二、LightRAG 数据结构

### 2.1 实体（entity）

| 字段 | 说明 |
|------|------|
| entity_name | 实体名（自然语言，如 "Python"、"任飞"、"影像记忆脑区"） |
| entity_type | 实体类型（见第三章） |
| description | 实体描述（向量匹配的主要字段） |
| source_id | 来源标识 |

### 2.2 关系（relationship）

| 字段 | 说明 |
|------|------|
| src_id | 源实体名 |
| tgt_id | 目标实体名 |
| keywords | 关系类型（如 "skilled_in"、"包含"） |
| description | 关系描述 |
| weight | 边权重 |

**方向语义**：LightRAG 图为无向图——`src_id`/`tgt_id` 的前后顺序仅为存储排序，**不代表方向**。关系方向语义存在于 `description` 文本中（如 "李磊 属于 人际关系脑区"），模型解读关系方向以 description 为准，不要依据 src/tgt 顺序判断"谁指向谁"。查询工具（query/query_data/get_graph/timeline_query/get_relation_info）返回的关系同理。

### 2.3 文档块（chunk）

| 字段 | 说明 |
|------|------|
| id | 块 ID |
| full_doc_id | 所属文档 ID |
| chunk_order_index | 块在文档中的顺序 |
| content | 块文本内容 |

### 2.4 向量索引

NanoVectorDB 格式，存储在 `~/.niu/lightrag_storage/` 下：

| 文件 | 内容 |
|------|------|
| `vdb_chunks.json` | 文档块的向量索引 |
| `vdb_entities.json` | 实体描述的向量索引 |
| `vdb_relationships.json` | 关系描述的向量索引 |

## 三、实体类型（entity_type）

| 类型 | 说明 | 创建来源 |
|------|------|----------|
| skill | Skills 文件 | injector/sync.py 同步 |
| tool | MCP 工具描述 | injector/lightrag_sync.py 同步（当前已禁用，工具走 disk 模式发现） |
| person | 人物 | photo-server 照片入库时创建 |
| concept | 概念/知识实体 | 文档入库时 LightRAG 自动提取 |
| photo | 照片摘要 | photo-server 照片入库时创建 |
| brainregion | 脑区节点 | injector/region_sync.py 定期运行 Leiden 社区检测生成 |
| interactionhabit | 交互习惯 | 类型已在 `CUSTOM_ENTITY_TYPES` 声明，当前无主动创建路径，预留给交互习惯分析 |
| document | 文档 | 文档入库时 LightRAG 自动提取 |
| organization | 组织 | 文档入库时 LightRAG 自动提取 |
| technology | 技术 | 文档入库时 LightRAG 自动提取 |
| other | 其他 | LLM 无法归类时的兜底类型 |

**实体命名规范**：所有实体名使用自然语言（如 "Python"、"任飞"、"影像记忆脑区"），不使用冒号前缀格式（如 ~~"skill:Python"~~、~~"person:uuid"~~）。`_normalize_entity_name()` 保留为恒等函数做向后兼容。

**大小写规范**：写入路径与查询路径均做小写归一化，但归一化时机不同：
- **写入路径**：LightRAG fork 的所有写入入口（`ainsert_custom_kg`、`acreate_entity`、`acreate_relation`、`_edit_entity_impl`、`_merge_entities_impl`）在落库时对 entity_type 和 keywords 统一 `.lower()`。
- **查询路径**：adapter 层过滤（`lightrag_adapter.py:324` 的 `target_type = entity_type.lower().strip()`）在比较时归一化。调用方传入 title case（如 `filter_by_entity_type(result, "Skill")`、`"InteractionHabit"`）也能被正确匹配，不要求调用方预先小写化。
- 此规范消除了大小写不一致导致的重复实体和 Counter 投票分裂问题。

## 四、检索模式

| 模式 | 说明 | 典型用途 |
|------|------|----------|
| naive | 纯向量检索，最快最简单 | 无图数据场景 |
| local | 实体+关系局部检索 | 具体问题，默认推荐 |
| global | 全局社区检索 | 宏观问题 |
| hybrid | local + global 结合 | 兼顾细节与全局 |
| mix | 最全面：local + global + 向量 | 全面检索，最慢 |
| bypass | 跳过检索，仅 LLM | 不需要知识库时 |

**Keywords 优化**：提供 `keywords` 参数可跳过 LLM 关键词提取，将延迟从 5-30s 降至 <1s，同时保持完整图遍历能力。动态注入（`_inject_dynamic_resources`）始终使用 keywords 模式。

## 五、LightRAG MCP 工具（23 个）

`lightrag-server` 提供 23 个统一工具，分四组：

### 查询组（5 个）

| 工具 | 说明 |
|------|------|
| `lightrag_query` | 查询知识库，返回生成文本或原始上下文 |
| `lightrag_query_data` | 查询知识库，返回结构化数据（实体 + 关系 + chunks）。支持 keywords 参数跳过 LLM 提取 |
| `lightrag_search_entities` | 按实体类型搜索（skill, tool, knowledge, person, photo, concept 等） |
| `lightrag_get_graph` | 获取子图（explore: BFS 遍历 / snapshot: 全图快照） |
| `lightrag_timeline_query` | 时间线查询：向量匹配 -> 遍历时间链 -> 按时间戳排序 |

时间链由系统在 nap/sleep/force 管道收尾自动补全：会话实体（`YYYY-MM-DD会话`）按日期用 `followed_by` 相连，形成按天索引链；当天无重要内容时不建实体（链允许缺口）。用户提"之前/后来/某天发生了什么"时可用 timeline_query 或定位会话实体展开当天内容。

### 插入组（5 个）

| 工具 | 说明 |
|------|------|
| `lightrag_insert` | 插入文档，LightRAG 自动提取实体和关系 |
| `lightrag_insert_file` | 按文件路径插入，LightRAG 读取并解析文件（支持 DOCX/PDF/PPTX/XLSX/TXT/MD 等），异步处理 |
| `lightrag_insert_custom_kg` | 直接注入结构化知识（实体 + 关系 + chunks），跳过 LLM 提取。用于 Skills、工具、照片名等需精确控制的数据 |
| `lightrag_insert_entity` | 插入单个实体（通过 inject_custom_kg），自动创建 Niu -> 实体锚点关系 |
| `lightrag_insert_relation` | 插入实体间关系（通过 inject_custom_kg）。**若源/目标实体不存在会自动创建**（含 `YYYY-MM-DD会话` 日期节点）——无需预先查询存在性，直接建链 |

### 管理组（6 个）

| 工具 | 说明 |
|------|------|
| `lightrag_delete_entity` | 删除实体及其所有关系 |
| `lightrag_delete_document` | 级联删除文档及其关联的 chunks、entities、relationships |
| `lightrag_document_status` | 获取文档处理状态计数（pending/processing/processed/failed） |
| `lightrag_get_document` | 获取完整文档内容及处理状态 |
| `lightrag_list_entities` | 列出实体、文档或实体类型标签（list_type: entities/documents/labels） |
| `lightrag_merge_entities` | 合并多个实体，整合所有关系 |

### 编辑/详情组（7 个）

| 工具 | 说明 |
|------|------|
| `lightrag_edit_entity` | 编辑实体信息（描述、类型、重命名），支持合并到已有实体 |
| `lightrag_edit_relation` | 编辑关系信息（关键词、描述、权重） |
| `lightrag_delete_relation` | 删除两个实体间的关系，保留实体 |
| `lightrag_get_entity_info` | 获取单个实体详细信息（含图谱和向量数据） |
| `lightrag_get_relation_info` | 获取两个实体间关系的详细信息 |
| `lightrag_create_entity` | 创建新实体（已存在则失败，如需 upsert 用 lightrag_insert_entity） |
| `lightrag_create_relation` | 创建新关系（两端实体必须存在，已存在则失败） |

## 六、文档入库流程

```
用户拖入文件
    ↓
photo-server/ingest_document
    ↓
1. check_kg_supported(file_path)  ← 格式检查
    ↓ 支持
2. 文件搬运到 workspace 目录
    ↓
3. lightrag_insert_file(target_path)  ← 提交到 LightRAG
    ↓
4. pipeline_enqueue_file  ← 入队（PENDING 状态）
    ↓
5. apipeline_process_enqueue_documents  ← 异步处理（fire-and-forget）
    ├── 解析文件 → 分块
    ├── LLM 提取实体/关系
    └── 写入图谱和向量索引
```

**格式支持**（`KG_SUPPORTED_EXTENSIONS`）：

| 支持入库 | 不支持入库 |
|----------|-----------|
| `.txt` `.md` `.csv` `.json` `.log` | `.doc`（旧版 Word） |
| `.pdf` `.docx` `.pptx` `.xlsx` | `.xls` `.ppt`（旧版 Office） |
| `.html` `.htm` | WPS 创建的假 `.docx`（OLE2 格式） |

`check_kg_supported` 通过读取文件头 4 字节检测 OLE2 签名（`D0 CF 11 E0`），拦截 WPS 创建的假 `.docx` 文件。不支持 KG 的格式仍可入库（文件搬运），但跳过知识图谱构建。

## 七、Skills 和工具同步

### 7.1 Skills 同步（injector/sync.py）

`SkillSync` 负责将 `memory/skills/` 目录的变化同步到 LightRAG 知识图谱。

**同步机制**：
- watchdog 实时监控 + 定时扫描（默认 60 秒间隔）作为 fallback
- 变化检测：文件内容 SHA256 哈希对比（非 mtime）
- self_writing 过滤：写入后 2 秒内的修改事件被忽略
- 状态持久化：`~/.niu/skill_sync_state.json`，进程重启后不会误判已有 skill 为"新增"
- 注入/删除成功才更新状态文件，失败则下次扫描重试
- 无变化不写盘（2026-07-19 修复）：`scan_and_sync` 入口快照 `_last_scan` + `_last_notes_scan`，出口对比是否变化，无变化跳过 `_save_state`。覆盖 watchdog 并发新增 + KG ghost cleanup 失败塞空值两个边界（added/updated/deleted 全 0 但状态确实变化的场景）。watchdog `_execute` 路径不受影响（文件变化触发，本身就是"有变化"）

**Skill 实体格式**：

```python
entity_name = skill_name  # 自然语言，如 "photo-processing"
entity_type = "Skill"  # 写入时用 title case，LightRAG fork 写入路径会归一化为小写 "skill"
description = "{描述} | 触发词: {triggers}; 标签: {tags}"
source_id = "skill://{skill_name}"
```

同时创建 `知识体系脑区 -> skill_name` 的 `包含` 关系（`BELONGS_TO_RELATION`，定义于 `niu_api/internal/region_manager.py`），确保 skill 可从脑区遍历到达。

**便签同步**：`_scan_notes()` 将 `workspace/notes/notes.json` 的变化作为整文件文档提交给 `lightrag_insert`，entity_type 为 `"knowledge"`。

### 7.2 MCP 工具同步（injector/lightrag_sync.py）

`LightRAGSync` 负责定期同步文档和 Skills 到 LightRAG。

**当前状态**：
- 文档同步：已禁用（`_sync_vectors_db` 返回空，vector-store 已删除）
- Skills 同步：委托给 `SkillSync.scan_and_sync()`
- MCP 工具同步：已禁用（工具走 disk YAML 模式发现，不再通过 LightRAG 检索）
- 同步间隔：默认 6 小时（21600 秒）
- 状态文件：`~/.niu/last_lightrag_sync.json`

**兼容层模块**（保留但非主路径）：
- `injector/kg_sync.py`：旧接口兼容层。`KGSync` 类的 `sync_once()` / `stop()` 委托给 `LightRAGSync` 单例后台线程，无独立逻辑。
- `injector/kg_scanner.py`：已禁用。仅保留 `get_kg_scanner()` 占位函数（调用时打 warning），`KGScanner` 类的所有方法均返回 disabled 提示。实体提取由 LightRAG `ainsert()` 接管。

### 7.3 脑区同步（injector/region_sync.py）

`RegionSync` 负责定期运行 Leiden 社区检测，更新脑区节点。

**同步周期**：默认 24 小时（86400 秒）

**同步步骤**：
1. 运行 Leiden 社区检测
2. 创建/更新脑区节点（entity_type="brainregion"）
3. 清理已消失的脑区
4. 刷新激活管理器
5. 合并共激活脑区 + 溶解萎缩脑区
6. 衰减脑区边权重（半衰期模型 + 保底机制）

**缺省脑区配置化**：

缺省脑区定义存储在 `~/.niu/preferences.json` 的 `brain_regions.defaults` 数组中，而非代码硬编码。程序启动时读取配置 → 查图谱 → 缺啥补啥。

```json
{
  "brain_regions": {
    "defaults": [
      {"label": "聊天历史", "description": "日常对话中提炼的偏好、技能和经验记忆", "priority": "medium"},
      {"label": "文档库", "description": "用户导入的文档和资料，经解析后入库的知识", "priority": "permanent"},
      {"label": "知识体系", "description": "系统化组织的概念、关系和理论体系", "priority": "long"},
      {"label": "人际关系", "description": "人物实体、关系网络、社交图谱", "priority": "permanent"},
      {"label": "工作事务", "description": "工作相关的项目、任务、决策记录", "priority": "medium"},
      {"label": "生活事务", "description": "日常生活相关的日程、健康、财务", "priority": "short"},
      {"label": "组织机构", "description": "组织结构、部门、团队信息", "priority": "permanent"}
    ]
  }
}
```

**保护机制**：清理/解散/合并脑区时，通过 `is_default_region()` 查配置列表判断是否缺省脑区，而非依赖 `community_id` 是否为空推断。这确保了：
- 声明式保护：配置文件里声明的就是缺省脑区，程序不靠推断
- 配置驱动创建：缺省脑区的名称、描述、优先级都从 preferences.json 读取
- 向后兼容：旧版 preferences.json 没有 `brain_regions` 段时，使用代码中的默认值

### 7.3b 脑区边衰减增强机制

**设计目标**：防止实体变成孤立节点，同时按脑区优先级实现差异化遗忘曲线。

**边分类**：
- **脑区边**：实体↔脑区节点的边（对端节点 entity_type == "brainregion"）。逻辑边，表示归属关系，参与衰减/增强/保底机制。
- **知识关系边**：实体↔实体的边（如"认识"、"擅长"）。真实边，由 LLM 从内容中提取，不参与衰减。
- **锚点边**：脑区↔脑区节点的边（导航结构），不参与衰减/增强。
- **_session: 前缀边**：会话临时边，不参与衰减/增强。

**优先级体系与半衰期**：

| 优先级 | 半衰期 | 日衰减率 | 含义 |
|--------|--------|----------|------|
| `permanent` | 360天 | 0.99808 | 衰减但保底冻结，永不删除 |
| `long` | 360天 | 0.99808 | 长期记忆 |
| `medium` | 180天 | 0.99615 | 中期记忆 |
| `short` | 90天 | 0.99232 | 短期记忆 |

**保底逻辑**（FLOOR_WEIGHT = 0.1）：
- 总边数 == 1（只剩这一条边） → 保底冻结，权重不低于 0.1
- total_degree >= 2 → 允许正常衰减，低于 0.1 时删除边（permanent 与非 permanent 一致）

**永久脑区边衰减规则**（2026-07-18 修复）：

永久脑区（permanent 优先级，如文档库/人际关系/组织机构）与普通脑区的**唯一区别**是：脑区节点本身不被 `dissolve_shrunk_regions` 删除。**实体归属边的衰减逻辑与普通脑区完全一致**：

- weight 衰减到 < FLOOR_WEIGHT（0.1）且实体 total_degree >= 2 → 删除
- weight > FLOOR_WEIGHT → 正常衰减
- total_degree <= 1（孤立实体）→ 保底保护

旧版本的"永久脑区归属边永久保底永不删除"逻辑是 bug，已修复。NIU 根节点（entity_type=other）不在脑区循环内，其与脑区的边天然不受衰减影响。

**永久脑区空壳状态**：永久脑区即使所有归属边被删除，脑区节点本身仍保留（is_default_region 跳过 dissolve）。下次有新文档入库会重新建立归属边。

**脑区 dissolve 阈值**（2026-07-19 恢复 + 孤岛保护）：

`dissolve_shrunk_regions` 默认 `shrink_threshold=100`——成员数 < 100 才判萎缩，连续 3 轮（`shrink_rounds=3`）后执行 dissolve。

**孤岛保护**（2026-07-19 新增）：dissolve 执行前会检查所有成员的 `total_degree`：
- 所有成员 `degree >= 2` → 安全，执行 dissolve（成员挪给最相似邻居脑区 + 删除脑区节点）
- 有任何一个成员 `degree <= 1` → **取消本次 dissolve**，`shrink_count` 继续累加（+1）后持久化，下轮重新扫

这避免删除脑区后成员变孤岛（0 条边）。如果孤岛成员后来多了别的边，下轮 dissolve 就能成功；如果一直只有 1 条边，脑区永远不被删。

**缺省脑区保护**：`is_default_region` 跳过 `~/.niu/preferences.json` 配置的缺省脑区，永远不会被 dissolve（即使 0 成员）。

**历史**：2026-07-13 commit `4f03f10d` 曾越权把 `shrink_threshold` 从 100 改成 10，2026-07-19 恢复。

**社区重算输入范围**（2026-07-18 扩展）：

社区重算（每 24 小时一次）参与资格规则：

| 条件 | 说明 |
|------|------|
| 条件 1：非直连脑区 | 实体没有任何 `_region:contains` 边直连脑区（含孤儿实体） |
| 条件 2：只剩 1 条保底归属边 | 实体只有 1 条 `_region:contains` 边，且该边 weight ≤ 0.1（保底值） |

满足任一条件即参与社区重算（OR 关系）。条件 2 让被保底规则锁死的实体有机会迁移到新脑区——一旦被分配到新脑区多 1 条归属边，下轮衰减自然解除保底（不再满足 total_degree <= 1）。

**增强机制**：工具使用时，对应脑区的边权重恢复到 1.0（"用一次就满血"）。无论优先级，恢复目标都是 1.0。次日衰减从 1.0 重新按各脑区半衰期下降。

**priority 存储**：脑区节点的 description 字段中包含 `brain_meta_priority:{priority}`，由 `_encode_description()` 写入、`parse_priority_from_description()` 解析。旧值 `"core"`/`"category"` 输出 info 日志并回退到 `"medium"`。

**触发时机**：
- 衰减：RegionSync 守护线程每24小时执行一次
- 增强：handler.py 工具调用成功后触发 `reinforce_on_tool_use(tool_name)`

**脑区内过滤检索机制**：

点亮脑区后，系统通过 LightRAG 的 `filter_lambda` 参数在脑区成员范围内做语义检索，而非全图谱匹配。这确保了：
- 同一查询在不同脑区范围内返回不同结果（如"差旅费"在财务脑区匹配报销制度，在技术脑区匹配出差部署）
- 脑区成员实体通过 `包含` 边（`BELONGS_TO_RELATION`）维护，`get_all_region_members()` 直接从 NetworkX 图读取
- 检索结果与全局向量检索结果通过 seen_names 去重，避免重复注入

## 八、运维操作

### 8.1 检查 LightRAG 状态

```bash
# 查看存储目录
ls ~/.niu/lightrag_storage/

# 查看图谱文件大小
ls -lh ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml

# 查看向量索引
ls -lh ~/.niu/lightrag_storage/vdb_*.json

# 查看文档存储
ls -lh ~/.niu/lightrag_storage/kv_store_*.json
```

### 8.2 重建知识库

```bash
# 1. 删除存储目录
rm -rf ~/.niu/lightrag_storage/

# 2. 清理同步状态缓存（否则 SkillSync/RegionSync 会跳过重新注入）
rm -f ~/.niu/skill_sync_state.json
rm -f ~/.niu/last_lightrag_sync.json
rm -f ~/.niu/last_region_sync.json

# 3. 重启应用
# LightRAG 会在首次 get_lightrag() 调用时自动初始化空图谱
# SkillSync/RegionSync 会在后台自动重新注入所有 Skills 和脑区
```

### 8.3 检查文档处理状态

通过 `lightrag_document_status` 工具查看各状态文档数量：

- **pending**：已入队，等待处理
- **processing**：正在提取实体/关系
- **processed**：处理完成，已写入图谱
- **failed**：处理失败（pipeline 异常或取消）

通过 `lightrag_get_document` 工具查看单个文档的详细内容和状态。

### 8.4 删除特定文档

通过 `lightrag_delete_document` 工具级联删除：文档 -> 关联 chunks -> 关联 entities -> 关联 relationships。

### 8.5 LightRAG 入库配置

LightRAG 入库参数从 `~/.niu/preferences.json` 的 `lightrag` 配置段读取，修改后重启程序生效。

```json
{
  "lightrag": {
    "llm_model_max_async": 4,
    "chunk_token_size": 1200,
    "chunk_overlap_token_size": 50,
    "max_gleaning": 1,
    "embedding_model": "bge-base-zh-v1.5",
    "reranker_model": "none"
  }
}
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `llm_model_max_async` | 4 | 入库时 chunk 级 LLM 并发数。值越大并发越高但可能触发 API 限流；设为 1 则串行处理，最稳定但最慢 |
| `chunk_token_size` | 1200 | 文档分片大小（token 数）。值越大则分片越少，单次 LLM 调用输入越长 |
| `chunk_overlap_token_size` | 50 | 分片重叠 token 数。增加重叠可减少跨片实体丢失 |
| `max_gleaning` | 1 | 补充提取次数。每次 gleaning 会额外调用一次 LLM 来找出遗漏实体；设为 0 跳过补充提取 |
| `embedding_model` | `bge-base-zh-v1.5` | 向量模型名称（见 8.6 向量模型切换） |
| `reranker_model` | `none` | 重排序模型，`none` 表示关闭 |

#### LightRAG 思考链与模型配置

LightRAG 入库请求默认禁用思考链（`reasoning_effort: "none"`），防止深度推理导致实体提取超时。
LightRAG 官方明确建议不要使用带思考链的模型做入库。

**方案一：自动禁用思考链（零配置生效）**

不做任何配置，系统自动将所有 LightRAG 入库请求的 `reasoning_effort` 设为 `"none"`。
即使主 Agent 使用带思考链的模型（如 ark-code-latest），入库请求也不受影响。

**方案二：独立模型配置**

在 `config/user-config.json` 中配置 `lightrag_llm` 段，让 LightRAG 使用不同模型：

```json
{
  "llm": {
    "presetId": "ark-code-latest",
    "apiKey": "...",
    "apiBase": "https://ark.cn-beijing.volces.com/api/coding/v3",
    "model": "ark-code-latest",
    "type": "openai"
  },
  "lightrag_llm": {
    "presetId": "doubao",
    "apiKey": "",
    "apiBase": "https://ark.cn-beijing.volces.com/api/v3",
    "model": "doubao-pro-32k",
    "type": "openai",
    "reasoning_effort": "none"
  }
}
```

- `lightrag_llm` 段 `model` 为空时，使用主 Agent 同一模型（正常默认行为），但独立控制 `reasoning_effort`
- `lightrag_llm` 有 `model` 但缺 `apiKey`/`apiBase` 时，从 `llm` 段继承
- `reasoning_effort` 是独立配置维度，默认 `"none"`（禁用思考链），即使同一模型也可用不同思考深度
- 修改配置后重启程序生效
- 也可通过 MCP 工具 `set_lightrag_llm_config` 动态修改

**reasoning_effort 参数**

| 值 | 效果 | 适用场景 |
|----|------|----------|
| `none` | 完全禁用思考链（默认） | LightRAG 入库、简单提取任务 |
| `low` | 浅层推理 | 需要少量推理的入库任务 |
| `medium` | 中等推理 | 非入库的图谱查询任务 |
| `high` | 深度推理 | 不建议用于 LightRAG |

在 `lightrag_llm` 段中设置 `reasoning_effort` 可覆盖默认值 `"none"`。

### 8.6 向量模型切换

在 `~/.niu/preferences.json` 的 `lightrag.embedding_model` 中配置：

| 模型 | 维度 | 最大序列长度 | 说明 |
|------|------|-------------|------|
| `bge-base-zh-v1.5` | 768 | 512 | 默认，中文优化 |
| `bge-m3` | 1024 | 8192 | 多语言，2.2GB |
| `minilm-l12` | 384 | 128 | 旧版默认，轻量 |

切换模型后需重建知识库（删除 `~/.niu/lightrag_storage/` 后重启）。

## 九、知识图谱损坏检测与自愈修复

### 9.1 真相源与派生文件

LightRAG 存储目录 `~/.niu/lightrag_storage/` 下有 12 个文件，分两类：

**3 个真相源文件**（用户数据，禁止修复程序写/删）：

| 文件 | 内容 | 说明 |
|------|------|------|
| `graph_chunk_entity_relation.graphml` | 知识图谱本体（实体+关系） | 唯一权威真相源，含每个实体/关系的 source_id 指向所属 chunk_id |
| `kv_store_full_docs.json` | 原文档全文 | 辅助真相源，按 doc_id 索引 |
| `kv_store_llm_response_cache.json` | LLM 抽取结果缓存 | 辅助真相源，每个 extract entry 含 chunk_id + original_prompt（chunk 原文） |

**9 个派生文件**（从 3 真相源派生，丢失可重建）：

| 文件 | 派生来源 |
|------|---------|
| `kv_store_text_chunks.json` | GraphML source_id 提活跃 chunk_id → cache original_prompt 提取原文（cache 没有则 full_docs chunking 反查） |
| `kv_store_doc_status.json` | full_docs 的 doc_id + text_chunks 反查 chunks_list |
| `vdb_chunks.json` | text_chunks 内容做 embedding |
| `vdb_entities.json` | GraphML 节点 description 做 embedding |
| `vdb_relationships.json` | GraphML edge 做 embedding |
| `kv_store_entity_chunks.json` | GraphML 节点 source_id 反转（实体→chunks） |
| `kv_store_relation_chunks.json` | GraphML 边 source_id 反转（关系→chunks） |
| `kv_store_full_entities.json` | chunk→doc 反查 + GraphML 实体反查（doc→entities） |
| `kv_store_full_relations.json` | chunk→doc 反查 + GraphML 关系反查（doc→relations） |

**关键关系**：实体不是孤立的节点，每个实体必须挂在 1 个或多个 chunk 上才有意义。chunk 是知识图谱的最小语义单元，是实体和关系的"出处"。实体的 `source_id` 表示"这个实体从哪些 chunk 抽取出来"，关系的 `source_id` 同理。脑区节点（entity_type=brainregion）的 `source_id` 含 chunk_id 跟普通实体完全等价——脑区就是 LLM 从这些 chunk 抽取出来的实体，不是特殊节点。

### 9.2 损坏检测

启动时 `lightrag_integrity.check_all()` 检测 3 真相源 + vdb 数据一致性：

**3 真相源完好性四态判定**（`_check_truth_sources_intact`）：
- **absent**：文件不存在或 size=0
- **empty**：文件存在但内容为空（空 dict `{}` / 空 GraphML）
- **has_content**：文件存在且有内容（至少 1 个 node / 1 个 entry）
- **corrupt**：文件存在但 JSON/XML 解析失败

判定规则（v2 修复 2026-07-28）：
- 3 文件全部 absent/empty → 全新用户合法（intact=True，还没导入文档）
- 3 文件全部 has_content 且无 corrupt → 完好（intact=True）
- 部分文件 has_content 部分 absent/empty → **合法中间状态**（intact=True）
  - 脑区/Skills 注入路径只写 GraphML + 3 vdb + 可选 text_chunks，不写 full_docs/cache
  - GraphML 有内容 + full_docs/cache absent 是正常状态（用户未入库文档）
- 任一文件 corrupt → 损坏（intact=False，unrecoverable）

**vdb 数据一致性检测**（`_check_vdb_missing`，v2 启用）：
- GraphML 有 node 但 `vdb_entities` 无对应向量 → major（数据不一致，真损坏）
- GraphML 有 edge 但 `vdb_relationships` 无对应向量 → major
- 这是 v2 的核心改进：真损坏判定从"文件存在性"改为"数据一致性"

**vdb 文件内部一致性检测**（`_check_vdb_internal`，v3 新增 2026-08-14）：
- 检测 vdb_entities / vdb_relationships / vdb_chunks 的 matrix 行数 vs data 条数是否一致（孤儿向量）
- 不一致 → major（vdb_matrix_mismatch）
- 成因：跨进程并发 upsert 导致 matrix 与 data 不同步（LightRAG fork 注释警告 "Only one process should updating the storage at a time"）
- 后果：nano-vectordb 查询时孤儿向量行号越界崩溃——多数时候不显式报错，仅表现为回答准确度下降/搜索匹配度降低；极端场景（top_k 恰好命中孤儿向量）才显式报错
- 与 v2 的区别：v2 只查 GraphML⊆vdb 单向（防数据丢失）；v3 查 vdb 文件内部（孤儿向量/尾部错位）

**派生 kv_store 文件缺失**（`_check_derived_missing`，v2 改为不报错）：
- `kv_store_doc_status` / `entity_chunks` / `relation_chunks` / `full_entities` / `full_relations` 缺失不是损坏
- LightRAG `JsonKVStorage.initialize` 把缺失文件当空 dict，运行时按需 upsert
- 缺失时记 INFO 日志保留知情权，不阻断启动，不主动重建，不写空文件

**检测结果分级**：
- **critical_errors**：3 真相源 corrupt（GraphML/full_docs/cache JSON/XML 解析失败）
- **major_errors**：vdb 与 GraphML 数据不一致（node/edge 缺对应向量）
- **minor_errors**：其他次要问题

### 9.3 启动阻断机制

检测到 critical 或 major 错误时，启动流程阻断：
- 不初始化 LightRAG 主类（`get_lightrag()` 返回 None）
- 不启动 SkillSync / RegionSync 守护线程
- splash 显示损坏提示 + "尝试修复"按钮
- 其他所有进程（API 请求、文档入库、脑区同步等）全部阻断

用户点击"尝试修复"后，调 `/api/kg/lightrag/repair` → `run_repair_on_user_request` 进入修复流程。

**v3 例外（自动修复不弹窗，2026-08-14）**：`vdb_matrix_mismatch`（vdb 文件内部 matrix/data 行数不一致）→ **启动自检自动修复**——从 data.vector 重建 matrix（秒级、不删任何数据）→ 重跑检测 → 通过后正常启动，**不弹窗、无需用户操作**。其他损坏（真相源 corrupt / vdb_missing 文件缺失）仍走阻断 + "尝试修复"弹窗路径。

### 9.4 修复流程（run_repair_on_user_request）

修复程序的核心原则（铁律）：
1. **修复第一步只保留 3 真相源**：9 派生文件全删除（不备份、不回滚）
2. **GraphML 是唯一真相源**：full_docs + cache 是辅助文档，从 GraphML 引用按需提取重建
3. **修复程序不写 3 真相源**：所有写 3 真相源的代码全删光，重建只写 9 派生
4. **所有重建从 GraphML 读取**：不从 GraphML 读取的恢复操作全部删除

修复流程：
```
1. 停止 RegionSync 守护线程（stop_background_sync_blocking，join timeout=60）
   - 防止 RegionSync in-flight 任务在修复期间写 GraphML
   - 超时抛 RuntimeError 终止修复（不能让 GraphML 被写）
2. 设 _repairing=True 信号灯
   - 让其他线程的 get_lightrag() 返回 None（兜底防御）
3. 调 repair_all：
   3.1 检测 3 真相源完好性（_check_truth_sources_intact）
       - v2 修复：partial 状态（GraphML 有 + full_docs/cache 缺）不再判 unrecoverable
       - 只有 corrupt（JSON/XML 解析失败）才 unrecoverable
       - corrupt → 不删派生（保留现场让用户排查）
   3.2 删除 9 派生文件
   3.3 按依赖链重建 9 派生（走 LightRAG storage.upsert 接口）：
       text_chunks → doc_status → vdb_chunks → vdb_entities → vdb_relationships
       → entity_chunks → relation_chunks → full_entities → full_relations
       任一 unrecoverable → 立即 break，不继续后续重建
       注意：脑区/Skills 路径下 full_docs 缺失时，doc_status/full_entities/full_relations
       走"full_docs 为空 → 不写派生"分支（LightRAG 原生行为，符合"不重建空文件"要求）
4. reset_init_state + 重跑 check_all 更新检测结果
5. **不重启 RegionSync**（关键！修复后程序应退出，让用户重启时由正常启动流程触发）
6. finally 块清 _repairing=False（让下次 get_lightrag 能初始化）
```

**为什么修复后不重启 RegionSync**：RegionSync 守护线程跑 `_sync_loop` → `_run_sync_impl` → `_manage_region_nodes` → `create_region_nodes` 会写 GraphML（创建/合并脑区节点）。守护线程有"距上次同步超 21.6h 立即跑首次同步"逻辑，修复后立即重启会触发 sync 写真相源。修复程序必须让用户重启程序，由正常启动流程在 check 通过后才启动 RegionSync。

### 9.5 走 LightRAG storage.upsert 接口（v9 关键改进）

9 个派生文件重建走 LightRAG 原生 storage 接口的 `upsert` 方法，**不绕过直接写 JSON 文件**：

| 派生文件 | Storage 类 | 自动注入字段 |
|---------|-----------|-------------|
| text_chunks | JsonKVStorage | _id / create_time / update_time / llm_cache_list |
| doc_status | JsonDocStatusStorage | chunks_list（upsert 自动调 index_done_callback 写盘） |
| vdb_chunks | NanoVectorDBStorage | __id__ / __created_at__ / vector / matrix（L2 归一化） |
| vdb_entities | NanoVectorDBStorage | 同上 |
| vdb_relationships | NanoVectorDBStorage | 同上 |
| entity_chunks | JsonKVStorage | _id / create_time / update_time |
| relation_chunks | JsonKVStorage | 同上 |
| full_entities | JsonKVStorage | 同上 |
| full_relations | JsonKVStorage | 同上 |

走 storage 接口的好处：
- 字段注入、向量计算、L2 归一化、index_done_callback 触发全部由 LightRAG 自动处理
- 重建产物跟 LightRAG 原生启动后的派生文件字节级一致
- 不会因为字段格式不符导致后续删除文档/查询实体功能失效

### 9.6 7 种损坏场景测试

修复程序覆盖 7 种知识图谱损坏场景：

| 场景 | 模拟操作 | 预期结果 |
|------|---------|---------|
| 1. vdb_entities 缺失 | 删 vdb_entities.json | v2：检测报 major（数据不一致），repair 重建 vdb_entities，3 真相源不变 |
| 2. 9 派生全缺失 | 删全部 9 派生文件 | v2：派生 kv_store 缺失不报 major（合法状态）；vdb 缺向量报 major 触发 repair；repair 重建 vdb + 必要派生，3 真相源不变 |
| 3. GraphML 损坏 | 写损坏 GraphML（如 `<invalid xml`） | unrecoverable，9 派生文件未被删（保留现场） |
| 4. full_docs 损坏 | 写损坏 full_docs JSON | unrecoverable |
| 5. cache 损坏 | 写损坏 cache JSON | unrecoverable |
| 6. 已删实体不复活 | GraphML 含已删实体引用 | 重建后派生文件不含已删实体 |
| 7. weight 衰减值保留 | GraphML 含 weight=0.5 | 重建后 GraphML 的 weight 不变（GraphML 没被修改） |

**v2 新增场景**（脑区/Skills 路径合法状态）：
- GraphML 有 node + full_docs/cache absent + 5 派生 kv_store 缺失 → 检测 ok=True（合法中间状态，不弹窗不修复）

### 9.7 修复合格判定

修复程序合格的硬性标准：
1. 7 种损坏场景全部通过测试
2. 修复前后 3 真相源 mtime + sha256 完全不变（铁律 2）
3. 重建的 9 派生文件跟 LightRAG 原生启动后格式一致（字段名/类型/key/value 结构）
4. 修复期间无其他进程写 3 真相源（RegionSync 已停 + _repairing 信号灯兜底）

**重要**：合格判定必须用真实环境测试（启动 `./niu` → 删 vdb → 修复 → 退出 → 检查 sha256），不能用 tmp_path 隔离测试。tmp_path 隔离测试无法覆盖真实启动流程的 RegionSync 守护线程副作用。

### 9.8 故障排查要点

主 Agent 帮助用户排查修复问题时，重点检查：

1. **3 真相源是否被改写**：
   ```bash
   # 记录修复前 sha256
   shasum -a 256 ~/.niu/lightrag_storage/{graph_chunk_entity_relation.graphml,kv_store_full_docs.json,kv_store_llm_response_cache.json}
   # 修复后再次记录，对比是否一致
   ```
   如果 sha256 变了，说明有进程在修复期间写了真相源——检查 RegionSync 是否真的停了、是否有其他守护线程。

2. **修复后派生文件是否完整**：
   ```bash
   ls -la ~/.niu/lightrag_storage/{vdb_*.json,kv_store_*.json}
   ```
   9 个派生文件应该全部存在且有合理大小。如果某个文件缺失或 size=0，说明对应 repair_xxx 函数失败。

3. **修复结果 unrecoverable 字段**：
   - `_unrecoverable=True` → 3 真相源损坏，无法修复，需要从备份恢复真相源
   - `_unrecoverable=False` 但某个 repair_xxx status=error → 该派生文件重建失败，可重跑修复

4. **RegionSync 守护线程状态**：
   修复期间 RegionSync 必须完全停止。如果日志看到"RegionSync 已停止"后又有"Sync complete"，说明守护线程没真正停。

### 9.9 用户简易修复指引（删 vdb 触发修复）

**v3 优先路径（重启即自动修复，无需删文件）**：

当 vdb 文件内部不一致（matrix/data 行数不匹配、孤儿向量）时，**直接重启程序即可自动修复**：
1. 退出程序
2. 重新启动 ./niu
3. 启动自检检测到 vdb 内部不一致 → 自动从 data.vector 重建 matrix → 恢复正常
   （无需删文件、无需点弹窗，几秒内完成）

**用户可观察的症状**（判断是否属于此类问题，不需要看控制台窗口）：
- Agent 回答问题准确度下降：依赖知识图谱的回答变模糊、答非所问、漏关键信息
- 知识图谱搜索功能匹配度降低：搜相关话题搜不到、搜出无关内容
- 脑区相关的知识注入不工作（图谱检索是脑区点亮/知识注入的依赖，图谱坏了脑区也点不亮）
- 显式"查询失败"报错只在极端场景出现（检索恰好命中损坏的向量），多数时候不报错，只是结果错乱
- 以上症状出现时，先重启程序——绝大多数情况重启后自动修复，无需做其他操作

**当用户怀疑知识图谱数据有问题时**（查询结果异常、实体缺失、关系丢失等），最简单的修复方法是**删除 3 个 vdb 文件后重启程序**，系统会自动触发修复流程重建向量索引：

```bash
# 1. 退出程序（确保没有 niu 进程在运行）
ps aux | grep -E "niu|python.*niu_api" | grep -v grep
# 如有残留进程，用 kill -TERM 优雅退出（禁止 pkill -f niu，会损坏 vdb 文件）

# 2. 删除 3 个 vdb 文件（向量索引，可安全删除）
rm ~/.niu/lightrag_storage/vdb_chunks.json
rm ~/.niu/lightrag_storage/vdb_entities.json
rm ~/.niu/lightrag_storage/vdb_relationships.json

# 3. 重新启动程序
./niu
```

**原理**：
- vdb 文件是从 GraphML 派生的向量索引，删除后 `check_all` 会检测到"GraphML 有 node/edge 但 vdb 缺对应向量"（数据不一致，major 损坏）
- splash 显示损坏提示 + "尝试修复"按钮
- 用户点"尝试修复"后，`run_repair_on_user_request` 从 GraphML 重新构建 vdb 向量索引
- **3 真相源（GraphML + full_docs + cache）不会被改写**，只是向量索引重建

**适用场景**：
- 查询知识图谱报错或结果异常
- 实体/关系丢失但 GraphML 应该有
- 切换 embedding 模型后需要重建向量索引
- vdb 文件损坏（JSON 解析失败）

**不适用场景**（需要从备份恢复真相源）：
- GraphML 文件损坏（critical，unrecoverable）
- full_docs / cache 文件损坏（critical，unrecoverable）
- 这类情况删 vdb 无效，需要从备份恢复真相源

**注意事项**：
- 删 vdb 后必须重启程序，不能在程序运行时删（会触发文件锁冲突）
- 修复期间程序会阻断所有依赖 LightRAG 的功能（查询、入库、脑区同步等）
- 修复完成后程序会自动退出，用户需再次启动程序进入正常使用

## 十、与旧架构的对照

| 旧概念 | 新对应 |
|--------|--------|
| vectors.db | `~/.niu/lightrag_storage/vdb_*.json` |
| VectorSearchAdapter | lightrag-server 工具（LightRAGAdapter / LightRAGIngester） |
| 递归查询（is_recursive） | LightRAG 检索模式（local/global/hybrid/mix） |
| query_pattern | 不存在，LightRAG 自动处理语义匹配 |
| vector-store MCP 工具（7 个） | lightrag-server MCP 工具（23 个） |
| kg-server MCP 工具（20 个） | lightrag-server MCP 工具（23 个），统一替代 |
| 双存储（vectors.db + LightRAG） | 单存储（LightRAG only） |
| workspace.path/vectors.db | 固定路径 `~/.niu/lightrag_storage/` |
| mtime 变化检测 | SHA256 内容哈希检测 |
| tool_lifecycle 衰减-覆盖评分 | 已移除，MCP 工具走 disk YAML 模式发现 |