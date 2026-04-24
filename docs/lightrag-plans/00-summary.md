# LightRAG 迁移方案总览

## 目标

将动态注入架构从 `vector_search` / `vectors.db` 迁移到 LightRAG 知识图谱作为主检索底座。

## 阶段状态

| 阶段 | 文档 | 状态 | 说明 |
|------|------|------|------|
| Phase 01 | 01-phase1-audit-report.md | ✅ 已完成 | 审计报告，已归档 |
| Phase 02 | 02-lightrag-retrieval-migration.md | ✅ 已完成 | 图检索替代向量检索 |
| Phase 03 | 03-skill-sync-migration.md | ✅ 已完成 | SkillSync 改为 LightRAG 主写入 |
| Phase 04 | 04-mcp-tool-registration-migration.md | ✅ 已完成 | MCP 工具注册改为 LightRAG 主写入 |
| Phase 05 | 05-test-and-validation.md | ✅ 已完成 | 测试验证（148 tests passed） |
| Phase 06 | 06-brain-region-activation.md | 🔜 待启动 | 脑区激活机制 |

## Phase 01-05 实施结果

**Commit**: `75d7464` feat: migrate dynamic injection from vector_search to LightRAG as primary retrieval
**Code Review Fix**: `7be6c86` fix: code review fixes for LightRAG migration

### 核心变更

1. **`agent/runner.py`** — `_inject_dynamic_resources()` 重写
   - LightRAG `search_multi_lightrag()` 替代 `vector_search.search_multi()` 作为主检索
   - 无 fallback，LightRAG 不可用时仅保留 `interaction_habits` + `brain memories`
   - 新增辅助方法：`_apply_query_patterns()`, `_search_tool_signal_skills_lightrag()`, `_build_tool_scores_from_lightrag()`, `_format_lightrag_entities_for_prompt()`

2. **`agent/injector/sync.py`** — SkillSync 改为 LightRAG 主写入
   - `_sync_skill()` 只调用 `_inject_skill_to_lightrag()`，不写 vectors.db
   - `_delete_skill()` 只从 LightRAG 删除
   - Ghost 检测改用 `list_entities(entity_type="skill")`

3. **`niu_api/injector.py`** — MCP 工具注册改为 LightRAG 主写入
   - `register_mcp_tool()` 和 `register_mcp_tools_batch()` 只写 LightRAG
   - 已删除 `_register_to_vector_db()` 死代码

4. **`niu_api/internal/lightrag_adapter.py`** — 新增 `search_multi_lightrag()`
   - 一次 `query_data()` 调用，按 `entity_type` 分组返回
   - `_ENTITY_TYPE_TO_CATEGORY` 映射：skill / mcp_tool / knowledge

5. **测试** — 148 tests passed
   - `test_lightrag_retrieval_migration.py` — 17 个新测试
   - `test_tool_hit_integration.py` — 重写为 LightRAG mock

### 已知遗留项（不在 Phase 01-05 范围）

- `vector_cleanup.py` 仍引用 vector_search（独立清理工具，非注入路径）
- `experience_summarizer.py` 有过时注释
- `list_resources()` 和 `delete_resource()` 仍读写 vectors.db（独立端点）
- memory-server L0/L1/L2 recall 迁移（Phase 06 可处理）
- photo-server vectors.db 依赖解除（Phase 06 可处理）

## Phase 06: 脑区激活

Phase 06 依赖 Phase 01-05 全部完成（LightRAG 作为统一底座），现在可以启动。

详见 [06-brain-region-activation.md](06-brain-region-activation.md)
