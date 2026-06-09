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
| keywords | 关系类型（如 "skilled_in"、"_region:contains"） |
| description | 关系描述 |
| weight | 边权重 |

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
| interactionhabit | 交互习惯 | handler.py 工具调用反馈时写入 |
| document | 文档 | 文档入库时 LightRAG 自动提取 |
| organization | 组织 | 文档入库时 LightRAG 自动提取 |
| technology | 技术 | 文档入库时 LightRAG 自动提取 |
| other | 其他 | LLM 无法归类时的兜底类型 |

**实体命名规范**：所有实体名使用自然语言（如 "Python"、"任飞"、"影像记忆脑区"），不使用冒号前缀格式（如 ~~"skill:Python"~~、~~"person:uuid"~~）。`_normalize_entity_name()` 保留为恒等函数做向后兼容。

**大小写规范**：所有 entity_type 和 keywords 统一小写存储（写入时 `.lower()`），查询时 `.lower()` 比较。此规范在 LightRAG fork 的所有写入路径（`ainsert_custom_kg`、`acreate_entity`、`acreate_relation`、`_edit_entity_impl`、`_merge_entities_impl`）和查询路径（`get_brain_regions`、`has_edge`、dict 查找）中统一执行。

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

### 插入组（5 个）

| 工具 | 说明 |
|------|------|
| `lightrag_insert` | 插入文档，LightRAG 自动提取实体和关系 |
| `lightrag_insert_file` | 按文件路径插入，LightRAG 读取并解析文件（支持 DOCX/PDF/PPTX/XLSX/TXT/MD 等），异步处理 |
| `lightrag_insert_custom_kg` | 直接注入结构化知识（实体 + 关系 + chunks），跳过 LLM 提取。用于 Skills、工具、照片名等需精确控制的数据 |
| `lightrag_insert_entity` | 插入单个实体（通过 inject_custom_kg），自动创建 Niu -> 实体锚点关系 |
| `lightrag_insert_relation` | 插入实体间关系（通过 inject_custom_kg） |

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

**Skill 实体格式**：

```python
entity_name = skill_name  # 自然语言，如 "photo-processing"
entity_type = "skill"
description = "{描述} | 触发词: {triggers}; 标签: {tags}"
source_id = "skill://{skill_name}"
```

同时创建 `知识体系脑区 -> skill_name` 的 `_region:contains` 关系，确保 skill 可从脑区遍历到达。

**便签同步**：`_scan_notes()` 将 `workspace/notes/notes.json` 的变化作为整文件文档提交给 `lightrag_insert`，entity_type 为 `"knowledge"`。

### 7.2 MCP 工具同步（injector/lightrag_sync.py）

`LightRAGSync` 负责定期同步文档和 Skills 到 LightRAG。

**当前状态**：
- 文档同步：已禁用（`_sync_vectors_db` 返回空，vector-store 已删除）
- Skills 同步：委托给 `SkillSync.scan_and_sync()`
- MCP 工具同步：已禁用（工具走 disk YAML 模式发现，不再通过 LightRAG 检索）
- 同步间隔：默认 6 小时（21600 秒）
- 状态文件：`~/.niu/last_lightrag_sync.json`

### 7.3 脑区同步（injector/region_sync.py）

`RegionSync` 负责定期运行 Leiden 社区检测，更新脑区节点。

**同步周期**：默认 24 小时（86400 秒）

**同步步骤**：
1. 运行 Leiden 社区检测
2. 创建/更新脑区节点（entity_type="brainregion"）
3. 清理已消失的脑区
4. 刷新激活管理器
5. 合并共激活脑区 + 溶解萎缩脑区

**缺省脑区配置化**：

缺省脑区定义存储在 `~/.niu/preferences.json` 的 `brain_regions.defaults` 数组中，而非代码硬编码。程序启动时读取配置 → 查图谱 → 缺啥补啥。

```json
{
  "brain_regions": {
    "defaults": [
      {"label": "聊天历史", "description": "日常对话中提炼的偏好、技能和经验记忆", "priority": "core"},
      {"label": "文档库", "description": "用户导入的文档和资料，经解析后入库的知识", "priority": "core"},
      {"label": "知识体系", "description": "系统化组织的概念、关系和理论体系", "priority": "core"},
      {"label": "人际关系", "description": "人物实体、关系网络、社交图谱", "priority": "category"},
      {"label": "工作事务", "description": "工作相关的项目、任务、决策记录", "priority": "category"},
      {"label": "生活事务", "description": "日常生活相关的日程、健康、财务", "priority": "category"}
    ]
  }
}
```

**保护机制**：清理/解散/合并脑区时，通过 `is_default_region()` 查配置列表判断是否缺省脑区，而非依赖 `community_id` 是否为空推断。这确保了：
- 声明式保护：配置文件里声明的就是缺省脑区，程序不靠推断
- 配置驱动创建：缺省脑区的名称、描述、优先级都从 preferences.json 读取
- 向后兼容：旧版 preferences.json 没有 `brain_regions` 段时，使用代码中的默认值

**脑区内过滤检索机制**：

点亮脑区后，系统通过 LightRAG 的 `filter_lambda` 参数在脑区成员范围内做语义检索，而非全图谱匹配。这确保了：
- 同一查询在不同脑区范围内返回不同结果（如"差旅费"在财务脑区匹配报销制度，在技术脑区匹配出差部署）
- 脑区成员实体通过 `_region:contains` 边维护，`get_all_region_members()` 直接从 NetworkX 图读取
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

### 8.6 向量模型切换

在 `~/.niu/preferences.json` 的 `lightrag.embedding_model` 中配置：

| 模型 | 维度 | 最大序列长度 | 说明 |
|------|------|-------------|------|
| `bge-base-zh-v1.5` | 768 | 512 | 默认，中文优化 |
| `bge-m3` | 1024 | 8192 | 多语言，2.2GB |
| `minilm-l12` | 384 | 128 | 旧版默认，轻量 |

切换模型后需重建知识库（删除 `~/.niu/lightrag_storage/` 后重启）。

## 九、与旧架构的对照

| 旧概念 | 新对应 |
|--------|--------|
| vectors.db | `~/.niu/lightrag_storage/vdb_*.json` |
| VectorSearchAdapter | lightrag-server 工具（LightRAGAdapter / LightRAGIngester） |
| 递归查询（is_recursive） | LightRAG 检索模式（local/global/hybrid/mix） |
| query_pattern | 不存在，LightRAG 自动处理语义匹配 |
| vector-store MCP 工具（7 个） | lightrag-server MCP 工具（16 个） |
| kg-server MCP 工具（20 个） | lightrag-server MCP 工具（16 个），统一替代 |
| 双存储（vectors.db + LightRAG） | 单存储（LightRAG only） |
| workspace.path/vectors.db | 固定路径 `~/.niu/lightrag_storage/` |
| mtime 变化检测 | SHA256 内容哈希检测 |
| tool_lifecycle 衰减-覆盖评分 | 已移除，MCP 工具走 disk YAML 模式发现 |