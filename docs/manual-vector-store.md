# 向量库与知识图谱运维手册

> 本文档描述当前系统的语义存储架构：LightRAG 知识图谱（主检索路径）和向量库（辅助路径）。

## 1. 架构概述

系统采用双存储架构：

| 存储 | 技术 | 位置 | 角色 |
|------|------|------|------|
| 知识图谱 | LightRAG + NanoVectorDB | `~/.niu/lightrag_storage/` | 主检索路径（Skills、知识、交互习惯、文档） |
| 向量库 | SQLite + 自建向量表 | `{workspace}/vectors.db` | 辅助路径（query_pattern、MCP 工具描述、interaction_habit 写入） |

**主检索路径**：每轮对话的 `_inject_dynamic_resources()` 通过 `LightRAGAdapter.search_multi_lightrag()` 检索 Skills、知识、交互习惯，0 LLM 调用（keywords 模式）。

**辅助路径**：`VectorSearchAdapter` 直接访问 `vectors.db`，用于递归查询（query_pattern）和 MCP 工具描述的初始化注册。

**MCP 服务器**：`lightrag-server`（15 个工具）统一替代了旧的 `vector-store`（7 个工具）和 `kg-server`（20 个工具）。当前 `REQUIRED_SERVERS` 共 8 个服务器。

## 2. LightRAG 知识图谱

### 2.1 存储与配置

**存储目录**：`~/.niu/lightrag_storage/`（由 `lightrag_manager.STORAGE_DIR` 定义）

**LLM 调用**：通过 `/llm/v1/` 代理路由到用户配置的模型（LiteLLM -> user-config.json）

**Embedding 调用**：直接 Python 函数调用（`niu_api.internal.embedding`），零 HTTP 开销

**Reranker**：直接 Python 函数调用（`niu_api.internal.reranker`），可选

**默认向量模型**：`bge-base-zh-v1.5`（768 维，512 tokens，中文优化）

**支持的向量模型**（在 `~/.niu/preferences.json` 的 `lightrag.embedding_model` 中配置）：

| 模型 | 维度 | 最大序列长度 | 说明 |
|------|------|-------------|------|
| `bge-base-zh-v1.5` | 768 | 512 | 默认，中文优化 |
| `bge-m3` | 1024 | 8192 | 多语言，2.2GB |
| `minilm-l12` | 384 | 128 | 旧版默认，轻量 |

**LightRAG 实例管理**（`niu_api.internal.lightrag_manager`）：
- 懒初始化：首次 `get_lightrag()` 调用时创建
- 独立守护线程运行 asyncio 事件循环，`call_async()` 桥接同步调用
- 初始化失败时缓存结果，避免重试风暴

### 2.2 查询模式

LightRAG 支持多种检索模式：

| 模式 | 说明 | 典型用途 |
|------|------|----------|
| `local` | 实体为中心的图遍历 | 精确查找，默认推荐 |
| `global` | 社区级概览 | 宏观理解 |
| `hybrid` | local + global | 兼顾细节与全局 |
| `mix` | KG + 向量组合 | 全面检索，最慢 |
| `naive` | 纯向量检索 | 无图数据场景 |
| `bypass` | 跳过检索，仅 LLM | 不需要知识库时 |

**Keywords 优化**：提供 `keywords` 参数可跳过 LLM 关键词提取，将延迟从 5-30s 降至 <1s，同时保持完整图遍历能力。动态注入（`_inject_dynamic_resources`）始终使用 keywords 模式。

### 2.3 LightRAG MCP 工具（15 个）

`lightrag-server` 提供 15 个统一工具，分三组：

**查询组（5 个）**：

| 工具 | 说明 |
|------|------|
| `lightrag_query` | 知询知识库，返回生成文本或原始上下文 |
| `lightrag_query_data` | 查询知识库，返回结构化数据（实体 + 关系 + chunks） |
| `lightrag_search_entities` | 按实体类型搜索（skill, tool, knowledge, person, photo, concept） |
| `lightrag_get_graph` | 获取子图（explore / snapshot） |
| `lightrag_timeline_query` | 时间线查询：向量匹配 -> 遍历时间链 -> 按时间戳排序 |

**插入组（4 个）**：

| 工具 | 说明 |
|------|------|
| `lightrag_insert` | 插入文档，LightRAG 自动提取实体和关系 |
| `lightrag_insert_custom_kg` | 直接注入结构化知识（实体 + 关系 + chunks），跳过 LLM 提取 |
| `lightrag_insert_entity` | 插入单个实体 |
| `lightrag_insert_relation` | 插入实体间关系 |

**管理组（6 个）**：

| 工具 | 说明 |
|------|------|
| `lightrag_delete_entity` | 删除实体及其所有关系 |
| `lightrag_delete_document` | 级联删除文档及其关联的 chunks、entities、relationships |
| `lightrag_document_status` | 获取文档处理状态计数 |
| `lightrag_get_document` | 获取完整文档内容及处理状态 |
| `lightrag_list_entities` | 列出实体、文档或实体类型标签 |
| `lightrag_merge_entities` | 合并多个实体，整合所有关系 |

### 2.4 核心适配器

**LightRAGAdapter**（`niu_api.internal.lightrag_adapter`）— 查询接口：

| 方法 | 说明 |
|------|------|
| `query()` | 文本查询，支持多种模式 |
| `query_data()` | 结构化查询，返回实体 + 关系 + chunks |
| `search_multi_lightrag()` | 单查询多分类检索，主检索入口 |
| `search_skills()` | 搜索 skill 实体 |
| `search_tools()` | 搜索 tool 实体 |
| `search_knowledge()` | 搜索 knowledge/concept 实体 |
| `search_interaction_habits()` | 搜索 interaction_habit 实体 |
| `explore_node()` | BFS 图遍历 |
| `timeline_query()` | 时间线查询 |
| `get_graph_snapshot()` | 全图快照 |

**LightRAGIngester**（`niu_api.internal.lightrag_adapter`）— 双路径注入接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| `inject_entity()` | 结构化 | 注入单个实体 |
| `inject_relation()` | 结构化 | 注入关系 |
| `inject_custom_kg()` | 结构化 | 注入完整自定义 KG（实体 + 关系 + chunks） |
| `inject_entities_batch()` | 结构化 | 批量注入实体（单次持久化，性能远优于循环调用） |
| `inject_document()` | 非结构化 | 插入文档，LLM 自动提取实体 |
| `inject_documents()` | 非结构化 | 批量插入文档 |
| `upsert_interaction_habit()` | 结构化 | 写入/更新交互习惯 |
| `update_habit_confidence()` | 结构化 | 更新交互习惯置信度 |

### 2.5 向后兼容别名

`lightrag-server` 的 `DEPRECATED_ALIASES` 映射了旧工具名到新工具名（仅文档参考，运行时由 `handler.py` 的 `_TOOL_ALIASES` 处理）：

| 旧工具 | 新工具 |
|--------|--------|
| `add_document` | `lightrag_insert` |
| `search_documents` | `lightrag_query` |
| `get_document` | `lightrag_get_document` |
| `delete_document` | `lightrag_delete_document` |
| `create_entity` | `lightrag_insert_entity` |
| `link_entities` | `lightrag_insert_relation` |
| `explore_node` | `lightrag_get_graph` |
| `query_graph` | `lightrag_query` |
| `graph_snapshot` | `lightrag_get_graph` |

## 3. 向量库（辅助路径）

### 3.1 概述

向量库（`vectors.db`）存储带向量的文档，用于：

- MCP 工具描述（`category=mcp_tool`）
- 查询模式（`category=query_pattern`）— 递归检索机制
- 系统文档（`category=document`）
- 交互习惯（`category=interaction_habit`）— 同时写入 LightRAG

**向量库路径**（按优先级解析）：

1. `NIU_DB_PATH` 环境变量（显式覆盖）
2. `WORKSPACE_PATH` 环境变量 + `/vectors.db`（由 Go 启动器设置）
3. `~/.niu/memory.json` 的 `workspace.path` + `/vectors.db`

路径解析统一由 `agent/vector_search.py` 的 `resolve_vector_db_path()` 和 `mcp-servers/vector-store/src/niu_vector_store/__init__.py` 的 `get_db_path()` 完成。解析失败时抛出 `ValueError`，不降级到默认路径。

### 3.2 数据结构

```sql
CREATE TABLE documents (
    id TEXT PRIMARY KEY,      -- 文档ID
    content TEXT NOT NULL,    -- 内容文本
    embedding BLOB,           -- 向量（float32 二进制）
    metadata TEXT             -- JSON 元数据
);
```

**索引**（由 `VectorSearchAdapter._ensure_indexes()` 创建）：

```sql
CREATE INDEX idx_level ON documents(json_extract(metadata, '$.level'));
CREATE INDEX idx_category ON documents(json_extract(metadata, '$.category'));
CREATE INDEX idx_server ON documents(json_extract(metadata, '$.server'));
```

**WAL 模式**：连接时启用 `PRAGMA journal_mode=WAL`，允许并发读写。

### 3.3 向量归一化

L2 归一化并非所有入库路径都执行：

- `add_document()`（MCP 服务器直接调用）不做 L2 归一化，直接存储原始向量
- `VectorSearchAdapter.upsert_interaction_habit()` 在入库前做 L2 归一化
- `init_vector_db.py` 的 `register_mcp_tools()` 在入库前做 L2 归一化

```python
vec = np.array(embedding, dtype=np.float32)
norm = np.linalg.norm(vec)
if norm > 0:
    vec = vec / norm
embedding_blob = vec.tobytes()
```

`VectorSearchAdapter` 的相似度搜索使用 `np.dot() / (norm_a * norm_b)` 计算余弦相似度，未归一化的向量仍可正确搜索。

### 3.4 文档类型

向量库中存储 5 类文档：

| category | 说明 | 用途 |
|----------|------|------|
| `mcp_tool` | MCP 工具描述 | 工具语义匹配 |
| `query_pattern` | 查询模式 | 递归检索 |
| `skill` | 动态技能 | 技能匹配 |
| `document` | 系统文档 | 文档检索 |
| `interaction_habit` | 交互习惯 | 习惯匹配与注入 |

注意：Skills 的主存储已迁移到 LightRAG 知识图谱，向量库中的 skill 记录为历史残留。

### 3.5 递归查询机制

两阶段向量检索，解决用户表达与工具描述语义差异问题：

```
用户输入："remind me in 5 minutes to take medicine"
    -> 第一轮检索
查询模式库（query_pattern）
    匹配到："remind me in X minutes"
    提取：refined_query = "schedule task"
    -> 第二轮检索
工具描述库（mcp_tool）
    匹配到：schedule_task
```

`query_pattern` 的 metadata 中包含：
- `is_recursive: True` — 触发递归检索
- `refined_query` — 第二轮检索使用的关键词
- `target_category` — 递归查询的目标 category（如 `"mcp_tool"`）

**search() 递归**：发现 `is_recursive=True` 的结果后，用 `refined_query` 执行第二轮检索，排除 `query_pattern` 类型。最多递归 3 次。

**search_multi() 递归**：一次查出所有记录，按 category 分桶。`query_pattern` 匹配不放入桶，单独收集为 `query_pattern_hits`。对最高分结果的 `refined_query` 对 `target_category`（默认 `mcp_tool`）做递归检索。递归可能触发新的 `query_pattern`，最多 3 轮。基础结果 + 所有递归结果一次性合并截断，同 doc_id 取最高分。

### 3.6 VectorSearchAdapter 接口

| 方法 | 说明 |
|------|------|
| `search()` | 语义搜索，支持递归查询 |
| `search_multi()` | 一次检索按 category 分组返回，支持递归 |
| `upsert_interaction_habit()` | 写入/更新 Interaction Habit |
| `search_interaction_habits()` | 检索 Interaction Habits |
| `update_habit_confidence()` | 更新置信度（success/fail） |
| `get_l2_content()` | 从 L1 记录获取对应 L2 原文 |
| `format_for_prompt()` | 格式化搜索结果为提示词注入格式 |

### 3.7 向量库 MCP 工具（7 个）

`vector-store` MCP 服务器提供以下 7 个工具（同进程架构，通过 `TOOL_SCHEMAS` 注册到 `ToolRegistry`）：

| 工具 | 说明 |
|------|------|
| `add_document` | 添加文档到向量库（支持 `file_path` 读取文件内容） |
| `search_documents` | 语义搜索（支持 `filter` 元数据过滤） |
| `get_document` | 按 ID 获取文档 |
| `delete_document` | 按 ID、语义搜索或 metadata 过滤删除文档 |
| `list_documents` | 列出所有文档（支持 `filter`、`limit`、`offset`） |
| `count_documents` | 统计文档总数 |
| `update_metadata` | 合并更新文档 metadata（保留未提及的字段） |

## 4. 交互习惯（Interaction Habits）

Interaction Habits 记录用户独特的表达方式和性格特征。同时存储在 LightRAG 知识图谱（主路径）和向量库（辅助路径）中。

### 4.1 三类内容

| 类型 | entity_type / metadata.type | 说明 |
|------|----------------------------|------|
| 工具方言 | `tool_dialect` | 用户独特的表达方式 -> 工具映射 |
| 用户状态 | `user_state` | 语气词 -> 情绪状态推断 |
| 用户画像 | `user_profile` | 关于用户的个人事实、偏好、习惯、性格 |

### 4.2 数据结构

**LightRAG 存储**：实体名格式 `habit:{habit_type}:{target_tool}`，description 包含习惯内容和 confidence 数据。

```python
# LightRAGIngester.upsert_interaction_habit() 写入格式
entity_name = "habit:tool_dialect:kg-server"
description = "赶紧叫下我 | confidence: {'success_count': 3, 'fail_count': 0, 'last_used': '2026-04-09'}"
entity_type = "interaction_habit"
```

**向量库存储**：通过 `VectorSearchAdapter.upsert_interaction_habit()` 写入。

```python
# 向量库中的记录
{
    "id": "habit:tool_dialect:123456",
    "content": "赶紧叫下我",
    "metadata": {
        "level": "l1",
        "category": "interaction_habit",
        "type": "tool_dialect",
        "target_tool": "scheduler-server/schedule_task",
        "source": "personal",
        "confidence": {
            "success_count": 3,
            "fail_count": 0,
            "last_used": "2026-04-09"
        }
    }
}
```

### 4.3 学习机制

Interaction Habits 的学习发生在两个时机：

1. **工具调用反馈**（handler.py）：工具调用成功/失败时，通过 `LightRAGIngester.update_habit_confidence()` 更新置信度
2. **对话学习**（context-manager）：Agent 在睡眠整理时从对话中学习新的表达方式

### 4.4 置信度机制

每个 Interaction Habit 携带置信度：
- `success_count`：成功匹配/验证次数
- `fail_count`：失败次数
- 当 `fail_count >= 3` 时，自动删除该记录

### 4.5 查询接口

主 Agent 在每轮对话时，通过 `_inject_dynamic_resources()` 查询相关的 Interaction Habits。当前检索路径为 **LightRAG 知识图谱**：

```python
# runner.py 中的实际调用
habit_adapter = LightRAGAdapter()
interaction_habits = habit_adapter.search_interaction_habits(
    query=effective_query, top_k=3, keywords=keywords,
)
```

## 5. Skills 同步

Skills 目录（`memory/skills/`）通过 `SkillSync`（`agent/injector/sync.py`）同步到 LightRAG 知识图谱。

**同步机制**：
- watchdog 实时监控 + 定时扫描（默认 60 秒间隔）作为 fallback
- 变化检测：文件 mtime 对比
- self_writing 过滤：写入后 2 秒内的修改事件被忽略
- 首次扫描时从 LightRAG 加载已有 skill 状态，避免重复 "Added"

**同步目标**：LightRAG 知识图谱（`_inject_skill_to_lightrag()`），entity_type=`"skill"`

**Skill 实体格式**：

```python
entity_name = "skill:{name}"
entity_type = "skill"
description = "{description} | 触发词: {triggers}; 标签: {tags}"
```

**便签同步**：`_scan_notes()` 将 `workspace/notes/notes.json` 的变化同步到 LightRAG，entity_type=`"knowledge"`

## 6. 动态注入机制

每轮对话前，`_inject_dynamic_resources()`（`agent/runner.py`）执行知识注入：

**检索顺序**：

1. **LightRAG 主检索**（`local + keywords` 模式）— skills + knowledge，0 LLM 调用
2. **interaction_habits**（LightRAG + keywords）
3. **brain memories**（脑图）

**返回空 MCP 工具评分**：动态注入不再注入 MCP 工具评分（旧版 `tool_lifecycle` 的衰减-覆盖模式已移除）。

**注入格式**：
- Skills 注入后附带技能使用指引（需 `file_read` 读取完整技能文件）
- Knowledge 注入后附带知识探索指引（可 `disk` 查询知识图谱）
- Interaction habits 直接注入
- Brain memories 直接注入

## 7. 初始化与运维脚本

### 7.1 主初始化脚本

**位置**：`scripts/init_vector_db.py`

**功能**：
1. 创建向量库表结构
2. 同步 Skills 到 LightRAG 知识图谱（通过 `SkillSync`）
3. 注册 MCP 工具描述到向量库（只注册 `visibility=dynamic` 的工具）
4. 注册查询模式（调用 `scripts/index_query_patterns.py`）
5. 注入系统说明书 L1 摘要（调用 `scripts/inject_system_manual.py`）

**执行方式**：
```bash
cd E:/tools/ai-bot
python scripts/init_vector_db.py

# 包含 Query Patterns 初始化（需要 LLM API）
python scripts/init_vector_db.py --with-query-patterns

# 非交互模式
python scripts/init_vector_db.py -y
```

### 7.2 MCP 工具注册策略

`register_mcp_tools()` 的注册策略：
- 从 `data/mcp_tools.json` 读取工具定义
- 从 `config/mcp-servers.yaml` 读取工具 `visibility` 配置
- 只注册 `visibility=dynamic` 的工具到向量库
- `static` 和 `hidden` 工具不存入向量库

### 7.3 辅助脚本

| 脚本 | 功能 |
|------|------|
| `scripts/export_all_mcp_tools.py` | 导出所有 MCP 工具到 `data/mcp_tools.json` |
| `scripts/register_all_mcp_tools_from_json.py` | 从 `data/mcp_tools.json` 批量注册工具到向量库 |
| `scripts/check_mcp_tools_in_db.py` | 检查向量库中的工具数量和分布 |
| `scripts/index_query_patterns.py` | 注册查询模式到向量库 |
| `scripts/inject_system_manual.py` | 注入系统说明书 L1 摘要到向量库 |
| `scripts/reindex_vectors.py` | 重新生成向量索引 |
| `scripts/verify_vector_db.py` | 验证向量库完整性 |
| `scripts/sync_skills.py` | 手动触发 Skills 同步 |
| `scripts/ingest_unified.py` | 统一文档入库（LightRAG + 向量库） |

### 7.4 批量注册模式

向量库支持分批注册：一次注册太多可能失败，失败时删除成功的，重新注册剩余的，直到全部完成。

## 8. Metadata 规范

### 8.1 基础字段（所有文档必须有）

```python
{
    "level": "l1",           # 层级标识（小写）
    "category": "...",       # 文档类型
    "language": "en"         # 内容语言（英文或中文，中文查询模式使用 "zh"）
}
```

### 8.2 按类型的扩展字段

**mcp_tool：**
```python
{
    "level": "l1",
    "category": "mcp_tool",
    "language": "en",
    "name": "schedule_task",
    "server": "scheduler-server",
    "description": "Create scheduled tasks...",
    "input_schema": {...}
}
```

**query_pattern：**
```python
{
    "level": "l1",
    "category": "query_pattern",
    "language": "en",
    "type": "query_pattern",
    "is_recursive": True,
    "refined_query": "schedule task",
    "target_category": "mcp_tool",
    "description": "Remind user after X minutes"
}
```

**skill：**
```python
{
    "level": "l1",
    "category": "skill",
    "language": "en",
    "name": "photo-processing",
    "description": "...",
    "source": "memory/skills/photo-processing.md",
    "priority": 50,
    "tags": [...],
    "triggers": [...]
}
```

**document：**
```python
{
    "level": "l1",
    "category": "document",
    "language": "en",
    "resource_type": "system_manual",
    "section": "Architecture > Data Flow",
    "title": "Data Flow Architecture"
}
```

**interaction_habit：**
```python
{
    "level": "l1",
    "category": "interaction_habit",
    "language": "zh",
    "type": "tool_dialect",
    "source": "personal",
    "confidence": {
        "success_count": 3,
        "fail_count": 0,
        "last_used": "2026-04-09"
    }
}
```

## 9. MCP 服务器加载

`agent/mcp_loader.py` 在启动时加载所有必需的 MCP 服务器，严格验证：任何加载失败将终止应用。

**REQUIRED_SERVERS（8 个）**：

| 服务器 | 模块 | 说明 |
|--------|------|------|
| `photo-server` | `niu_photo_server` | 照片管理 + 人脸识别 |
| `config-manager` | `niu_config_manager` | 配置管理 |
| `memory-server` | `niu_memory_server` | 智能记忆提取和检索 |
| `lightrag-server` | `niu_lightrag_server` | 知识图谱 + 语义搜索（统一替代 vector-store + kg-server） |
| `file-parser` | `niu_file_parser` | 文档解析 |
| `session-manager` | `niu_session_manager` | 会话管理 |
| `scheduler-server` | `niu_scheduler_server` | 定时任务 |
| `browser-server` | `niu_browser_server` | 浏览器自动化 |

所有服务器采用同进程架构，通过 `TOOL_SCHEMAS` 注册到 `ToolRegistry`，无需 stdio 通信。
