# LightRAG 知识图谱数据一致性检查与修复（重做版）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task.

**Goal:** 把当前"欺骗用户"的修复程序（空文件当损坏、集合比对而非因果链、repair 没跑完就报失败）改造为基于因果链的数据一致性检查与修复。

**Architecture:** 以 `kv_store_full_docs` + `kv_store_text_chunks` 为真相源，其他 10 个文件都是派生数据。检查 = 验证每个引用的 key 在被引用方存在（8 类因果链断裂）。修复 = 从真相源按依赖链重建。启动门控 = unrecoverable 或重建后仍断裂则拒绝启动。

**Tech Stack:** Python + nano-vectordb + networkx GraphML + bge-base-zh embedding

---

## 核心设计原则（不可违反）

1. **空文件不是错**——新用户/刚清空时所有文件都空，因果链自洽，合法启动
2. **不一致才是错**——引用的 key 在被引用方不存在 = 因果链断裂 = 损坏
3. **不报假失败**——repair 期间全局标志 `_repairing=True`，`get_lightrag` 静默返回 None，不报 critical 日志
4. **不做假数据**——修不好就 status=error 不写文件，让 check 仍检测到损坏，拒绝启动
5. **真相源不可重建**——`full_docs` / `text_chunks` 损坏 = unrecoverable，拒绝启动

---

## 8 类因果链断裂检查（核心）

每项检查一个文件的引用是否完整。**不检查文件大小/是否空**，只检查引用悬空。

| # | 检查 | 引用方 → 被引用方 | 严重级别 |
|---|------|-------------------|----------|
| 1 | entity_chunks 引用悬空 | kv_store_entity_chunks 的 key(entity_name) 是否都在 GraphML node 里 | major |
| 2 | relation_chunks 引用悬空 | kv_store_relation_chunks 的每个 key 用 `parse_relation_chunk_key`（`<SEP>` 分隔）拆分为 (src, tgt)，检查 GraphML 中是否存在 edge (src, tgt) 或 (tgt, src) | major |
| 3 | text_chunks 文档悬空 | kv_store_text_chunks 的 full_doc_id 是否都在 full_docs 里 | critical（真相源断裂） |
| 4 | text_chunks 缓存悬空 | kv_store_text_chunks 的 llm_cache_list（如果字段不存在视为空列表，通过）引用的 cache_key 是否都在 llm_response_cache 里 | minor（缓存丢失可重建） |
| 5 | doc_status chunks 悬空 | kv_store_doc_status 的 chunks_list 引用的 chunk_id 是否都在 text_chunks 里 | major |
| 6 | vdb_entities 向量缺失 | GraphML 每个 node 的 `ent-{md5(name)}` 是否都在 vdb_entities.data 里 | major |
| 7 | vdb_relationships 向量缺失 | GraphML 每个 edge 用 `make_relation_vdb_ids(src, tgt)` 生成候选 ID 列表（含正序 `rel-{md5(sorted_src+sorted_tgt)}` 和逆序 `rel-{md5(sorted_tgt+sorted_src)}`），检查列表中是否至少一个 ID 在 vdb_relationships.data 里 | major |
| 8 | vdb_chunks 向量缺失 | text_chunks 每个 chunk 的 `chunk-{md5(content)}` 是否都在 vdb_chunks.data 里 | major |
| 9 | GraphML edge 端点悬空 | GraphML 每个 edge 的 source 和 target 是否都在 GraphML node 集合中存在 | major |
| 10 | vdb_relationships 端点悬空 | vdb_relationships 每条记录的 src_id / tgt_id 是否都在 GraphML node 中存在 | major |

**文件级 critical**（JSON 解析失败/matrix 维度不匹配）= 文件本身损坏，不是引用悬空。

**所有文件不存在或为空 dict** = ok=True，新用户合法启动。检查函数统一容错：文件不存在 = 空数据 = 通过（无引用即无悬空）。

**delete 中途失败**导致的不一致（如 full_docs 已删但 doc_status 未删、GraphML 残留 edge 引用已删 node）由 10 项因果链检查检测，由 repair_all 按依赖链修复。

---

## 修复策略（按依赖链重建）

```
full_docs (真相源，不可重建)
  ↓ chunking
text_chunks (真相源，不可重建)
  ↓ 从 text_chunks.full_doc_id 反向构建 chunk→doc 映射
doc_status (chunks_list 从 text_chunks 的 key 派生)
  ↓ 重跑 LLM extract（用 llm_response_cache 重放；不调 entity extraction LLM，但 summary 阶段可能调 LLM 如果 summary 缓存未命中）
GraphML (图谱结构)
  ↓ embedding
vdb_entities + vdb_relationships (实体/关系向量)
  ↓ embedding text_chunks
vdb_chunks (chunk 向量)
  ↓ 从 GraphML source_id 提取
entity_chunks + relation_chunks (chunk 引用)
  ↓ 从 GraphML source_id → chunk→doc 映射（依赖 doc_status.chunks_list）
full_entities + full_relations (文档级索引)
```

**修复函数**（每个文件一个，按依赖链顺序调用）：

1. `repair_text_chunks` — 从 full_docs 重新 chunking 重建（如果 full_docs 完好）。**注意**：如果 chunk_size 配置变更，重新 chunking 会产生不同 chunk_id，导致所有下游引用失效 → 报 unrecoverable
2. `repair_doc_status` — 从 text_chunks 派生 chunks_list（按 full_doc_id 分组）+ 从 full_docs 派生 status（PROCESSED 如果 GraphML 有数据，否则 PENDING）
3. `repair_graphml` — 从零重建（GraphML 完全损坏时走 `apipeline_process_enqueue_documents`）：
   - **先 drop 旧 GraphML**：调 `chunk_entity_relation_graph.drop()`（networkx_impl.py:607-629）或手动删除 GraphML 文件 + 重置内存 graph，避免残留损坏数据
   - repair 前将所有 PROCESSED 文档状态改为 PENDING（触发重处理）
   - extract 阶段：`use_llm_func_with_cache` 的 `cache_type="extract"` cache 命中时不调 LLM（utils.py:2058-2067 自带能力）
   - summary 阶段：monkeypatch `global_config["force_llm_summary_on_merge"]` 设成 999999，让 `_handle_entity_relation_summary`（operate.py:221-224）尽量走 `separator.join(current_list)` 分支。**已知局限**：map-reduce 路径（operate.py:243-301，total_tokens > summary_context_size=12000 时触发）和 total_tokens >= summary_max_tokens=1200 路径仍可能调 LLM。这些路径依赖 `cache_type="summary"` cache 命中才不调 LLM
   - **不保证零 LLM 调用**：如果 `llm_response_cache` 中 summary cache key 丢失，summary 阶段会调 LLM。用户需承担少量 LLM 调用费用。如果用户不接受 LLM 调用，可选择 unrecoverable（手动从备份恢复 GraphML）
   - llm_response_cache 损坏（extract cache miss）则不可恢复 → unrecoverable=True
   - **中间窗口期**：`apipeline_process_enqueue_documents` 按文档逐个重新 chunking，每个文档完成后通过 `_insert_done()`（lightrag.py:2181）持久化 text_chunks 到磁盘。`llm_cache_list` 被初始化为 `[]` 后由 `update_chunk_cache_list`（operate.py:3126-3133）恢复。repair 期间 `_repairing=True` 保护 get_lightrag，check_all 直接读文件——但 run_repair_on_user_request 内部只在 repair 完成后才调 check_all，中间窗口期不会误报
4. `repair_vdb_chunks` — 遍历 text_chunks 重新 embedding 重建
5. `repair_vdb_entities` — 遍历 GraphML node 重新 embedding 重建
6. `repair_vdb_relationships` — 遍历 GraphML edge 重新 embedding 重建（用 `make_relation_vdb_ids` 生成正序 ID）
7. `repair_entity_chunks` — 从 GraphML node source_id 提取重建（source_id 是 `<SEP>` 分隔的 chunk_id 列表）
8. `repair_relation_chunks` — 从 GraphML edge source_id 提取重建，key 用 `make_relation_chunk_key(src, tgt)` 生成
9. `repair_full_entities` — 从 GraphML source_id → chunk→doc 映射重建（chunk→doc 映射依赖 doc_status.chunks_list，已在步骤 2 修复）
10. `repair_full_relations` — 同上
11. `repair_llm_response_cache` — 不可重建，清空（minor，允许降级启动）

**repair_all 顺序**：按依赖链从上到下，先修上游再修下游。

**失败处理**：
- 真相源（full_docs/text_chunks）损坏 → unrecoverable=True，status=error
- GraphML 损坏且 llm_response_cache 也损坏 → unrecoverable=True（无法重建图谱）
- embedding 失败 >10% → status=error，不写文件
- 其他失败 → status=error，记录原因

---

## 启动门控（三级）

- **A 级（unrecoverable / critical）**：真相源损坏 / GraphML 不可恢复 / 文件 JSON 解析失败 → 拒绝启动，返回 None
- **B 级（major）**：因果链断裂（向量缺失/引用悬空）→ 拒绝启动，需用户选"尝试修复"
- **C 级（minor）**：仅缓存损坏 → 允许降级启动，日志警告

**repair 期间保护**（多重保护，避免误报 + 并发竞争）：
1. `run_repair_on_user_request` 开始时设 `_repairing=True`，用 `try/finally` 确保异常路径也清除
2. `get_lightrag` 检测到 `_repairing` 静默返回 None（不报 critical 日志），避免 SkillSync 后台轮询误报
3. repair 期间置 `_rag_instance = None`，让新调用方拿不到 LightRAG 实例（避免新 ingest 请求并发写文件竞争）
4. **pipeline busy 检查**（避免中断已提交的 ingest）：
   - 实现方式：复用 `niu_api/kg_api.py:378-399` 的 `_read_pipeline_busy()` 模式——直接读 `_shared_dicts[ps_key]["busy"]` 不加锁（单进程模式下 GIL 保护字典读，线程安全，零竞争零超时）
   - 等待机制：循环 `_read_pipeline_busy()` + `time.sleep(5)` 直到 busy=False，超时上限 300s
   - 超时处理：如果 300s 后仍 busy，拒绝 repair，返回 `{"status": "error", "message": "pipeline busy 超过 300s，请稍后重试"}`
5. `run_repair_on_user_request` 结束后调 `reset_init_state()` 让下次 `get_lightrag` 重新初始化
6. **已知局限**：已提交到 `_loop` 的 ingest coroutine 无法强制中断，只能靠 pipeline busy 检查等待其完成。repair 期间 check_all 不被调用（run_repair_on_user_request 内部只在 repair 完成后才调 check_all）

---

## Task 分解（6 个 Task，不分太细）

### Task 0: 验证 key 格式 + 读 LightRAG 源码确认

**文件**: 无（只读不改）

实现前先读用户真实数据 + LightRAG 源码，确认 key 格式和函数来源：
- `vdb_relationships.__id__` = `rel-{md5(sorted_src+sorted_tgt)}`（已验证：`rel-0f0a0f2ab29ade1aa4a9437d4d8c6d27` 对应 src=niu, tgt=聊天历史脑区）
- `relation_chunks` key = `{sorted_src}<SEP>{sorted_tgt}`（已验证：`公共服务局<SEP>张伟龙`），`<SEP>` = `GRAPH_FIELD_SEP`（constants.py:44）
- `make_relation_vdb_ids`（utils.py:570-584）返回正序+逆序两个候选 ID
- `make_relation_chunk_key`（utils.py:2947-2950）= `GRAPH_FIELD_SEP.join(sorted((src, tgt)))`
- `parse_relation_chunk_key` 已存在（utils.py:2953-2959），直接 `from lightrag.utils import parse_relation_chunk_key`

- [ ] Step 1: 读用户真实数据确认 key 格式（已验证）
- [ ] Step 2: 读 LightRAG 源码确认函数来源（已验证）
- [ ] Step 3: 在实现中复用 LightRAG 的 `make_relation_vdb_ids` / `make_relation_chunk_key`，自己写 `parse_relation_chunk_key`

### Task 1: 重写 check（因果链检查）

**文件**: `niu_api/internal/lightrag_integrity.py`

删除现有所有 check 函数（check_vdb/check_kv_store/check_graphml/check_entity_sync/check_relationship_sync/check_chunks_sync/check_cross_file/check_vector_quality/check_metadata_fields/check_duplicates 全部删掉）。

重写为 8 项因果链检查 + 文件级 critical 检查。每个检查只验证引用完整性，不检查文件是否空。

- [ ] Step 1: 写 8 个检查函数 + 文件级检查 + check_all 聚合
- [ ] Step 2: 写测试（每项检查一个 PASS 场景 + 一个 FAIL 场景，用 tempfile 隔离）
- [ ] Step 3: 运行测试通过
- [ ] Step 4: 提交

### Task 2: 重写 repair（按依赖链重建）

**文件**: `niu_api/internal/lightrag_repair.py`

删除现有所有 repair 函数。重写 11 个 repair 函数 + repair_all（按依赖链顺序）+ _atomic_write_json + _embed_batch。

- [ ] Step 1: 写 _atomic_write_json + _embed_batch 工具函数
- [ ] Step 2: 写 11 个 repair 函数（每个从对应真相源重建）
- [ ] Step 3: 写 repair_all（按依赖链顺序 + status 协调）
- [ ] Step 4: 写测试（每个 repair 函数一个测试，用 tempfile + monkeypatch _embed_text）
- [ ] Step 5: 运行测试通过
- [ ] Step 6: 提交

### Task 3: 启动门控 + repair 期间保护

**文件**: `niu_api/internal/lightrag_manager.py`

- [ ] Step 1: get_lightrag 三级门控 + `_repairing` 标志保护
- [ ] Step 2: run_repair_on_user_request 设/清 `_repairing` + 用 severity 判 repaired
- [ ] Step 3: 写测试（A/B/C 三级 + repair 期间 get_lightrag 静默）
- [ ] Step 4: 运行测试通过
- [ ] Step 5: 提交

### Task 4: launcher 展示

**文件**: `launcher/src/main.rs`

改造 format_repair_summary：展示 expected/actual/lost + unrecoverable 分级 + minor 警告。

- [ ] Step 1: 改 format_repair_summary
- [ ] Step 2: `./launcher/build.sh` 编译
- [ ] Step 3: 提交

### Task 5: 端到端验证（真实数据 + 备份恢复）

**文件**: `tests/test_lightrag_e2e.py`

**测试前必须**：`cp -r ~/.niu/lightrag_storage ~/.niu/lightrag_storage.e2e-bak-$(date +%s)` 完整备份。测试后从备份恢复 + 校验完整性（MD5 对比）。损坏脚本支持 `FORCE_YES=1` 非交互。

9 个场景：
1. vdb_entities 截断 → 从 GraphML 重建
2. vdb_chunks 截断 → 从 text_chunks 重建
3. GraphML 损坏 → 从零重建（改 doc_status 为 PENDING + extract cache 重放 + summary 禁用 LLM）
4. text_chunks 损坏 → unrecoverable，拒绝启动
5. full_docs 损坏 → unrecoverable，拒绝启动
6. 新用户空数据（文件不存在或空 dict）→ 正常启动（不报错，critical=0, major=0, minor=0, ok=True）
7. entity_chunks 引用悬空 → 从 GraphML 重建
8. `kv_store_llm_response_cache.json` 损坏 → 清空（minor）→ 降级启动
9. 修复成功后 LightRAG 能初始化 + delete 中途失败导致的不一致 → repair 后恢复一致

- [ ] Step 1: 做完整备份
- [ ] Step 2: 写 9 个 e2e 测试
- [ ] Step 3: 运行测试通过
- [ ] Step 4: 校验恢复完整性（MD5 对比）
- [ ] Step 5: 清理备份
- [ ] Step 6: 提交

---

## 每步验证

每个 Task 完成后：
1. `python -m py_compile niu_api/internal/lightrag_*.py` 语法检查
2. `python -m pytest tests/test_lightrag_*.py -v` 测试通过
3. `python -m pyright niu_api/internal/lightrag_*.py` 无新 error
4. 提交

## 约束

1. **测试用真实数据**：e2e 测试用 `~/.niu/lightrag_storage`，但必须先 `cp -r` 完整备份，测完从备份恢复 + MD5 校验
2. **单元测试用 tempfile 隔离**：不碰用户真实数据
3. **Rust 编译用 `./launcher/build.sh`**：不用 `cargo build`
4. **commit 不加 --no-verify**
5. **修复程序代码委托子 Agent 写**：我作为协调者把控设计，不自己改代码
