# LightRAG 融合方案

> 创建日期：2026-04-22
> 状态：需求分解完成，待方案设计

## 一、背景

当前系统使用 **SQLite 向量库 + KuzuDB 知识图谱** 作为知识检索基础设施，两者独立运行，检索时简单拼接结果。LightRAG 提供了**双层检索**（entity-level + relation-level）+ **图遍历扩展**的融合检索模式，检索质量显著更高。

目标：用 LightRAG 替代现有 vector-store + kg-server，保留现有 MCP 工具接口的语义，升级底层检索能力。

## 二、核心优势

1. **双层检索是质变**：当前向量库和 KG 独立查询，结果简单拼接。LightRAG 的 hybrid/mix 模式先从向量找实体/关系，再沿图扩展
2. **实体提取一体化**：当前 kg_scanner 用子 Agent 异步提取，流程复杂且延迟大。LightRAG 在 ainsert 时同步提取+合并
3. **描述合并机制**：LightRAG 有 `_handle_entity_relation_summary` 做增量合并（LLM 摘要），当前系统只是简单覆盖
4. **存储后端可升级**：当前 SQLite 暴力扫描无法扩展。LightRAG 可无缝切换到 Milvus/FAISS/Qdrant

## 三、大模型代理（关键适配器）

### 现有资产

`niu_api/page_agent_proxy.py`（已删除，git 历史可恢复）实现了 OpenAI 兼容的 LLM 代理：

- **端点**：`/proxy/v1/chat/completions` 和 `/proxy/v1/models`
- **格式转换**：OpenAI 格式 ↔ LiteLLM 格式（消息、工具定义、响应、tool_calls）
- **走自有通道**：通过 `LiteLLMSession` 调用 `config/user-config.json` 里的模型配置
- **工具循环支持**：完整处理 tool_calls 的返回和解析
- **异步桥接**：`asyncio.to_thread` 包装同步的 `LiteLLMSession.chat()` 生成器
- **支持 OpenAI 和 Anthropic**：自动转换为 OpenAI 格式返回

### 对 LightRAG 的意义

LightRAG 的 LLM 绑定支持 OpenAI 格式，只需将 `base_url` 指向本地代理即可：

```python
rag = LightRAG(
    llm_model_func=lambda **kwargs: openai_complete(
        model="proxy-model",
        base_url="http://localhost:9876/proxy/v1",
        api_key="not-needed",
        **kwargs,
    ),
)
```

**之前分析中"高严重度"的 LLM 调用不兼容问题，被代理完全消解。**

### 恢复计划

1. 从 git 恢复 `page_agent_proxy.py`
2. 去掉 Page-Agent 特有逻辑，改为通用 LLM Proxy（重命名为 `llm_proxy.py`）
3. 在 `niu_api/__main__.py` 注册路由

## 四、技术架构

### 当前架构

```
Agent (handler.py)
    ├── vector-store MCP (7 工具)
    │   ├── SQLite + SentenceTransformer MiniLM-L12 (384维)
    │   ├── 暴力扫描（无 ANN 索引）
    │   └── L0/L1/L2 三级存储
    ├── kg-server MCP (20 工具)
    │   ├── KuzuDB (嵌入式, Cypher 查询)
    │   ├── 实体/关系/文档管理
    │   └── kg_scanner 异步提取
    └── 两者独立查询，结果简单拼接
```

### 目标架构

```
Agent (handler.py)
    └── lightrag-server MCP (新)
        ├── LightRAG Core
        │   ├── entities_vdb       ← 替代 vector-store 的实体检索
        │   ├── relationships_vdb  ← 替代 vector-store 的关系检索
        │   ├── chunks_vdb         ← 替代 vector-store 的文档检索
        │   └── chunk_entity_relation_graph ← 替代 kg-server
        ├── LLM 代理层 (llm_proxy.py)
        │   └── 复用 LiteLLMSession + user-config.json
        ├── Embedding 适配层
        │   ├── 候选模型：BAAI/bge-m3（LightRAG 推荐，同规模多语言）
        │   └── Reranker：BAAI/bge-reranker-v2-m3（LightRAG 推荐，提升检索精度）
        └── 保留独立存储
            ├── interaction_habits (SQLite)
            └── photos/faces (photos.db)
```

### 检索模式

LightRAG 的 `QueryParam(mode=...)` 支持四种模式，覆盖所有检索需求：

| mode | 向量检索 | 图遍历 | 等价于 | 适用场景 |
|------|---------|--------|--------|---------|
| `local` | ✅ | ❌ | 当前向量库 | 纯语义匹配（工具发现、习惯检索） |
| `global` | ❌ | ✅ | 当前 KG 查询 | 纯图遍历（路径查找、社区分析） |
| `hybrid` | ✅ | ✅ | 向量+KG拼接 | 向量找起点+图扩展 |
| `mix` | ✅ | ✅ | hybrid+原文 | 最全面检索，含原始文档块 |

**关键结论**：通过不同 mode 参数，LightRAG 可以替代当前向量库和 KG 的所有检索场景，不需要保留独立的向量库。

### Embedding 模型选择

| 模型 | 维度 | 大小 | 多语言 | 说明 |
|------|------|------|--------|------|
| 当前：all-MiniLM-L12 | 384 | ~120MB | ❌ 英文为主 | 检索质量一般 |
| 候选：BAAI/bge-m3 | 1024 | ~570MB | ✅ 多语言 | LightRAG 推荐，同规模下最优 |

**迁移影响**：更换 Embedding 模型意味着现有向量数据需要全部重新计算，无法增量迁移。建议在融合时一次性切换。

### Reranker

LightRAG 推荐 BAAI/bge-reranker-v2-m3 作为 Reranker，在检索结果返回前做二次排序，提升精度。

- 模型：BAAI/bge-reranker-v2-m3
- 作用：对 LightRAG 返回的候选结果做 cross-encoder 精排
- 位置：在 LightRAG 查询之后、返回给 Agent 之前
- 是否必须：非必须，但推荐。初期可不用，后续作为优化项加入

### 存储后端选择

| 组件 | 初期（验证） | 生产（升级） |
|------|-------------|-------------|
| 向量存储 | NanoVectorDB（默认） | Milvus / FAISS |
| 图存储 | NetworkX（默认） | Neo4j |
| KV 存储 | JsonKVStorage（默认） | 保持默认 |

---

## 五、业务场景分解（第一步产出）

### 场景总览

| # | 场景 | 当前使用 | 触发时机 | 关键文件 |
|---|------|---------|---------|---------|
| V1 | 每轮动态资源注入 | vector-store (search_multi) | 每轮对话开始+结束 | `agent/runner.py:562-675` |
| V2 | 工具生命周期评分 | vector-store (search → update_from_search) | 每轮结束 | `agent/runner.py:437-481` |
| V3 | Skill 同步到向量库 | vector-store (upsert) | 文件变更(watchdog)+定期扫描 | `agent/injector/sync.py` |
| V4 | 交互习惯追踪 | vector-store (search_habits, update_confidence) | 每次工具调用后 | `agent/handler.py:302-327` |
| V5 | MCP 工具描述索引 | vector-store (add_document) | 初始化+API端点 | `scripts/init_vector_db.py`, `niu_api/injector.py` |
| V6 | 系统手册 L1 注入 | vector-store (upsert) | 初始化脚本 | `scripts/inject_system_manual.py` |
| V7 | 照片/文档 L1/L2 存储 | vector-store (直接SQL) | 照片入库 | `mcp-servers/photo-server/` |
| V8 | 递归查询模式搜索 | vector-store (search_multi + recursion) | 每轮对话 | `agent/vector_search.py:622-711` |
| V9 | 向量库清理 | vector-store (直接SQL) | 定时任务 | `agent/vector_cleanup.py` |
| K1 | 文档入库到 KG | kg-server (create_document) | 照片/文档/笔记创建 | `photo-server`, `notes_api.py` |
| K2 | 实体提取（pending 文档） | kg-server + entity-extractor 子Agent | KGScanner 60秒扫描 | `agent/injector/kg_scanner.py` |
| K3 | KG 丰富化 | kg-server + kg-enricher 子Agent | 定时任务(8am) | `config/agents/kg-enricher.md` |
| K4 | Dream Evolver 知识写入 | kg-server (create_entity/document/link) | 对话结束后的后台处理 | `config/agents/dream-evolver.md` |
| K5 | KG 批量回填同步 | kg-server + vector-store | 6小时周期 | `agent/injector/kg_sync.py` |
| K6 | 图可视化 API | kg-server (snapshot/stats/hubs/explore/path) | 前端请求 | `niu_api/kg_api.py` |
| K7 | 知识探索引导 | kg-server (explore_node, get_related_entities) | 向量检索命中文档后注入提示 | `agent/runner.py:663` |
| K8 | LLM 直接调用 KG 工具 | kg-server (全部19工具) | LLM 工具调用 | `agent/handler.py:793-876` |
| C1 | 记忆存取（memory-server） | vector-store (共享 vectors.db) | remember/recall 工具调用 | `mcp-servers/memory-server/` |

### 场景详细分析

---

#### V1: 每轮动态资源注入

**当前实现**：`NiuRunner._inject_dynamic_resources()` 调用 `VectorSearchAdapter.search_multi()` 同时搜索 skill、mcp_tool、document、interaction_habit 四类内容，注入到系统提示词。

**期望目标**：用 LightRAG 的 `aquery_data(mode="mix")` 替代，一次查询同时获得实体、关系、文档块，检索质量更高（图扩展 vs 独立向量匹配）。

**差距**：
- LightRAG 的查询是"问题→上下文"，不按 category 分类返回。需要保留 category 过滤能力
- 当前 search_multi 按 category 分桶返回，LightRAG 返回扁平的 entities+relationships+chunks
- interaction_habit 和 skill 是系统内部概念，不是知识库文档，LightRAG 无法直接处理

---

#### V2: 工具生命周期评分

**当前实现**：向量搜索返回 mcp_tool 类型的结果，相似度分数 * 100 作为工具评分，通过 `ToolLifecycleManager.update_from_search()` 覆盖衰减后的分数。

**期望目标**：LightRAG 的实体检索可以找到与查询相关的技术实体，但无法直接映射到 MCP 工具名。

**差距**：
- MCP 工具描述索引是系统特有概念，LightRAG 的实体/关系模型不直接支持
- 需要保留独立的工具描述索引机制，或在 LightRAG 中用自定义实体类型模拟

---

#### V3: Skill 同步到向量库

**当前实现**：`SkillSync` 监控 `memory/skills/*.md`，提取触发词/描述/标签，生成 L1 摘要，计算 embedding，upsert 到 vectors.db。

**期望目标**：Skill 作为文档插入 LightRAG，通过 `ainsert()` 自动分块+提取实体+建索引。

**差距**：
- LightRAG 的 ainsert 会用 LLM 提取实体，增加 API 调用成本
- Skill 的触发词/标签等结构化元数据在 LightRAG 中无法直接保留
- 可能需要用 `ainsert_custom_kg()` 直接注入预提取的实体

---

#### V4: 交互习惯追踪

**当前实现**：每次工具调用后，`handler.tool_after_callback()` 搜索匹配的交互习惯，更新成功/失败置信度。

**期望目标**：交互习惯是系统运行时状态，不适合放入 LightRAG 的知识图谱。应保留独立存储。

**差距**：无。此场景不需要迁移到 LightRAG，保留现有 SQLite 即可。

---

#### V5: MCP 工具描述索引

**当前实现**：初始化时将 MCP 工具描述作为 L1 记录存入 vectors.db（category=mcp_tool），用于语义搜索发现相关工具。

**期望目标**：同 V2，工具描述是系统特有概念。可考虑作为特殊文档插入 LightRAG，或保留独立索引。

**差距**：
- LightRAG 的实体提取会对工具描述产生不相关的实体（如提取工具参数名作为实体）
- 工具描述需要精确匹配 server/tool 名称，LightRAG 的语义检索可能引入噪声

---

#### V6: 系统手册 L1 注入

**当前实现**：系统手册章节作为 L1 摘要存入 vectors.db（category=document, resource_type=system_manual）。

**期望目标**：系统手册作为文档插入 LightRAG，通过 chunks_vdb 检索。

**差距**：
- L1/L2 层级在 LightRAG 中不存在。LightRAG 自动分块，但分块策略不同
- 系统手册内容偏技术性，LightRAG 的实体提取可能提取出大量技术术语实体

---

#### V7: 照片/文档 L1/L2 存储

**当前实现**：photo-server 直接写 vectors.db，存储照片描述的 L1/L2 记录。

**期望目标**：照片描述作为文档插入 LightRAG。

**差距**：
- photo-server 当前直接操作 SQLite，改为 LightRAG 需要调用 ainsert
- 照片的人脸/场景描述格式需要适配 LightRAG 的文档格式

---

#### V8: 递归查询模式搜索

**当前实现**：`search_multi(enable_recursion=True)` 检测 query_pattern 记录，用 refined_query 做二次搜索，实现多跳检索。

**期望目标**：LightRAG 的 hybrid/mix 模式天然支持多跳检索（实体→关系→关联实体），不需要显式的递归机制。

**差距**：
- LightRAG 的图遍历本身就是多跳的，可以替代递归查询模式
- 但某些精确的查询改写规则（如"浏览新闻"→"browser_navigate news website"）仍需保留

---

#### V9: 向量库清理

**当前实现**：`VectorCleanup` 清理无效 L1 指针、孤立 skill、孤立 MCP 工具、重复记录。

**期望目标**：LightRAG 有自己的数据管理（`adelete_by_doc_id` 等），但清理逻辑不同。

**差距**：
- 需要重写清理逻辑，适配 LightRAG 的存储结构
- L1/L2 指针验证不再需要

---

#### K1: 文档入库到 KG

**当前实现**：photo-server/notes_api 创建 Document 节点（entity_status=pending），等待 KGScanner 提取实体。

**期望目标**：直接调用 `rag.ainsert(content)`，LightRAG 同步完成分块+实体提取+关系建立+向量化。

**差距**：
- 当前是异步提取（pending→processing→completed），LightRAG 是同步提取
- 需要处理插入失败的重试逻辑
- 大文档的插入可能耗时较长（LLM 提取实体），需要异步化

---

#### K2: 实体提取（pending 文档）

**当前实现**：KGScanner 60秒扫描 pending 文档，派 entity-extractor 子Agent 提取实体/关系。

**期望目标**：LightRAG 的 ainsert 内建实体提取，不需要独立的 scanner。

**差距**：
- KGScanner 整个机制可以废弃，LightRAG 内建了更强大的提取
- 但需要保留"文档待处理"的状态追踪（LightRAG 的 DocStatus 机制可替代）

---

#### K3: KG 丰富化

**当前实现**：kg-enricher 子Agent 将向量库中的经验/画像/查询模式同步到 KG。

**期望目标**：LightRAG 统一了向量库和 KG，不需要跨系统同步。

**差距**：
- kg-enricher 的"经验→实体"映射逻辑需要适配 LightRAG 的 `ainsert_custom_kg()`
- 交互习惯/查询模式等系统状态仍需独立存储

---

#### K4: Dream Evolver 知识写入

**当前实现**：dream-evolver 从对话中提取错误经验/成功经验/工具方言/用户画像，写入 KG。

**期望目标**：经验/画像作为文档插入 LightRAG，或用 `acreate_entity()`/`acreate_relation()` 直接写入图。

**差距**：
- Dream Evolver 的输出是结构化的（实体+关系+置信度），LightRAG 的 `acreate_entity()`/`acreate_relation()` 可以直接接收
- 置信度映射：当前 0-1.0 → LightRAG 没有原生的置信度字段，需要存在描述或元数据中

---

#### K5: KG 批量回填同步

**当前实现**：KGSync 每6小时从 photos.db 和 vectors.db 同步数据到 KG。

**期望目标**：LightRAG 统一存储，不需要 vectors.db→KG 的同步。photos.db→LightRAG 的同步仍需保留。

**差距**：
- `_sync_vectors_db()` 整个流程可以废弃
- `_sync_photos_db()` 需要改为调用 LightRAG 的 `ainsert()` 或 `ainsert_custom_kg()`
- 孤立节点清理逻辑需要适配

---

#### K6: 图可视化 API

**当前实现**：`niu_api/kg_api.py` 暴露 9 个 FastAPI 端点，调用 kg-server 的 snapshot/stats/hubs/explore/path 等工具。

**期望目标**：用 LightRAG 的 `get_knowledge_graph()`、`get_graph_labels()` 等方法替代。

**差距**：
- LightRAG 的图查询接口不如当前 kg-server 丰富（没有 surprising_connections、hub_entities 等）
- 需要在 LightRAG 之上实现这些分析功能，或直接操作 NetworkX 图
- 前端可视化接口需要适配新的数据格式

---

#### K7: 知识探索引导

**当前实现**：向量检索命中文档后，在系统提示词中注入"可使用 kg-server/explore_node 查询关联信息"的引导。

**期望目标**：LightRAG 的 hybrid/mix 模式已经包含了图扩展，不需要额外的探索引导。

**差距**：
- 这个引导机制可以简化或移除
- 但 LLM 仍可能需要直接查询图的能力（如精确查找某个实体），需要保留部分 KG 工具

---

#### K8: LLM 直接调用 KG 工具

**当前实现**：LLM 在对话中直接调用 kg-server 的 19 个工具（explore_node、query_graph 等）。

**期望目标**：用 LightRAG 的查询接口替代大部分只读工具，保留少量写入工具。

**差距**：
- 只读工具（explore_node、find_path、graph_stats 等）→ LightRAG 的 `query_data()`、`get_knowledge_graph()` 等
- 写入工具（create_entity、link_entities 等）→ LightRAG 的 `acreate_entity()`、`acreate_relation()` 等
- query_graph（自由 Cypher）→ LightRAG 没有等价接口，需要直接操作 NetworkX 图或放弃

---

#### C1: 记忆存取（memory-server）

**当前实现**：memory-server 共享 vectors.db，存储 L0/L1/L2 记忆记录，用 cosine similarity 检索。

**期望目标**：记忆作为文档插入 LightRAG 的 chunks_vdb，或保留独立存储。

**差距**：
- 记忆的 L0/L1/L2 层级是核心设计，LightRAG 的分块策略不同
- 记忆检索需要精确的 level 过滤，LightRAG 不支持
- 建议：记忆系统保留独立 SQLite 存储，不迁移到 LightRAG

---

### 场景分类：必须迁移 / 可迁移 / 保留独立

| 分类 | 场景 | 理由 |
|------|------|------|
| **必须迁移** | V1(动态注入), K1(文档入库), K2(实体提取), K5(KG同步) | 核心检索和知识构建流程，融合的主要收益点 |
| **可迁移** | V2(工具生命周期), V3(Skill同步), V5(MCP工具索引), V6(系统手册), V7(照片存储), V8(递归查询), V9(清理), K3(KG丰富化), K4(Dream写入), K6(可视化), K7(探索引导), K8(LLM调用KG) | 用 LightRAG mode="local" 替代纯向量场景，mode="global" 替代纯图场景 |
| **保留独立** | V4(交互习惯→纠错文档+ainsert), C1(记忆→脑图方案) | 原先标记为保留独立的场景已全部规划了迁移方案 |

**核心结论**：通过 LightRAG 的 mode 参数（local/global/hybrid/mix），所有检索场景都可以统一到 LightRAG。V8递归检索已由图谱遍历替代，V4交互习惯改为纠错文档+ainsert，C1记忆改为脑图方案。

---

## 六、实施步骤

### 第一步：需求分解 ✅ 已完成

### 第二步：方案设计（下一步）

针对"必须迁移"和"可迁移"的场景，派子 Agent 设计对接 LightRAG 的完整方案：
- LightRAG API 映射
- 数据迁移方案
- MCP 工具接口设计
- 向后兼容处理

### 第三步：实施（待方案确认后）

1. Phase 1 — 旁路验证：恢复代理 + 验证 LightRAG 通过代理正常调用 LLM
2. Phase 2 — 替换核心：新建 lightrag-server，暴露 MCP 工具接口
3. Phase 3 — 数据迁移：现有数据导入 LightRAG

## 七、风险与注意事项

| 风险 | 应对 |
|------|------|
| L1/L2 层级概念在 LightRAG 中不存在 | 需设计映射方案（chunk 级别标记？） |
| 现有 27 个 MCP 工具接口需逐一映射 | 部分可合并，部分需保留 |
| 数据迁移可能丢失部分关系 | 先备份，迁移后验证 |
| LightRAG 的 LLM 调用频率可能较高 | 代理层可加缓存/限流 |
| 交互习惯/工具生命周期等系统概念不适合放入 LightRAG | 保留独立 SQLite 存储 |

## 八、参考资料

- LightRAG 源码：`E:\tools\LightRAG`
- 当前向量库：`E:\tools\ai-bot\mcp-servers\vector-store\src\`
- 当前知识图谱：`E:\tools\ai-bot\mcp-servers\kg-server\src\`
- 大模型代理（已删除）：`git show fc8ec3e~1:niu_api/page_agent_proxy.py`
- 当前 Agent Handler：`E:\tools\ai-bot\agent\generic\handler.py`
- 向量检索适配器：`E:\tools\ai-bot\agent\vector_search.py`
- KG 扫描器：`E:\tools\ai-bot\agent\injector\kg_scanner.py`
- KG 同步器：`E:\tools\ai-bot\agent\injector\kg_sync.py`
- 工具生命周期：`E:\tools\ai-bot\agent\tool_lifecycle.py`
