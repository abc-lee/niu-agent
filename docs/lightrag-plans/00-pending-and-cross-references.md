# LightRAG 融合工程 — 未尽事宜与牵连关系

> 最后更新：2026-04-23
> 用途：记录子工程完工后的牵连影响、跨工程依赖、需注意的副作用

## 跨工程依赖关系

```
05 (LLM代理+Embedding) ← 所有工程的基础，必须最先实施
    ↓
01 (数据注入与检索) ← 依赖05的代理和Embedding
02 (文档入库流水线) ← 依赖01的注入机制
03 (记忆脑图) ← 依赖01的注入机制 + 02的流水线
04 (MCP工具接口) ← 依赖01的检索接口 + 02的流水线
```

**实施顺序建议**：05 → 01 → 02 → 03/04（并行）

---

## 子工程 01 完工后的牵连

### 初始化脚本改造
- **影响**：`scripts/init_vector_db.py` 和 `scripts/inject_system_manual.py` 需要完全重写
- **牵连**：所有依赖初始化脚本的启动流程需要适配
- **注意**：初始化脚本改造是关键，只要注入正确，运行时逻辑基本不用改

### Embedding维度变更
- **影响**：从384维→1024维，所有现有向量数据失效
- **牵连**：memory-server 共享 vectors.db，维度变更后记忆数据也失效
- **注意**：需要数据迁移脚本 `scripts/reindex_vectors.py`，迁移期间双模型并行

### 交互习惯改造
- **影响**：`handler.py` 的 `tool_after_callback()` 不再写 SQLite，改为更新 md 文件
- **牵连**：`vector_search.py` 的 `search_interaction_habits()` 需要替换为 LightRAG 查询
- **注意**：纠错文档格式需要稳定，变更后要触发 LightRAG 重新索引

### Skills注入方式变更
- **影响**：`agent/injector/sync.py` 的 SkillSync 不再 upsert 向量库，改为 ainsert_custom_kg()
- **牵连**：Skills 的触发词/标签需要预定义为实体关系，不能依赖 LLM 提取
- **注意**：Skill 文件格式可能需要调整，增加结构化元数据区域

### MCP工具注入方式变更
- **影响**：`niu_api/injector.py` 的工具描述注册不再写向量库，改为 ainsert_custom_kg()
- **牵连**：`agent/tool_lifecycle.py` 的评分机制需要适配 LightRAG 的 local 模式检索
- **注意**：USED_FOR 和 OFTEN_WITH 关系需要从工具描述和历史数据中挖掘

---

## 子工程 02 实施完成记录（2026-04-23）

### KGSync → LightRAGSync 委托
- `agent/injector/kg_sync.py` 的 `KGSync` 类改为委托 `lightrag_sync.LightRAGSync`
- `_sync_vectors_db()` 废弃，不再同步向量库
- `_sync_photos_db()` 改为调 LightRAG `ainsert_custom_kg()` 注入人物/地点实体
- 新增 co_occurrence 关系追踪（`USED_FOR`/`OFTEN_WITH`），用 pair_key 去重
- delta 追踪：status JSON 记录已同步的 photo_ids/person_ids/co_occ_ids，避免重复处理

### KGScanner 禁用
- `agent/injector/kg_scanner.py` 的 `KGScanner` 类标记废弃，`scan()` 返回空结果
- 实体提取改由 LightRAG `ainsert()` 自动完成

### kg_api → LightRAGAdapter
- `niu_api/kg_api.py` 的端点改用 `LightRAGAdapter` 而非 KuzuDB
- `entities()` 和 `explore()` 通过 LightRAG `aquery_data()` 实现

### notes_api → ainsert
- `niu_api/notes_api.py` 的 `sync_note_to_kg` 改用 `call_async(rag.ainsert(prefixed))`
- `BackgroundTasks.add_task` 改用 `asyncio.to_thread` 包装同步函数，避免阻塞 ASGI 事件循环

### mcp_loader 移除 kg-server
- `agent/mcp_loader.py` 不再加载 kg-server 模块
- `config/mcp-servers.yaml` 中 kg-server 标记为 `disabled: true`

### lightrag_pipeline 增强
- `niu_api/internal/lightrag_pipeline.py` 添加 `threading.Semaphore` 背压控制
- `_evict_completed_tasks` 同时清理 completed 和 failed 状态
- `update_document` 在 delete 成功但 insert 失败时追踪 failed task，防止静默数据丢失

### 代码审查
- 三轮迭代审查，修复所有 CRITICAL/HIGH 问题
- 关键修复：tuple arity 不匹配、_save_status 参数缺失、Semaphore 死代码、测试断言假阳性

---

## 子工程 02 完工后的牵连

### KGScanner 废弃
- **影响**：`agent/injector/kg_scanner.py` 整个文件废弃
- **牵连**：`config/agents/entity-extractor.md` 子Agent定义废弃
- **注意**：pending→processing→completed 状态追踪改为 LightRAG 的 DocStatus

### KGSync 改造
- **影响**：`agent/injector/kg_sync.py` 的 `_sync_vectors_db()` 废弃，`_sync_photos_db()` 改为调 LightRAG
- **牵连**：6小时定时任务的逻辑需要重写
- **注意**：孤立节点清理逻辑需要适配 LightRAG 的存储结构

### Dream Evolver 适配
- **影响**：`config/agents/dream-evolver.md` 的输出格式需要适配 ainsert_custom_kg()
- **牵连**：KuzuDB 字段名→LightRAG 字段名的映射
- **注意**：置信度字段在 LightRAG 中没有原生支持，需存在描述或元数据中

### photo-server 适配
- **影响**：photo-server 不再直接写 vectors.db 和 KuzuDB，改为调 LightRAG
- **牵连**：照片入库流程从"写DB+标记pending"变为"调ainsert"
- **注意**：人脸识别结果（人名）需要用 ainsert_custom_kg() 精确注入

---

## 子工程 03 完工后的牵连

### memory-server 替换
- **影响**：`mcp-servers/memory-server/` 整个模块可能被 brain-server 替代
- **牵连**：remember/recall 工具接口需要映射到脑图操作
- **注意**：L0/L1/L2 层级概念需要映射到脑图的实体权重和关系类型

### 向量库依赖解除
- **影响**：memory-server 不再共享 vectors.db
- **牵连**：vectors.db 可以完全废弃（所有场景都已迁移到 LightRAG）
- **注意**：需要确认没有其他模块直接读 vectors.db

### 记忆衰减机制
- **影响**：脑图的遗忘曲线需要定时任务执行衰减
- **牵连**：需要新增定时任务或在现有定时任务中增加衰减逻辑
- **注意**：衰减频率和参数需要调优

---

## 子工程 04 完工后的牵连

### MCP工具接口变更
- **影响**：27个工具→12个工具，LLM的工具选择模式需要重新学习
- **牵连**：`config/agents/niu.md` 的工具列表需要更新
- **注意**：过渡期需要旧工具名别名，避免 LLM 调用失败

### 图可视化API重写
- **影响**：`niu_api/kg_api.py` 的9个端点需要用 LightRAG 数据重写
- **牵连**：前端 Electron 的 KG 窗口需要适配新的 API 格式
- **注意**：surprising_connections、hub_entities 等分析功能需要客户端实现

### handler.py 调度逻辑
- **影响**：`agent/generic/handler.py` 的 dispatch() 需要适配新的工具名
- **牵连**：所有硬编码的工具名引用需要更新
- **注意**：向后兼容别名期间，新旧工具名都能调度

---

## 子工程 05 完工后的牵连

### LLM代理恢复
- **影响**：`niu_api/page_agent_proxy.py` 恢复为 `niu_api/llm_proxy.py`
- **牵连**：`niu_api/__main__.py` 需要注册新路由
- **注意**：需要新增 /v1/embeddings 端点（LightRAG 会调用）

### Embedding模型切换
- **影响**：`niu_api/internal/embedding.py` 从 MiniLM-L12 切换到 bge-m3
- **牵连**：模型文件从 ~120MB 增大到 ~570MB，需要下载
- **注意**：首次启动时自动下载，需要网络连接和磁盘空间

### Reranker加载
- **影响**：新增 ~560MB 的 reranker 模型（懒加载）
- **牵连**：总内存占用增加约 1.1GB（embedding 570MB + reranker 560MB）
- **注意**：懒加载+空闲卸载可以控制内存峰值

---

## 全局性未尽事宜

### 数据迁移
- 现有 vectors.db 和 knowledge.db 的数据需要完整迁移到 LightRAG
- 迁移前必须备份，迁移后需要验证数据完整性
- Embedding 维度变更意味着所有向量需要重新计算

### 向后兼容过渡期
- 建议设置过渡期，新旧系统并行运行
- 过渡期结束后再移除旧代码
- 旧 MCP 工具名保留别名直到过渡期结束

### 性能验证
- LightRAG 的 LLM 调用频率可能较高（实体提取、查询）
- 需要监控代理层的 LLM 调用量和延迟
- 可能需要加缓存/限流

### 前端适配
- KG 窗口的可视化 API 格式变更
- 需要确认前端是否有其他直接调用 vector-store/kg-server 的地方
