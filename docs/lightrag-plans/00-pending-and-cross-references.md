# LightRAG 融合工程 — 未尽事宜与牵连关系

> 最后更新：2026-04-24
> 用途：记录子工程完工后的牵连影响、跨工程依赖、需注意的副作用

## 跨工程依赖关系

```
05 (LLM代理+Embedding) ← 所有工程的基础，必须最先实施
    ↓
01 (数据注入与检索) ← 依赖05的代理和Embedding
02 (文档入库流水线) ← 依赖01的注入机制
03 (记忆脑图) ← 依赖01的注入机制 + 02的流水线
04 (MCP工具接口) ← 依赖01的检索接口 + 02的流水线
06 (脑区激活) ← 依赖01-05全部完成，LightRAG作为统一底座
```

**实施顺序建议**：05 → 01 → 02 → 03/04（并行）→ 06

---

## Phase 01-05 实施完成记录（2026-04-24）

### runner.py 重写
- `_inject_dynamic_resources()` 从 vector_search 改为 LightRAG 主检索
- 新增 `search_multi_lightrag()` → 按 entity_type 分桶返回
- 新增 `_build_tool_scores_from_lightrag()` → 排名代理分数（top-5=70, 6-10=55, 11-20=40）
- 新增 `_search_tool_signal_skills_lightrag()` → LightRAG 替代 vector_search.search()
- 新增 `_format_lightrag_entities_for_prompt()` → 格式化 LightRAG entity dict
- `lightrag_available` 标志避免冗余调用

### SkillSync 改造
- `_sync_skill()` 只写 LightRAG，不写 vectors.db
- `_delete_skill()` 只从 LightRAG 删除
- Ghost 检测用 `list_entities(entity_type="skill", limit=500)`
- `_load_existing_skills()` 从 LightRAG 加载已有 skill

### MCP 工具注册改造
- `register_mcp_tool()` / `register_mcp_tools_batch()` 只写 LightRAG
- 已删除 `_register_to_vector_db()` 死代码

### 代码审查
- 两轮迭代审查，修复所有 CRITICAL/IMPORTANT 问题
- 关键修复：list_entities dict-vs-list、entity key "id" vs "name"、lightrag_available 初始化、silent except、interaction_habit 死映射

### 测试
- 148 tests passed
- 17 个新测试覆盖 LightRAG 迁移逻辑
- test_tool_hit_integration.py 重写为 LightRAG mock

---

## Phase 01-05 完工后的牵连

### vectors.db 清理（Phase 06 前需处理）
- **影响**：`niu_api/compat.py` 的 `/api/vector/stats` 和 shutdown 路径仍引用 vector_search
- **牵连**：一旦 vector_search.py 被移除，这些路径会崩溃
- **注意**：Phase 06 启动前需清理或替换这些残留引用

### autonomous_explorer 仍引用 vector_search
- **影响**：`agent/autonomous_explorer.py` 仍 import vector_search
- **牵连**：执行该路径时会用旧 vectors.db 而非 LightRAG
- **注意**：Phase 06 可一并处理

### format_resources_for_prompt() 死代码
- **影响**：`agent/runner.py` 的 `format_resources_for_prompt()` 依赖旧 SearchResult 接口，从未被调用
- **牵连**：新代码用 `_format_lightrag_entities_for_prompt()` 替代
- **注意**：Phase 06 启动前可清理

### test_tool_lifecycle.py 仍 mock vector_search
- **影响**：测试注入 mock vector_search module 到 sys.modules
- **牵连**：runner.py 已不再 import vector_search，mock 无效但无害
- **注意**：Phase 06 可清理

### API contract key 不一致
- **影响**：`list_entities()` 返回 `"id"` key，search APIs 返回 `"entity_name"` key
- **牵连**：下游消费者需分别处理两种 key
- **注意**：Phase 06 可统一为 `entity_name`（LightRAG canonical key）

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

---

## 子工程 03 完工后的牵连

### memory-server 替换
- **影响**：`mcp-servers/memory-server/` 的 remember/recall 工具委托到脑图
- **牵连**：handler.py 已实现双写（memory-server + brain graph），过渡期并行
- **注意**：user_memory 工具（操作 memory.json）不受影响，与脑图是不同层级

### 记忆衰减机制
- **影响**：脑图的遗忘曲线需要定时任务执行衰减
- **牵连**：需要新增定时任务或在现有定时任务中增加衰减逻辑
- **注意**：衰减频率和参数需要调优，Phase 06 会完善

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

---

## Phase 06 启动前提

Phase 06（脑区激活）依赖 Phase 01-05 全部完成。现在所有前置条件已满足：

- ✅ LightRAG 作为统一检索底座（替代 vector_search）
- ✅ SkillSync 写入 LightRAG（替代 vectors.db）
- ✅ MCP 工具注册写入 LightRAG（替代 vectors.db）
- ✅ 脑图基础架构已就位（BrainGraph + brain_api）
- ✅ 148 tests passed，两轮代码审查通过

Phase 06 启动前建议清理的遗留项：
- `niu_api/compat.py` 的 vector_search 残留引用
- `agent/autonomous_explorer.py` 的 vector_search 引用
- `agent/runner.py` 的 `format_resources_for_prompt()` 死代码
- `tests/test_tool_lifecycle.py` 的 vector_search mock