# LightRAG 数据修复重构：GraphML 为唯一真相源 + cache original_prompt 主补充 + full_docs fallback v8 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 LightRAG 数据修复逻辑重构为"GraphML 是唯一真相源 + full_docs/cache 是日志类型全量辅助文档 + 修复第一步只保留 3 真相源 + 其他 9 派生文件全删除 + 按 GraphML 引用按需提取重建"。3 真相源任一损坏即报修复失败，全部完好时只重建 9 个派生文件，真相源一根毫毛不动。

---

## 铁律（100% 正确理解 v8）

### 用户铁律（原话不改）

1. **修复程序的第一步，只保留三个真相源文件，其他所有文件全删除**
   - 3 真相源 = `graph_chunk_entity_relation.graphml` + `kv_store_full_docs.json` + `kv_store_llm_response_cache.json`
   - 其他 9 个派生文件全删除（不备份不复制，直接删光）

2. **GraphML 是唯一真相源**——里面有多少条就恢复多少条。full_docs + cache 是**日志类型的全量辅助文档**。恢复时 GraphML 里某条信息不全，从这两个文件找最后一条匹配记录补充。多条匹配取 create_time 最大

3. **所有含有写这三个真相源文件的代码段和程序全部删除，全要删光**

4. **所有不在 GraphML 文件中读取信息做恢复的操作，全部删光**

### v8 核心原则（铁律 2 正确版）

**GraphML 是唯一真相源：**
- GraphML 里有多少条就恢复多少条
- 活跃 chunk_id 集合 = GraphML 所有 node 的 d3 source_id + 所有 edge 的 d10 source_id，<SEP> 分隔
- 脑区节点 = node 的 d1 entity_type == "brainregion"（8 个）

**full_docs + cache 是日志类型全量辅助文档：**
- 它们是历史日志，含所有历史版本/已删实体 entry
- **绝对不能把 full_docs/cache 当真相源重放覆盖 GraphML**
- 它们的作用：GraphML 里某条信息不全时，按需提取补充

**cache 的 original_prompt 是主补充源（v8 核心纠正）：**
- cache 的 `original_prompt` 字段含 LLM extraction 调用时的完整 prompt
- prompt 含 `---Task---` 等指令 + ` ``` ` 之间的 chunk 原文
- 真实数据验证（2026-07-17）：全部 293 个 extract entry 的 original_prompt 含 **8 个 ```（4 对）**，只有第一对 ``` 之间是 chunk 原文（位置示例：第 1 个 ``` 在 1096，第 2 个在 3289），后续 3 对是 LLM 输出示例
- 正则 `r"```\s*(.+?)\s*```"` + re.DOTALL 非贪婪匹配第一对，能正确提取 chunk 原文（已验证真实数据，非贪婪 `.+?` 不会跨多对 ```）
- 多条 cache entry 同 chunk_id → 取 create_time 最大的 entry（最后录入版本）
- **v8 核心纠正**：cache 是**主补充源**，覆盖大部分活跃 chunk

**cache 的 return 字段不是 chunk 原文：**
- `return` 是 LLM extraction 结果（`entity<|#|>名字<|#|>类型<|#|>描述` 格式）
- **绝对不能把 return 当 chunk 原文**

**full_docs 是 fallback（v8）：**
- cache 找不到时，对每个 doc 用独立 tokenizer chunking，算 chunk_id
- 跟 GraphML 活跃 chunk_id 匹配，多条匹配取 create_time 最大
- 用 `compute_mdhash_id(content, prefix="chunk-")` 算 chunk_id

**脑区节点特殊处理：**
- 脑区 node 的 d1 entity_type == "brainregion"，d3 source_id 是普通 `chunk-{hash}` 格式
- 脑区 chunk 不在 full_docs 里，也不一定能从 cache 找到
- 脑区 chunk 的 content = `{脑区名}: {d2 description 拼接}`
- 脑区 chunk 的 full_doc_id = `brain_{脑区名}`

### 不加任何检测逻辑（铁律 4 严格执行）

- 不加"僵尸脑区检测"
- 不加"已删脑区检测"
- 不加"僵尸 cache entry 检测"
- check_all 只检测 missing/损坏
- _check_truth_sources_intact 只检测 3 真相源是否完好（不检测内容）
- 删除 `repair_graphml` / `repair_brainregion_zombies` / `repair_llm_response_cache` / `repair_graphml_orphan_edges` 函数体（铁律 3）
- 删除 `get_lightrag_for_repair`（铁律 3，调它就是调 apipeline 写真相源）
- 删除 `_rebuild_vdb_matrix`（铁律 3，可能被违规函数调用）
- 删除 `_embed_batch` 的 fallback 分支（调 get_lightrag_for_repair）

---

## 真实数据验证结果（已验证，v8 方案基于这些事实）

### GraphML 状态（干净）
- **2201 nodes + 3725 edges**（16 污染脑区已删）
- 活跃 chunk_id 共 **145 个**（132 来自 node d3 source_id + 127 来自 edge d10 source_id，有重叠）
- 脑区节点 **8 个**（d1 entity_type=="brainregion"）
- 脑区节点 d3 source_id 是 `chunk-{hash}` 格式（普通 chunk_id，不是 `brain_xxx`）
- 脑区节点 d2 description 格式：`{脑区描述}<SEP>brain_meta_region_id:<SEP>brain_meta_size:94<SEP>brain_meta_representative:<SEP>brain_meta_updated_at:...<SEP>brain_meta_priority:medium`

### cache 结构（293 个 extract entry）
- keys: `['return', 'cache_type', 'chunk_id', 'original_prompt', 'queryparam', 'create_time', 'update_time', '_id']`
- `chunk_id`: `chunk-{hash}`
- `create_time`: 时间戳（可排序，多条匹配取最大）
- `original_prompt`: 含 `---Task---` 等指令 + ` ``` ` 之间的 chunk 原文
- 真实数据验证（2026-07-17）：293 个 extract entry 的 original_prompt 全部含 8 个 ```（4 对），只有第一对 ``` 之间是 chunk 原文
- 正则 `r"```\s*(.+?)\s*```"` + re.DOTALL 非贪婪匹配第一对，能正确提取 chunk 原文（已验证真实数据）
- `return`: LLM extraction 结果（`entity<|#|>名字<|#|>类型<|#|>描述` 格式，**不能**当 chunk 原文）
- **117 个 chunk_id 有多条 extract entry**（需按 create_time 取最大）

### full_docs 结构（53 docs）
- 按 doc_id 索引，doc_id 是 `doc-{hash}` 或 `refined:日期:序号` 格式
- value 是 `{content: 文档原文, file_path: 文件路径, create_time: 时间戳}`
- **不含脑区 doc**（脑区不在 full_docs 里）

### text_chunks 现状（92 条，缺 53 个）
- 活跃 145 个 chunk_id
- text_chunks 现有 92 条，缺 53 个
- 脑区 chunk 的 content = `{脑区名}: {description拼接}`，full_doc_id = `brain_{脑区名}`（已验证真实数据）

### 核心算法（基于真实数据）

对每个活跃 chunk_id（145 个）：
1. **cache 优先**（cache 有 293 个 extract entry，覆盖大部分 chunk）：
   - 按 chunk_id 索引 cache，多条匹配取 create_time 最大
   - 从 original_prompt 提取 chunk 原文（正则 ``` 之间）
2. **full_docs fallback**（cache 找不到时）：
   - 对每个 doc 用独立 tokenizer chunking，算出 chunk_id（compute_mdhash_id）
   - 跟 GraphML 的 chunk_id 匹配
3. **脑区节点**（d1=brainregion 的 node）：
   - content = `{脑区名}: {d2 description 拼接}`
   - full_doc_id = `brain_{脑区名}`
4. **三处都没有 → missing**（理论上不该发生，因为 GraphML 有引用说明数据存在）

**注意**：cache 是主补充源（293 entry 覆盖大部分），full_docs 是 fallback。这符合铁律 2"从日志文件提取最后一条匹配记录"。

---

## v7 → v8 核心纠正

| 维度 | v7 | v8 |
|------|-----|-----|
| 主补充源 | full_docs（重新 chunking 反查） | **cache original_prompt 优先**（正则提取 ``` 之间） |
| fallback | cache | **full_docs**（cache 找不到时才 chunking 反查） |
| 多条匹配 | create_time 取最大 | create_time 取最大（同 v7） |
| 脑区 chunk | full_doc_id="brain"，从 text_chunks 查 | full_doc_id=`brain_{脑区名}`，content=`{脑区名}: {d2 description}`，**直接从 GraphML 构造，不查 text_chunks/full_docs/cache** |
| tokenizer/embedding | 调 get_lightrag_for_repair（违规） | **独立加载 TiktokenTokenizer + niu_api.internal.embedding.get_model**（铁律 3） |
| run_repair_on_user_request | 等 SkillSync + 二次 repair + 调 get_lightrag（违规） | **先停 RegionSync（get_region_sync().stop_background_sync）+ _repairing 信号灯兜底 + 不调 get_lightrag/apipeline**（铁律 3） |

---

## Architecture

### 3 真相源（完全不可动）

- **`graph_chunk_entity_relation.graphml`** — **唯一真相源**：当前图谱状态权威清单（实体集、关系集、weight 衰减值、description summary、脑区元数据、活跃 chunk_id 集合）
- **`kv_store_full_docs.json`** — **日志类型全量辅助文档**：所有历史版本文档原文池，按 create_time 取最大
- **`kv_store_llm_response_cache.json`** — **日志类型全量辅助文档**：所有历史 LLM extract entry，含 `original_prompt`（chunk 原文）+ `return`（extraction 结果）

### 9 派生文件（可重建，从 3 真相源按需提取）

- `kv_store_text_chunks.json`
- `kv_store_doc_status.json`
- `vdb_chunks.json`
- `vdb_entities.json`
- `vdb_relationships.json`
- `kv_store_entity_chunks.json`
- `kv_store_relation_chunks.json`
- `kv_store_full_entities.json`
- `kv_store_full_relations.json`

### 重建算法（v8）

| 文件 | 重建算法 | 防复活机制 |
|------|---------|----------|
| `kv_store_text_chunks.json` | 从 GraphML node d3 + edge d10 提活跃 chunk_id 集合 C；对 C 中每个 chunk_id：**cache original_prompt 优先**（取 create_time 最大 entry，正则 ``` 提取原文）→ **full_docs fallback**（chunking 反查）→ **脑区节点**（d1=brainregion，content=`{脑区名}: {d2}`，full_doc_id=`brain_{脑区名}`）；llm_cache_list 从 cache 按 chunk_id 反向构建 | 只重建 C 中的 chunk |
| `kv_store_doc_status.json` | 从 text_chunks.full_doc_id 反向分组；所有 doc 标记 `status="processed"` | processed 不被重处理 |
| `vdb_chunks.json` | 遍历 text_chunks 重新 embedding（独立加载 niu_api.internal.embedding.get_model） | 只对 C 中的 chunk embedding |
| `vdb_entities.json` | 遍历 GraphML nodes 重新 embedding（content=f"{name}\n{desc}"） | **天然防复活**（只遍历 GraphML 存在的 node） |
| `vdb_relationships.json` | 遍历 GraphML edges 重新 embedding（content=f"{kw}\t{src}\n{tgt}\n{desc}"）；**不写 weight** | **天然防复活 + weight 不丢** |
| `kv_store_entity_chunks.json` | 从 GraphML node source_id 提取 chunk_ids | **天然防复活** |
| `kv_store_relation_chunks.json` | 从 GraphML edge source_id 提取 chunk_ids | **天然防复活** |
| `kv_store_full_entities.json` | 从 GraphML source_id + text_chunks.full_doc_id 反向映射 | **天然防复活** |
| `kv_store_full_relations.json` | 从 GraphML edge source_id + text_chunks.full_doc_id 反向映射 | **天然防复活** |

### 关键设计决策（v8）

1. **3 真相源完全不可动**：GraphML + full_docs + cache 都不写不改不删（读取是必要的，用于按需提取重建派生文件）。repair_all 只检测完好性，不修改。
2. **删除所有会动真相源的步骤**（铁律 3）：`repair_graphml` / `repair_brainregion_zombies` / `repair_llm_response_cache` / `repair_graphml_orphan_edges` / `get_lightrag_for_repair` / `_rebuild_vdb_matrix` / `_embed_batch` fallback 分支，全部函数体删除。
3. **GraphML 损坏 = unrecoverable**：不尝试重建（无白名单可过滤，重建无意义）。
4. **full_docs 损坏 = unrecoverable**：无法 fallback 重建 text_chunks。
5. **cache 损坏 = unrecoverable**：original_prompt 是主补充源，丢失无法恢复 chunk 原文。
6. **备份只备份 9 派生文件**：3 真相源不可动，不需要备份。
7. **回滚只回滚 9 派生文件**：3 真相源从未被修改，回滚不涉及它们。
8. **weight 不写 vdb**：vdb_relationships 的 meta_fields 不含 weight，weight 只存在 GraphML。
9. **脑区 chunk 直接从 GraphML 构造**（v8 纠正）：不查 text_chunks/full_docs/cache，从 d1=brainregion 的 node 直接构造 `{脑区名}: {d2 description}` + `brain_{脑区名}`。
10. **cache original_prompt 是主补充源**（v8 纠正）：覆盖大部分活跃 chunk，正则 ``` 提取原文。
11. **full_docs 是 fallback**（v8 纠正）：cache 找不到时才 chunking 反查。
12. **tokenizer 独立加载**（v8 纠正）：用 `lightrag.utils.TiktokenTokenizer`（model_name="gpt-4o-mini"），不调 get_lightrag。
13. **embedding 独立加载**（v8 纠正）：用 `niu_api.internal.embedding.get_model`，不调 get_lightrag。
14. **run_repair_on_user_request 重写**（铁律 3）：先停 RegionSync（`get_region_sync().stop_background_sync()`，实例方法非模块函数）+ `_repairing=True` 信号灯兜底 + 不调 get_lightrag + 不调 apipeline。
15. **测试用真实数据 + 真实 LLM**：不用 mock，每次测试后必须检查 3 真相源 mtime + hash 不变。
16. **不加任何检测逻辑**：不加僵尸脑区检测、已删脑区检测、僵尸 cache 检测。

---

## 文件结构

| 文件 | 责任 | 改动类型 |
|------|------|---------|
| `niu_api/internal/lightrag_repair.py` | 删除 6 个违规函数 + 重写 repair_text_chunks 为"cache original_prompt 优先 + full_docs fallback + 脑区直接构造" + 扩展 _load_graphml_nodes 返回 3 元组（etype, desc, src）+ 修复 4 处解构 + 重写 repair_all 为"3 真相源不可动 + 删 9 派生 + 重建" + 独立加载 tokenizer/embedding | 修改 |
| `niu_api/internal/lightrag_manager.py` | 重写 run_repair_on_user_request 为"先停 RegionSync + 不调 get_lightrag/apipeline" + 删除 get_lightrag_for_repair | 修改 |
| `tests/test_lightrag_repair_unit.py` | 删除违规函数的测试 + 新增 v8 算法测试 | 修改 |
| `tests/test_lightrag_repair.py` | 删除违规函数的 e2e 测试 + 适配 v8 返回结构 | 修改 |

---

## subagent 提示词强制要求（每个 Task 必须写进 implementer 提示词）

**铁律提醒**（每个 Task 的 implementer 提示词必须包含以下内容，不能省略）：

```
你是 v8 修复方案的 implementer。以下铁律必须严格遵守，违反任何一条立即停止：

1. 绝对禁止移动/删除/修改 ~/.niu/lightrag_storage/ 下任何 3 真相源文件：
   - graph_chunk_entity_relation.graphml
   - kv_store_full_docs.json
   - kv_store_llm_response_cache.json
   你可以读取它们做验证，但绝对不能 mv/rm/cat > 覆盖。

2. 测试隔离用 monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)，
   绝对不能用 mv 命令移动真实文件。
   必须同时 monkeypatch lightrag_integrity._STORAGE_DIR 和 lightrag_manager.STORAGE_DIR。

3. 每次测试后必须 stat + shasum 3 真相源，附真实命令输出：
   ```bash
   stat -f "%Sm %z %N" ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
   stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_full_docs.json
   stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
   shasum -a 256 ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
   shasum -a 256 ~/.niu/lightrag_storage/kv_store_full_docs.json
   shasum -a 256 ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
   ```
   必须附真实输出（不能用"应该不变"这种模糊描述）。

4. 必须真实跑 Pyright + pytest，附真实输出，不能撒谎：
   ```bash
   cd REDACTED_USER_PATH/tools/ai-bot
   ./python/bin/python -m pyright niu_api/internal/lightrag_repair.py 2>&1 | tail -30
   ./python/bin/python -m pytest tests/test_lightrag_repair_unit.py -xvs 2>&1 | tail -40
   ```
   如果有报错必须修，不能撒谎说"已通过"。

5. 修复文件权限（CLAUDE.md 铁律 7）：
   ```bash
   find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
   ```

6. 不加任何检测逻辑（铁律 4）：
   不加"僵尸脑区检测"、"已删脑区检测"、"僵尸 cache 检测"。
   check_all 只检测 missing/损坏。
   _check_truth_sources_intact 只检测 3 真相源是否完好（不检测内容）。

7. 不调 get_lightrag/get_lightrag_for_repair/apipeline（铁律 3）：
   tokenizer 独立加载用 lightrag.utils.TiktokenTokenizer。
   embedding 独立加载用 niu_api.internal.embedding.get_model。

8. git 操作后必须修复文件权限（CLAUDE.md 铁律 7）。

9. 修改前必须先做临时提交备份（CLAUDE.md 铁律 3）：
   git add -A && git commit -m "backup: before <Task名>"

10. 完成后报告必须包含：
    - 修改了哪些文件 + 行数变化
    - Pyright 真实输出（不能撒谎）
    - pytest 真实输出（不能撒谎）
    - 3 真相源 stat + shasum 真实输出（不能撒谎）
    - 任何疑虑
```

---

## Task 1: 删除 v4 违规函数（铁律 3）

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py`（删除 6 个函数）
- Modify: `niu_api/internal/lightrag_manager.py`（删除 `get_lightrag_for_repair`）
- Test: `tests/test_lightrag_repair_unit.py`（删除违规函数的测试）

### 背景

v4 实现保留了违规函数（`repair_graphml` / `repair_brainregion_zombies` / `repair_llm_response_cache` / `repair_graphml_orphan_edges`），仅从 `_REBUILD_ORDER` 移除。铁律 3 要求"所有含有写这三个真相源文件的代码段和程序全部删除"——这些函数体必须删除。

`get_lightrag_for_repair` 调 get_lightrag 触发 apipeline 初始化，可能写真相源，必须删除。

`_rebuild_vdb_matrix` 是违规函数的辅助，被 `repair_brainregion_zombies` 调用，必须删除。

`_embed_batch` 的 fallback 分支调 `get_lightrag_for_repair`，必须删除 fallback 分支（保留主分支用 niu_api.internal.embedding.get_model）。

### 需要删除的函数（6 个）

| 函数 | 行号范围 | 删除原因 |
|------|---------|---------|
| `repair_graphml` | 824-1117 | 写 GraphML（铁律 3） |
| `repair_graphml_orphan_edges` | 1131-1289 | 写 GraphML（铁律 3） |
| `repair_llm_response_cache` | 2012-2055 | 写 cache（铁律 3） |
| `repair_brainregion_zombies` | 2058-2390 | 写 GraphML + cache（铁律 3） |
| `get_lightrag_for_repair`（lightrag_manager.py） | 1008-1071 | 调 get_lightrag 触发 apipeline 写真相源（铁律 3） |
| `_rebuild_vdb_matrix` | 2706-2752 | repair_brainregion_zombies 辅助，删主调后无用 |

### `_embed_batch` fallback 分支删除

现有 `_embed_batch`（`lightrag_repair.py:106-148`）含 fallback 分支调 `get_lightrag_for_repair`。删除第 130-148 行 fallback 分支，只保留主分支用 `niu_api.internal.embedding.get_model`。

### - [ ] Step 1: 临时提交备份（铁律 3）

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: before v8-Task1 delete 6 v4 violation functions"
```

### - [ ] Step 2: 删除 6 个违规函数

Edit `niu_api/internal/lightrag_repair.py`：

1. 删除 `repair_graphml`（L824-1117，约 293 行）
2. 删除 `repair_graphml_orphan_edges`（L1131-1289，约 158 行）
3. 删除 `repair_llm_response_cache`（L2012-2055，约 43 行）
4. 删除 `repair_brainregion_zombies`（L2058-2390，约 332 行）
5. 删除 `_rebuild_vdb_matrix`（L2706-2752，约 46 行）

Edit `niu_api/internal/lightrag_manager.py`：
6. 删除 `get_lightrag_for_repair`（L1008-1071，约 63 行）

### - [ ] Step 3: 删除 `_embed_batch` 的 fallback 分支

Edit `niu_api/internal/lightrag_repair.py:130-148`：

删除 fallback 分支，只保留主分支。修改后 `_embed_batch` 变成：

```python
def _embed_batch(texts: list[str]) -> list[list[float]] | None:
    """批量 embedding。

    v8：只用 niu_api.internal.embedding 预加载的模型，不调 get_lightrag_for_repair（铁律 3）。
    失败返回 None。

    空列表返回 []（不调模型）。
    """
    if not texts:
        return []

    try:
        from niu_api.internal.embedding import get_model

        model = get_model()
        if model is not None:
            vecs = model.encode(texts)
            return [list(map(float, v)) for v in vecs]
    except Exception as e:  # noqa: BLE001
        logger.error(f"[LightRAGRepair] embedding 模型失败: {e}")
        return None

    logger.error("[LightRAGRepair] embedding 模型未就绪（get_model() 返回 None）")
    return None
```

### - [ ] Step 4: 删除违规函数的测试

Edit `tests/test_lightrag_repair_unit.py`：

删除以下测试函数（依赖被删函数）：
- `test_repair_brainregion_zombies_cleans_zombie_cache_entries`
- `test_repair_brainregion_zombies_no_zombies_leaves_cache_intact`
- `test_repair_brainregion_zombies_does_not_delete_normal_doc_with_zombie_word`
- `test_repair_brainregion_zombies_corrupt_cache_preserves_file`
- 任何其他 `test_repair_brainregion_zombies_*` 或 `test_repair_graphml_*` 或 `test_repair_llm_response_cache_*` 或 `test_repair_graphml_orphan_edges_*` 测试

### - [ ] Step 5: Pyright + pytest 真实验证

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
./python/bin/python -m pyright niu_api/internal/lightrag_repair.py 2>&1 | tail -30
./python/bin/python -m pyright niu_api/internal/lightrag_manager.py 2>&1 | tail -30
./python/bin/python -m pytest tests/test_lightrag_repair_unit.py -xvs 2>&1 | tail -40
```

Expected:
- Pyright 无新报错（删除函数后不应有新未定义引用）
- pytest 剩余测试全通过（如果有失败必须修，不能撒谎）

### - [ ] Step 6: 3 真相源 stat + shasum 验证

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_full_docs.json
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
shasum -a 256 ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
shasum -a 256 ~/.niu/lightrag_storage/kv_store_full_docs.json
shasum -a 256 ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
```

Expected:
- 3 真相源 mtime + hash 跟 Task 开始前完全一致（Task 1 只删函数体，不动数据）

### - [ ] Step 7: 修复文件权限 + 临时提交

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
git add -A && git commit -m "refactor(repair): v8-Task1 delete 6 v4 violation functions (repair_graphml/repair_graphml_orphan_edges/repair_llm_response_cache/repair_brainregion_zombies/get_lightrag_for_repair/_rebuild_vdb_matrix) + remove _embed_batch fallback"
```

---

## Task 2: 独立加载 tokenizer + embedding（铁律 3）

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py`（新增 `_get_tokenizer` + `_get_embed_model` 函数）
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

v4 的 `repair_text_chunks` fallback 分支调 `get_lightrag_for_repair()` 拿 tokenizer（铁律 3 违规）。v8 改为独立加载：
- tokenizer：`lightrag.utils.TiktokenTokenizer`（model_name="gpt-4o-mini"）
- embedding：`niu_api.internal.embedding.get_model`（已在 `_embed_batch` 用，无需新加）

### - [ ] Step 1: 临时提交备份

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: before v8-Task2 independent tokenizer embedding"
```

### - [ ] Step 2: 新增 `_get_tokenizer` 函数

Edit `niu_api/internal/lightrag_repair.py`，在 `_embed_batch` 附近新增：

```python
def _get_tokenizer():
    """独立加载 tokenizer（不调 get_lightrag_for_repair，铁律 3）。

    用 lightrag.utils.TiktokenTokenizer（model_name="gpt-4o-mini"）。
    失败返回 None。
    """
    try:
        from lightrag.utils import TiktokenTokenizer

        return TiktokenTokenizer(model_name="gpt-4o-mini")
    except Exception as e:  # noqa: BLE001
        logger.error(f"[LightRAGRepair] 加载 TiktokenTokenizer 失败: {e}")
        return None


def _get_chunk_config() -> tuple[int, int]:
    """读 chunk_size + chunk_overlap（不调 get_lightrag_for_repair，铁律 3）。

    从 niu_api.internal.lightrag_manager._get_lightrag_config 读。
    失败 fallback (1200, 50)（与 lightrag_manager.py:853 真实默认值 chunk_overlap_token_size=50 一致）。
    """
    try:
        from niu_api.internal.lightrag_manager import _get_lightrag_config

        config = _get_lightrag_config()
        chunk_token_size = config.get("chunk_token_size", 1200)
        chunk_overlap = config.get("chunk_overlap_token_size", 50)
        return chunk_token_size, chunk_overlap
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LightRAGRepair] 读 chunk_config 失败，用 fallback (1200, 50): {e}")
        return 1200, 50
```

**注意**：`_get_lightrag_config` 只读配置不调 apipeline，安全。

### - [ ] Step 3: 写测试

Edit `tests/test_lightrag_repair_unit.py`，新增：

```python
def test_get_tokenizer_independent_load():
    """_get_tokenizer 应独立加载 TiktokenTokenizer，不调 get_lightrag_for_repair。"""
    from niu_api.internal.lightrag_repair import _get_tokenizer

    tokenizer = _get_tokenizer()
    assert tokenizer is not None, "TiktokenTokenizer 应加载成功"
    # 验证有 tokenize 方法
    assert hasattr(tokenizer, "tokenize"), "tokenizer 应有 tokenize 方法"


def test_get_chunk_config_no_get_lightrag():
    """_get_chunk_config 不应调 get_lightrag（铁律 3）。

    Task 1 已删 get_lightrag_for_repair，所以这里 patch get_lightrag（仍存在的函数）。
    _get_chunk_config 只应调 _get_lightrag_config（读配置，不调 apipeline），
    不应调 get_lightrag（会触发 apipeline 写真相源）。
    """
    from unittest.mock import patch

    # patch get_lightrag（Task 1 后仍存在）；若 _get_chunk_config 误调它则 AssertionError
    with patch("niu_api.internal.lightrag_manager.get_lightrag", side_effect=AssertionError("禁止调 get_lightrag")):
        from niu_api.internal.lightrag_repair import _get_chunk_config

        chunk_size, chunk_overlap = _get_chunk_config()
        assert chunk_size > 0
        assert chunk_overlap >= 0
```

### - [ ] Step 4: Pyright + pytest 真实验证

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
./python/bin/python -m pyright niu_api/internal/lightrag_repair.py 2>&1 | tail -20
./python/bin/python -m pytest tests/test_lightrag_repair_unit.py::test_get_tokenizer_independent_load tests/test_lightrag_repair_unit.py::test_get_chunk_config_no_get_lightrag -xvs 2>&1 | tail -30
```

### - [ ] Step 5: 3 真相源 stat + shasum 验证

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
shasum -a 256 ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
shasum -a 256 ~/.niu/lightrag_storage/kv_store_full_docs.json
shasum -a 256 ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
```

Expected: 3 真相源 mtime + hash 跟 Task 2 开始前一致。

### - [ ] Step 6: 修复权限 + 临时提交

```bash
cd REDACTED_USER_PATH/tools/ai-bot
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
git add -A && git commit -m "refactor(repair): v8-Task2 independent tokenizer (TiktokenTokenizer) + chunk_config reader (no get_lightrag_for_repair)"
```

---

## Task 3: 扩展 _load_graphml_nodes 返回 3 元组 + 修复 4 处解构

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py`（`_load_graphml_nodes` 改 3 元组 + 4 处解构修复）
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

现有 `_load_graphml_nodes`（`lightrag_repair.py:295-355`）返回 `dict[str, tuple[str, str]]`（desc, src）。v8 需要识别脑区节点（d1 entity_type=="brainregion"），必须返回 3 元组 `(entity_type, desc, src)`。

调用 `_load_graphml_nodes` 的 4 处解构（line 531, 1472, 1745, 1895）必须同步修复。

### - [ ] Step 1: 临时提交备份

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: before v8-Task3 extend _load_graphml_nodes 3-tuple"
```

### - [ ] Step 2: 写失败测试

Edit `tests/test_lightrag_repair_unit.py`，新增：

```python
def test_load_graphml_nodes_returns_3_tuple_with_entity_type(tmp_path, monkeypatch):
    """_load_graphml_nodes 应返回 {node_id: (entity_type, desc, src)} 3 元组。"""
    import xml.etree.ElementTree as ET

    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})

    # 普通实体节点
    n1 = ET.SubElement(graph, f"{{{ns}}}node", {"id": "entity-x"})
    ET.SubElement(n1, f"{{{ns}}}data", {"key": "d1"}).text = "person"
    ET.SubElement(n1, f"{{{ns}}}data", {"key": "d2"}).text = "desc X"
    ET.SubElement(n1, f"{{{ns}}}data", {"key": "d3"}).text = "chunk-aaa"

    # 脑区节点
    n2 = ET.SubElement(graph, f"{{{ns}}}node", {"id": "文档库脑区"})
    ET.SubElement(n2, f"{{{ns}}}data", {"key": "d1"}).text = "brainregion"
    ET.SubElement(n2, f"{{{ns}}}data", {"key": "d2"}).text = "文档库脑区描述<SEP>brain_meta_size:94"
    ET.SubElement(n2, f"{{{ns}}}data", {"key": "d3"}).text = "chunk-bbb"

    (tmp_path / "graph_chunk_entity_relation.graphml").write_text(
        ET.tostring(root, encoding="unicode")
    )

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import _load_graphml_nodes

    nodes, err = _load_graphml_nodes()
    assert err is None
    assert nodes["entity-x"] == ("person", "desc X", "chunk-aaa")
    assert nodes["文档库脑区"] == ("brainregion", "文档库脑区描述<SEP>brain_meta_size:94", "chunk-bbb")
```

### - [ ] Step 3: 修改 `_load_graphml_nodes` 返回 3 元组

Edit `niu_api/internal/lightrag_repair.py:295-355`：

```python
def _load_graphml_nodes() -> tuple[dict[str, tuple[str, str, str]], dict[str, Any] | None]:
    """解析 GraphML nodes，返回 {node_id: (entity_type, description, source_id)} + error。

    v8：返回 3 元组（entity_type, desc, src），识别脑区节点 d1=="brainregion"。

    entity_type = d1（缺省空字符串）, description = d2, source_id = d3
    """
    import xml.etree.ElementTree as ET

    path = _storage_dir() / _GRAPHML_FILE
    if not path.exists():
        return {}, None
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError as e:
        return {}, {
            "check": "xml_parse",
            "file": _GRAPHML_FILE,
            "msg": str(e),
            "severity": "critical",
        }
    except Exception as e:  # noqa: BLE001
        return {}, {
            "check": "xml_parse",
            "file": _GRAPHML_FILE,
            "msg": f"{type(e).__name__}: {e}",
            "severity": "critical",
        }

    ns = "{http://graphml.graphdrawing.org/xmlns}"
    nodes: dict[str, tuple[str, str, str]] = {}

    graph = root.find(f"{ns}graph")
    if graph is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "graph":
                graph = child
                break
    if graph is None:
        return {}, {
            "check": "no_graph_element",
            "file": _GRAPHML_FILE,
            "severity": "critical",
        }

    for child in graph:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "node":
            nid = child.get("id", "")
            if not nid:
                continue
            etype = ""
            desc = ""
            src = ""
            for data in child.findall(f"{ns}data"):
                key = data.get("key")
                if key == "d1":
                    etype = data.text or ""
                elif key == "d2":
                    desc = data.text or ""
                elif key == "d3":
                    src = data.text or ""
            nodes[nid] = (etype, desc, src)
    return nodes, None
```

### - [ ] Step 4: 修复 4 处解构

Edit `niu_api/internal/lightrag_repair.py`：

**位置 1：L531（repair_text_chunks）**
```python
# 改前
for node_id, (desc, src_ids) in nodes.items():
    if src_ids:
        active_chunk_ids.update(c for c in src_ids.split(GRAPH_FIELD_SEP) if c)
# 改后
for node_id, (etype, desc, src_ids) in nodes.items():
    if src_ids:
        active_chunk_ids.update(c for c in src_ids.split(GRAPH_FIELD_SEP) if c)
```

**位置 2：L1472（repair_vdb_entities）**
```python
# 改前
for node_id, (desc, src) in nodes.items():
    # desc 为空时用 node_id 作为 fallback
    ...
# 改后
for node_id, (etype, desc, src) in nodes.items():
    # desc 为空时用 node_id 作为 fallback
    ...
```

**位置 3：L1745（repair_entity_chunks）**
```python
# 改前
for node_id, (desc, src) in nodes.items():
    if not src:
        ...
# 改后
for node_id, (etype, desc, src) in nodes.items():
    if not src:
        ...
```

**位置 4：L1895（repair_full_entities）**
```python
# 改前
for node_id, (desc, src) in nodes.items():
    if not src:
        continue
    ...
# 改后
for node_id, (etype, desc, src) in nodes.items():
    if not src:
        continue
    ...
```

### - [ ] Step 5: Pyright + pytest 真实验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./python/bin/python -m pyright niu_api/internal/lightrag_repair.py 2>&1 | tail -20
./python/bin/python -m pytest tests/test_lightrag_repair_unit.py -xvs 2>&1 | tail -40
```

### - [ ] Step 6: 3 真相源 stat + shasum 验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
shasum -a 256 ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
shasum -a 256 ~/.niu/lightrag_storage/kv_store_full_docs.json
shasum -a 256 ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
```

Expected: 3 真相源 mtime + hash 跟 Task 3 开始前一致（Task 3 不动数据）。

### - [ ] Step 7: 修复权限 + 临时提交

```bash
cd REDACTED_USER_PATH/tools/ai-bot
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
git add -A && git commit -m "refactor(repair): v8-Task3 extend _load_graphml_nodes 3-tuple (etype, desc, src) + fix 4 destructure sites"
```

---

## Task 4: 重写 repair_text_chunks 按需提取算法（cache original_prompt 优先 + full_docs fallback + 脑区直接构造）

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py:484-717`（重写 repair_text_chunks）
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

v4 的 `repair_text_chunks` fallback 链是 `existing_tc → full_docs`，没有 cache original_prompt 提取。v8 改为：

1. **脑区节点**（d1=brainregion）直接从 GraphML 构造（不查 text_chunks/full_docs/cache）：
   - content = `{node_id 脑区名}: {d2 description}`
   - full_doc_id = `brain_{node_id 脑区名}`
2. **cache original_prompt 优先**（非脑区 chunk）：
   - 按 chunk_id 索引 cache extract entry，多条取 create_time 最大
   - 正则 `r"```\s*(.+?)\s*```"` + re.DOTALL 提取 chunk 原文
3. **full_docs fallback**（cache 找不到时）：
   - 对每个 doc 用独立 tokenizer chunking，算 chunk_id
   - 跟活跃 chunk_id 匹配
4. **三处都没有 → missing**（理论上不该发生）

### - [ ] Step 1: 临时提交备份

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: before v8-Task4 rewrite repair_text_chunks cache original_prompt priority"
```

### - [ ] Step 2: 写失败测试

Edit `tests/test_lightrag_repair_unit.py`，新增：

```python
def _write_graphml_v8(tmp_path, nodes_data, edges_data=None):
    """写 GraphML v8 测试 fixture。
    nodes_data = [(node_id, etype, desc, src), ...]
    edges_data = [(src, tgt, src_ids, desc, kw), ...]
    """
    import xml.etree.ElementTree as ET
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    for node_id, etype, desc, src in nodes_data:
        node = ET.SubElement(graph, f"{{{ns}}}node", {"id": node_id})
        if etype:
            ET.SubElement(node, f"{{{ns}}}data", {"key": "d1"}).text = etype
        if desc:
            ET.SubElement(node, f"{{{ns}}}data", {"key": "d2"}).text = desc
        if src:
            ET.SubElement(node, f"{{{ns}}}data", {"key": "d3"}).text = src
    if edges_data:
        for src, tgt, src_ids, desc, kw in edges_data:
            edge = ET.SubElement(graph, f"{{{ns}}}edge", {"source": src, "target": tgt})
            if desc:
                ET.SubElement(edge, f"{{{ns}}}data", {"key": "d8"}).text = desc
            if kw:
                ET.SubElement(edge, f"{{{ns}}}data", {"key": "d9"}).text = kw
            if src_ids:
                ET.SubElement(edge, f"{{{ns}}}data", {"key": "d10"}).text = src_ids
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text(
        ET.tostring(root, encoding="unicode")
    )


def _build_cache_prompt(chunk_content):
    """构造 cache original_prompt（含 ``` 包裹的 chunk 原文）。"""
    return f"""---Task---
Extract entities and relationships from the input text.

---Data---
```
{chunk_content}
```

---Output---
"""


def test_repair_text_chunks_cache_original_prompt_priority(tmp_path, monkeypatch):
    """repair_text_chunks 应优先从 cache original_prompt 提取 chunk 原文。"""
    chunk_content = "测试 chunk 原文 cache 优先"
    # GraphML：1 个实体引用 chunk-active
    _write_graphml_v8(tmp_path, [("entity-x", "person", "desc X", "chunk-active")])

    # text_chunks 为空（强制走 cache 提取路径）
    (tmp_path / "kv_store_text_chunks.json").write_text("{}")

    # cache：1 个 extract entry，chunk_id=chunk-active，original_prompt 含 chunk 原文
    cache = {
        "cache-key-1": {
            "return": "entity<|#|>名字<|#|>person<|#|>描述",
            "cache_type": "extract",
            "chunk_id": "chunk-active",
            "original_prompt": _build_cache_prompt(chunk_content),
            "create_time": 1781930000,
        }
    }
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache))

    # full_docs 空（验证 cache 优先于 full_docs）
    (tmp_path / "kv_store_full_docs.json").write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_text_chunks

    result = repair_text_chunks()

    assert result["status"] == "ok"
    assert result["actual"] == 1
    assert result["lost"] == 0

    tc_after = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert "chunk-active" in tc_after
    assert tc_after["chunk-active"]["content"] == chunk_content
    # llm_cache_list 应包含 cache_key
    assert "cache-key-1" in tc_after["chunk-active"]["llm_cache_list"]


def test_repair_text_chunks_cache_multiple_entries_take_latest_create_time(tmp_path, monkeypatch):
    """同 chunk_id 多条 cache entry，取 create_time 最大的。"""
    chunk_v1 = "v1 chunk 原文"
    chunk_v2 = "v2 chunk 原文"
    _write_graphml_v8(tmp_path, [("entity-x", "person", "desc X", "chunk-active")])

    (tmp_path / "kv_store_text_chunks.json").write_text("{}")
    (tmp_path / "kv_store_full_docs.json").write_text("{}")

    cache = {
        "cache-key-old": {
            "return": "v1 extraction",
            "cache_type": "extract",
            "chunk_id": "chunk-active",
            "original_prompt": _build_cache_prompt(chunk_v1),
            "create_time": 1781930000,
        },
        "cache-key-new": {
            "return": "v2 extraction",
            "cache_type": "extract",
            "chunk_id": "chunk-active",
            "original_prompt": _build_cache_prompt(chunk_v2),
            "create_time": 1781930999,  # 更大
        },
    }
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_text_chunks

    result = repair_text_chunks()

    assert result["status"] == "ok"
    tc_after = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    # 应取 create_time=1781930999 的 entry（v2）
    assert tc_after["chunk-active"]["content"] == chunk_v2


def test_repair_text_chunks_full_docs_fallback_when_cache_miss(tmp_path, monkeypatch):
    """cache 找不到 chunk_id 时，从 full_docs chunking 反查。"""
    # 用真实 chunk_id 算法（compute_mdhash_id）
    from lightrag.utils import compute_mdhash_id

    chunk_content = "这是从 full_docs 反查的 chunk 原文"
    expected_chunk_id = compute_mdhash_id(chunk_content, prefix="chunk-")

    _write_graphml_v8(tmp_path, [("entity-x", "person", "desc X", expected_chunk_id)])

    (tmp_path / "kv_store_text_chunks.json").write_text("{}")
    # cache 空（强制走 full_docs fallback）
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")

    # full_docs：1 个 doc，content 经 chunking 后产生 expected_chunk_id
    docs = {
        "doc-1": {
            "content": chunk_content,
            "file_path": "test.md",
            "create_time": 1781930000,
        }
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_text_chunks

    result = repair_text_chunks()

    assert result["status"] == "ok"
    assert result["lost"] == 0
    tc_after = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert expected_chunk_id in tc_after
    assert tc_after[expected_chunk_id]["content"] == chunk_content
    assert tc_after[expected_chunk_id]["full_doc_id"] == "doc-1"


def test_repair_text_chunks_brainregion_direct_construction(tmp_path, monkeypatch):
    """脑区节点（d1=brainregion）直接从 GraphML 构造，不查 full_docs/cache。"""
    brain_desc = "文档库脑区描述<SEP>brain_meta_size:94"
    _write_graphml_v8(tmp_path, [
        ("文档库脑区", "brainregion", brain_desc, "chunk-brain-1"),
    ])

    (tmp_path / "kv_store_text_chunks.json").write_text("{}")
    (tmp_path / "kv_store_full_docs.json").write_text("{}")  # 脑区不在 full_docs
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")  # 脑区也不在 cache

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_text_chunks

    result = repair_text_chunks()

    assert result["status"] == "ok"
    assert result["lost"] == 0
    tc_after = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert "chunk-brain-1" in tc_after
    # content = "文档库脑区: {d2 description}"
    assert tc_after["chunk-brain-1"]["content"] == f"文档库脑区: {brain_desc}"
    # full_doc_id = "brain_文档库脑区"
    assert tc_after["chunk-brain-1"]["full_doc_id"] == "brain_文档库脑区"


def test_repair_text_chunks_missing_when_three_sources_all_miss(tmp_path, monkeypatch):
    """cache + full_docs + 脑区都没匹配 → missing（lost>0）。"""
    _write_graphml_v8(tmp_path, [
        ("entity-x", "person", "desc X", "chunk-not-found-anywhere"),
    ])

    (tmp_path / "kv_store_text_chunks.json").write_text("{}")
    (tmp_path / "kv_store_full_docs.json").write_text("{}")
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_text_chunks

    result = repair_text_chunks()

    assert result["status"] == "ok"
    assert result["expected"] == 1
    assert result["actual"] == 0
    assert result["lost"] == 1


def test_repair_text_chunks_real_cache_extraction(tmp_path, monkeypatch):
    """v8 核心验证（I3）：用真实 cache 数据验证正则提取 chunk 原文正确性。

    真实 cache 的 original_prompt 含 8 个 ```（4 对），只有第一对 ``` 之间是 chunk 原文。
    非贪婪正则 r"```\\s*(.+?)\\s*```" 必须正确提取第一对之间内容，不能跨多对 ```。
    """
    import os, shutil
    src_dir = os.path.expanduser("~/.niu/lightrag_storage")

    # 拷贝真实 3 真相源到 tmp_path
    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    for fname in truth_files:
        shutil.copy2(os.path.join(src_dir, fname), tmp_path / fname)

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_text_chunks

    result = repair_text_chunks()

    # 真实数据：145 个活跃 chunk，cache 应覆盖大部分，full_docs fallback 覆盖剩余
    # 脑区 8 个直接构造，其余从 cache + full_docs 提取
    assert result["status"] == "ok", f"repair_text_chunks 失败: {result.get('message', '')}"
    assert result["expected"] == 145, f"活跃 chunk 数应为 145，实际 {result['expected']}"
    assert result["lost"] == 0, f"应无丢失 chunk，实际 lost={result['lost']}, missing={result.get('missing_chunks', [])}"

    # 验证 text_chunks 内容非空（每个 chunk content 必须有真实原文，不是空串）
    import json
    tc = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    empty_content = [cid for cid, v in tc.items() if not v.get("content", "").strip()]
    assert not empty_content, f"以下 chunk content 为空: {empty_content[:5]}"

    # 验证至少有 1 个 chunk 是从 cache 提取的（非脑区 chunk 占多数）
    non_brain = [cid for cid, v in tc.items() if not str(v.get("full_doc_id", "")).startswith("brain_")]
    assert len(non_brain) > 0, "应有非脑区 chunk 从 cache/full_docs 提取"

    # 验证正则没把 LLM 输出示例（后续 3 对 ``` 之间的内容）当 chunk 原文：
    # 如果正则贪婪匹配跨多对 ```，chunk content 会含 "entity<|#|>" 等 LLM 输出标记
    bad_extraction = [cid for cid, v in tc.items() if "<|#|>" in v.get("content", "")]
    assert not bad_extraction, f"正则提取错误，含 LLM 输出标记: {bad_extraction[:5]}"
```

**注意**：本测试用真实 cache 数据（293 个 extract entry，每个 original_prompt 含 8 个 ```），验证非贪婪正则正确提取第一对 ``` 之间的 chunk 原文，不会跨多对 ``` 提取 LLM 输出示例。这是 v8 核心纠正点的真实验证。

### - [ ] Step 3: 重写 repair_text_chunks

Edit `niu_api/internal/lightrag_repair.py:484-717`：

```python
def repair_text_chunks() -> dict[str, Any]:
    """v8：从 GraphML 提活跃 chunk_id + cache original_prompt 优先 + full_docs fallback + 脑区直接构造。

    真相源：GraphML（唯一真相源，提活跃 chunk_id + 脑区元数据）
    辅助：cache original_prompt（主补充源，正则提取 ``` 之间 chunk 原文，多条取 create_time 最大）
         full_docs（fallback，cache 找不到时 chunking 反查）
    派生：kv_store_text_chunks.json

    算法：
    1. 解析 GraphML 提取活跃 chunk_id 集合 C（从所有 node d3 + edge d10）
    2. 识别脑区节点（d1=brainregion），直接构造 chunk：
       - content = "{node_id}: {d2 description}"
       - full_doc_id = "brain_{node_id}"
    3. 对 C 中非脑区 chunk_id：
       a. cache original_prompt 优先：按 chunk_id 索引 cache extract entry，
          多条取 create_time 最大，正则 r"```\\s*(.+?)\\s*```" + re.DOTALL 提取 chunk 原文
       b. full_docs fallback：cache 找不到时，对每个 doc 用独立 tokenizer chunking，
          算 chunk_id（compute_mdhash_id），跟活跃 chunk_id 匹配
    4. 三处都没有 → missing
    5. llm_cache_list 从 cache 按 chunk_id 反向构建

    GraphML 损坏 = unrecoverable
    cache 损坏 + full_docs 损坏 = unrecoverable
    """
    import re

    storage_dir = _storage_dir()
    tc_path = storage_dir / "kv_store_text_chunks.json"
    full_docs_path = storage_dir / "kv_store_full_docs.json"
    cache_path = storage_dir / "kv_store_llm_response_cache.json"

    # 1. 解析 GraphML 提取活跃 chunk_id 集合 C + 识别脑区节点
    nodes, nodes_err = _load_graphml_nodes()
    if nodes_err is not None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {nodes_err.get('msg', '')}",
            "unrecoverable": True,
        }
    node_ids_set, edges_list, edges_err = _load_graphml_nodes_edges()
    if edges_err is not None:
        return {
            "status": "error",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML",
            "message": f"GraphML 损坏: {edges_err.get('msg', '')}",
            "unrecoverable": True,
        }

    # 收集活跃 chunk_id
    active_chunk_ids: set[str] = set()
    brainregion_chunks: dict[str, tuple[str, str]] = {}
    # brainregion_chunks: chunk_id -> (content, full_doc_id)

    for node_id, (etype, desc, src_ids) in nodes.items():
        if etype == "brainregion":
            # 脑区节点直接构造 chunk
            if src_ids:
                brain_content = f"{node_id}: {desc}"
                brain_full_doc_id = f"brain_{node_id}"
                for cid in src_ids.split(GRAPH_FIELD_SEP):
                    if cid:
                        brainregion_chunks[cid] = (brain_content, brain_full_doc_id)
                        active_chunk_ids.update(c for c in src_ids.split(GRAPH_FIELD_SEP) if c)
        else:
            if src_ids:
                active_chunk_ids.update(c for c in src_ids.split(GRAPH_FIELD_SEP) if c)
    for edge_tuple in edges_list:
        edge_src_ids = edge_tuple[2]  # (src, tgt, src_ids, desc, kw)
        if edge_src_ids:
            active_chunk_ids.update(c for c in edge_src_ids.split(GRAPH_FIELD_SEP) if c)

    # 全新用户（GraphML 为空）→ 返回 ok 空结果
    if not active_chunk_ids:
        logger.info("[LightRAGRepair] GraphML 无活跃 chunk_id（全新用户），写空 text_chunks")
        if tc_path.exists():
            _backup_corrupt(tc_path)
        _atomic_write_json(tc_path, {})
        return {
            "status": "ok",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "GraphML + cache + full_docs",
            "message": "GraphML 无活跃 chunk_id，重建空 text_chunks",
        }

    # 2. 读 cache（主补充源）
    cache: dict[str, Any] = {}
    cache_corrupt = False
    if cache_path.exists():
        loaded = _load_json_dict(cache_path)
        if isinstance(loaded, dict):
            cache = loaded
        elif loaded is None and cache_path.exists():
            cache_corrupt = True

    # 3. 读 full_docs（fallback）
    full_docs: dict[str, Any] = {}
    full_docs_corrupt = False
    if full_docs_path.exists():
        loaded = _load_json_dict(full_docs_path)
        if isinstance(loaded, dict):
            full_docs = loaded
        elif loaded is None and full_docs_path.exists():
            full_docs_corrupt = True

    # 4. 构建 cache 的 chunk_id -> [entries] 映射（按 create_time 降序）
    cache_by_chunk_id: dict[str, list[tuple[int, str, str]]] = {}
    # 类型: chunk_id -> [(create_time, original_prompt, cache_key), ...]
    cache_pattern = re.compile(r"```\s*(.+?)\s*```", re.DOTALL)
    for cache_key, entry in cache.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("cache_type") != "extract":
            continue
        cid = entry.get("chunk_id")
        if not cid:
            continue
        ct = entry.get("create_time", 0)
        op = entry.get("original_prompt", "")
        cache_by_chunk_id.setdefault(cid, []).append((ct, op, cache_key))

    # 每个 chunk_id 的 entries 按 create_time 降序排（最大在前）
    for cid in cache_by_chunk_id:
        cache_by_chunk_id[cid].sort(key=lambda x: x[0], reverse=True)

    # 5. 判断是否需要扫 full_docs（cache 没覆盖 + 脑区没覆盖的 chunk）
    non_brain_active = active_chunk_ids - set(brainregion_chunks.keys())
    cache_covered = set(cid for cid in cache_by_chunk_id if cid in non_brain_active)
    need_full_docs_scan = any(cid not in cache_covered for cid in non_brain_active)

    # 6. full_docs chunking 反查（仅当需要扫 + cache 损坏时检测 unrecoverable）
    full_docs_chunk_map: dict[str, tuple[int, str, str, str]] = {}
    # 类型: chunk_id -> (create_time, doc_id, chunk_content, file_path)
    if need_full_docs_scan:
        if not full_docs:
            # cache + full_docs 都损坏 → unrecoverable
            if cache_corrupt and full_docs_corrupt:
                return {
                    "status": "error",
                    "expected": len(active_chunk_ids),
                    "actual": 0,
                    "lost": len(active_chunk_ids),
                    "source": "GraphML + cache + full_docs",
                    "message": "cache 和 full_docs 都损坏，无法 fallback 重建",
                    "unrecoverable": True,
                }
        else:
            # 独立加载 tokenizer（不调 get_lightrag_for_repair，铁律 3）
            tokenizer = _get_tokenizer()
            if tokenizer is None:
                return {
                    "status": "error",
                    "expected": len(active_chunk_ids),
                    "actual": 0,
                    "lost": len(active_chunk_ids),
                    "source": "GraphML + cache + full_docs",
                    "message": "TiktokenTokenizer 加载失败，无法 chunking",
                    "unrecoverable": True,
                }
            chunk_token_size, chunk_overlap = _get_chunk_config()

            from lightrag.operate import chunking_by_token_size

            # 按 create_time 降序排 full_docs（多 doc 匹配同 chunk_id 时取最新版本）
            sorted_docs = sorted(
                full_docs.items(),
                key=lambda kv: kv[1].get("create_time", 0) if isinstance(kv[1], dict) else 0,
                reverse=True,
            )

            for doc_id, doc_data in sorted_docs:
                if not isinstance(doc_data, dict):
                    continue
                content = doc_data.get("content", "")
                if not content:
                    continue
                file_path = doc_data.get("file_path", "")
                create_time = doc_data.get("create_time", 0)

                chunks = chunking_by_token_size(
                    tokenizer, content,
                    chunk_token_size=chunk_token_size,
                    chunk_overlap_token_size=chunk_overlap,
                )
                for chunk in chunks:
                    chunk_content = chunk["content"]
                    cid = compute_mdhash_id(chunk_content, prefix="chunk-")
                    if cid not in full_docs_chunk_map:
                        full_docs_chunk_map[cid] = (create_time, doc_id, chunk_content, file_path)

    # 7. 遍历 C 构建 new_tc
    new_tc: dict[str, Any] = {}
    missing_chunks: list[str] = []

    # 7a. 先填脑区 chunk
    for cid, (content, full_doc_id) in brainregion_chunks.items():
        # 脑区 chunk 的 llm_cache_list 也从 cache 反向构建（如果有）
        cache_keys_for_cid = []
        if cid in cache_by_chunk_id:
            cache_keys_for_cid = [e[2] for e in cache_by_chunk_id[cid]]
        new_tc[cid] = {
            "content": content,
            "full_doc_id": full_doc_id,
            "llm_cache_list": cache_keys_for_cid,
        }

    # 7b. 非脑区 chunk：cache 优先 → full_docs fallback → missing
    for cid in active_chunk_ids:
        if cid in new_tc:
            continue  # 已是脑区 chunk
        if cid in cache_by_chunk_id:
            # cache original_prompt 提取（取 create_time 最大的 entry）
            latest_entry = cache_by_chunk_id[cid][0]  # 已降序排
            _, op, _ = latest_entry
            m = cache_pattern.search(op)
            if m:
                chunk_content = m.group(1)
                # 从 cache entry 反查 doc_id（不在 cache 字段里，用空字符串）
                # 注意：cache entry 不含 full_doc_id 字段，无法反查 doc_id
                # 用 "" 占位（doc_status 重建时会处理这种 chunk）
                new_tc[cid] = {
                    "content": chunk_content,
                    "full_doc_id": "",  # cache 不含 doc_id，留空
                    "llm_cache_list": [e[2] for e in cache_by_chunk_id[cid]],
                }
                continue
        # full_docs fallback
        if cid in full_docs_chunk_map:
            ct, doc_id, content, file_path = full_docs_chunk_map[cid]
            new_tc[cid] = {
                "content": content,
                "full_doc_id": doc_id,
                "llm_cache_list": [e[2] for e in cache_by_chunk_id.get(cid, [])],
            }
            continue
        # 三处都没有 → missing
        missing_chunks.append(cid)

    # 8. 备份损坏的 text_chunks + 原子写
    _backup_corrupt(tc_path)
    try:
        _atomic_write_json(tc_path, new_tc)
    except Exception as e:
        return {
            "status": "error",
            "expected": len(active_chunk_ids),
            "actual": len(new_tc),
            "lost": len(active_chunk_ids) - len(new_tc),
            "source": "GraphML + cache + full_docs",
            "message": f"写 text_chunks 失败: {e}",
            "unrecoverable": True,
        }

    actual = len(new_tc)
    logger.info(
        f"[LightRAGRepair] 重建 text_chunks: {actual}/{len(active_chunk_ids)} 条 "
        f"(cache original_prompt 优先 + full_docs fallback + 脑区直接构造，"
        f"missing={len(missing_chunks)})"
    )
    return {
        "status": "ok",
        "expected": len(active_chunk_ids),
        "actual": actual,
        "lost": len(missing_chunks),
        "source": "GraphML + cache + full_docs",
        "missing_chunks": missing_chunks[:10],
        "message": f"重建 {actual}/{len(active_chunk_ids)} 个 chunk，missing {len(missing_chunks)} 个",
    }
```

### - [ ] Step 4: Pyright + pytest 真实验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./python/bin/python -m pyright niu_api/internal/lightrag_repair.py 2>&1 | tail -30
./python/bin/python -m pytest tests/test_lightrag_repair_unit.py -xvs -k "repair_text_chunks" 2>&1 | tail -60
```

Expected: 6 个新测试全通过（5 个合成 fixture + 1 个真实 cache 数据测试）。如有失败必须修，不能撒谎。真实 cache 数据测试 `test_repair_text_chunks_real_cache_extraction` 是 v8 核心验证，必须通过。

### - [ ] Step 5: 3 真相源 stat + shasum 验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_full_docs.json
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
shasum -a 256 ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
shasum -a 256 ~/.niu/lightrag_storage/kv_store_full_docs.json
shasum -a 256 ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
```

Expected: 3 真相源 mtime + hash 跟 Task 4 开始前一致。

### - [ ] Step 6: 修复权限 + 临时提交

```bash
cd REDACTED_USER_PATH/tools/ai-bot
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
git add -A && git commit -m "refactor(repair): v8-Task4 rewrite repair_text_chunks (cache original_prompt priority + full_docs fallback + brainregion direct construction)"
```

---

## Task 5: repair_doc_status 回归测试

**Files:**
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

`repair_doc_status`（`lightrag_repair.py:720-823`）从 text_chunks.full_doc_id 反向分组 + 标记所有 doc status="processed"。v4 实现已正确，v8 只需新增回归测试覆盖：
1. 脑区 chunk 的 full_doc_id=`brain_{脑区名}` 应在 doc_status 中出现
2. cache fallback chunk 的 full_doc_id=""（空）应跳过
3. full_docs fallback chunk 的 full_doc_id=doc_id 应在 doc_status 中

### - [ ] Step 1: 临时提交备份

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: before v8-Task5 repair_doc_status regression test"
```

### - [ ] Step 2: 写回归测试

Edit `tests/test_lightrag_repair_unit.py`，新增：

```python
def test_repair_doc_status_brainregion_full_doc_id(tmp_path, monkeypatch):
    """脑区 chunk full_doc_id=brain_xxx 应在 doc_status 中出现。"""
    tc = {
        "chunk-brain-1": {
            "content": "文档库脑区: 描述",
            "full_doc_id": "brain_文档库脑区",
            "llm_cache_list": [],
        }
    }
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps(tc))
    (tmp_path / "kv_store_full_docs.json").write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_doc_status

    result = repair_doc_status()

    assert result["status"] == "ok"
    ds = json.loads((tmp_path / "kv_store_doc_status.json").read_text())
    assert "brain_文档库脑区" in ds
    assert ds["brain_文档库脑区"]["status"] == "processed"


def test_repair_doc_status_skip_empty_full_doc_id(tmp_path, monkeypatch):
    """cache fallback chunk 的 full_doc_id="" 应跳过（不写入 doc_status）。"""
    tc = {
        "chunk-active": {
            "content": "chunk 原文",
            "full_doc_id": "",  # cache fallback，空 doc_id
            "llm_cache_list": [],
        }
    }
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps(tc))
    (tmp_path / "kv_store_full_docs.json").write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_doc_status

    result = repair_doc_status()

    assert result["status"] == "ok"
    ds = json.loads((tmp_path / "kv_store_doc_status.json").read_text())
    # 空 full_doc_id 的 chunk 不写入 doc_status
    assert len(ds) == 0
```

### - [ ] Step 3: Pyright + pytest 真实验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./python/bin/python -m pytest tests/test_lightrag_repair_unit.py -xvs -k "repair_doc_status" 2>&1 | tail -40
```

Expected: 2 个新测试通过。

### - [ ] Step 4: 3 真相源 stat + shasum 验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
shasum -a 256 ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
shasum -a 256 ~/.niu/lightrag_storage/kv_store_full_docs.json
shasum -a 256 ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
```

### - [ ] Step 5: 修复权限 + 临时提交

```bash
cd REDACTED_USER_PATH/tools/ai-bot
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
git add -A && git commit -m "test(repair): v8-Task5 repair_doc_status regression tests (brainregion + empty full_doc_id)"
```

---

## Task 6: repair_vdb_chunks/entities/relationships 回归测试

**Files:**
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

v4 的 `repair_vdb_chunks`/`repair_vdb_entities`/`repair_vdb_relationships` 已正确从 GraphML/text_chunks 重建。v8 新增回归测试覆盖：
1. `repair_vdb_entities` 只遍历 GraphML 存在的 node（防复活）
2. `repair_vdb_relationships` 不写 weight（weight 只在 GraphML）
3. `repair_vdb_chunks` 只对 text_chunks 中的 chunk embedding

### - [ ] Step 1: 临时提交备份

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: before v8-Task6 repair_vdb regression tests"
```

### - [ ] Step 2: 写回归测试

Edit `tests/test_lightrag_repair_unit.py`，新增：

```python
def test_repair_vdb_entities_only_graphml_nodes(tmp_path, monkeypatch):
    """repair_vdb_entities 应只遍历 GraphML 存在的 node（防复活）。"""
    _write_graphml_v8(tmp_path, [
        ("entity-active", "person", "desc active", "chunk-a"),
        # 已删实体不在 GraphML 里
    ])

    # text_chunks 含 chunk-a（让 embedding 可调）
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-a": {"content": "content a", "full_doc_id": "doc-1", "llm_cache_list": []},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_vdb_entities

    result = repair_vdb_entities()

    assert result["status"] == "ok"
    vdb_e = json.loads((tmp_path / "vdb_entities.json").read_text())
    # 只含 entity-active，不含已删实体
    assert len(vdb_e.get("data", [])) == 1
    assert vdb_e["data"][0]["__id__"] == "entity-active"


def test_repair_vdb_relationships_no_weight(tmp_path, monkeypatch):
    """repair_vdb_relationships 的 meta_fields 不应含 weight。"""
    _write_graphml_v8(
        tmp_path,
        [("entity-a", "person", "desc a", "chunk-a"), ("entity-b", "person", "desc b", "chunk-b")],
        [("entity-a", "entity-b", "chunk-rel", "desc rel", "关系词")],
    )

    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-a": {"content": "a", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-b": {"content": "b", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-rel": {"content": "rel", "full_doc_id": "doc-1", "llm_cache_list": []},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_vdb_relationships

    result = repair_vdb_relationships()

    assert result["status"] == "ok"
    vdb_r = json.loads((tmp_path / "vdb_relationships.json").read_text())
    for item in vdb_r.get("data", []):
        meta = item.get("__metadata__", {})
        # weight 不应出现在 vdb（只在 GraphML）
        assert "weight" not in meta, f"vdb_relationships 不应写 weight: {meta}"
```

### - [ ] Step 3: Pyright + pytest 真实验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./python/bin/python -m pytest tests/test_lightrag_repair_unit.py -xvs -k "repair_vdb" 2>&1 | tail -50
```

### - [ ] Step 4: 3 真相源 stat + shasum 验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
shasum -a 256 ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
shasum -a 256 ~/.niu/lightrag_storage/kv_store_full_docs.json
shasum -a 256 ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
```

### - [ ] Step 5: 修复权限 + 临时提交

```bash
cd REDACTED_USER_PATH/tools/ai-bot
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
git add -A && git commit -m "test(repair): v8-Task6 repair_vdb regression tests (entities防复活 + relationships无weight)"
```

---

## Task 7: repair_entity/relation/full_* 回归测试

**Files:**
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

v4 的 `repair_entity_chunks`/`repair_relation_chunks`/`repair_full_entities`/`repair_full_relations` 已正确从 GraphML source_id 提取。v8 新增回归测试覆盖：
1. `repair_entity_chunks` 只从 GraphML node source_id 提取（防复活）
2. `repair_relation_chunks` 只从 GraphML edge source_id 提取（防复活）
3. `repair_full_entities`/`repair_full_relations` 从 GraphML + text_chunks.full_doc_id 反向映射

### - [ ] Step 1: 临时提交备份

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: before v8-Task7 repair_entity_relation_full regression tests"
```

### - [ ] Step 2: 写回归测试

Edit `tests/test_lightrag_repair_unit.py`，新增：

```python
def test_repair_entity_chunks_only_graphml_source(tmp_path, monkeypatch):
    """repair_entity_chunks 只从 GraphML node source_id 提取 chunk_ids。"""
    _write_graphml_v8(tmp_path, [
        ("entity-active", "person", "desc", "chunk-a<SEP>chunk-b"),
        # 已删实体不在 GraphML，其 chunk 不应被提取
    ])

    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-a": {"content": "a", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-b": {"content": "b", "full_doc_id": "doc-1", "llm_cache_list": []},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_entity_chunks

    result = repair_entity_chunks()

    assert result["status"] == "ok"
    ec = json.loads((tmp_path / "kv_store_entity_chunks.json").read_text())
    assert "entity-active" in ec
    assert set(ec["entity-active"]) == {"chunk-a", "chunk-b"}


def test_repair_relation_chunks_only_graphml_source(tmp_path, monkeypatch):
    """repair_relation_chunks 只从 GraphML edge source_id 提取 chunk_ids。"""
    _write_graphml_v8(
        tmp_path,
        [("entity-a", "person", "desc a", "chunk-a"), ("entity-b", "person", "desc b", "chunk-b")],
        [("entity-a", "entity-b", "chunk-rel1<SEP>chunk-rel2", "desc", "kw")],
    )

    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps({
        "chunk-a": {"content": "a", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-b": {"content": "b", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-rel1": {"content": "r1", "full_doc_id": "doc-1", "llm_cache_list": []},
        "chunk-rel2": {"content": "r2", "full_doc_id": "doc-1", "llm_cache_list": []},
    }))

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_relation_chunks

    result = repair_relation_chunks()

    assert result["status"] == "ok"
    rc = json.loads((tmp_path / "kv_store_relation_chunks.json").read_text())
    # edge 的 key 是 "src\x1etgt"
    key = "entity-a\x1eentity-b"
    assert key in rc
    assert set(rc[key]) == {"chunk-rel1", "chunk-rel2"}
```

### - [ ] Step 3: Pyright + pytest 真实验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./python/bin/python -m pytest tests/test_lightrag_repair_unit.py -xvs -k "repair_entity_chunks or repair_relation_chunks or repair_full" 2>&1 | tail -50
```

### - [ ] Step 4: 3 真相源 stat + shasum 验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
shasum -a 256 ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
shasum -a 256 ~/.niu/lightrag_storage/kv_store_full_docs.json
shasum -a 256 ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
```

### - [ ] Step 5: 修复权限 + 临时提交

```bash
cd REDACTED_USER_PATH/tools/ai-bot
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
git add -A && git commit -m "test(repair): v8-Task7 repair_entity/relation/full regression tests (防复活)"
```

---

## Task 8: 重写 repair_all（3 真相源不可动 + 删 9 派生 + 重建）

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py:2433-2615`（重写 repair_all）
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

v4 的 `repair_all` 已实现"3 真相源不可动 + 备份 9 派生 + 重建"。v8 微调：
1. 删除 9 派生文件前不再备份（铁律 1：其他文件全删除）
2. `_REBUILD_ORDER` 已在 Task 1 删除违规函数后只剩 9 个 repair 函数，无需修改
3. `_rollback_backup` 改为"从备份目录恢复"（备份目录已存在，不依赖派生文件还在）

### - [ ] Step 1: 临时提交备份

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: before v8-Task8 rewrite repair_all"
```

### - [ ] Step 2: 写失败测试

Edit `tests/test_lightrag_repair_unit.py`，新增：

```python
def test_repair_all_3_truth_sources_intact(tmp_path, monkeypatch):
    """repair_all 完成后 3 真相源 mtime + 内容完全不变。"""
    import os

    # 拷贝真实 3 真相源到 tmp_path（测试用，不动真实数据）
    import shutil
    src_dir = os.path.expanduser("~/.niu/lightrag_storage")
    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    for fname in truth_files:
        shutil.copy2(os.path.join(src_dir, fname), tmp_path / fname)

    # 记录 3 真相源的 stat + 内容 hash
    def _hash(path):
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()
    truth_hashes_before = {f: _hash(tmp_path / f) for f in truth_files}
    truth_mtimes_before = {f: (tmp_path / f).stat().st_mtime for f in truth_files}

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_all

    result = repair_all()

    # 3 真相源 hash + mtime 必须完全不变
    truth_hashes_after = {f: _hash(tmp_path / f) for f in truth_files}
    truth_mtimes_after = {f: (tmp_path / f).stat().st_mtime for f in truth_files}
    assert truth_hashes_after == truth_hashes_before, "3 真相源内容被修改"
    assert truth_mtimes_after == truth_mtimes_before, "3 真相源 mtime 被修改"

    # repair_all 应成功（无 unrecoverable）
    assert not result.get("_unrecoverable", False), f"repair_all 报 unrecoverable: {result.get('_unrecoverable_reason')}"


def test_repair_all_9_derived_files_deleted_and_rebuilt(tmp_path, monkeypatch):
    """repair_all 应删除 9 派生文件后重建。"""
    import os, shutil
    src_dir = os.path.expanduser("~/.niu/lightrag_storage")
    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    for fname in truth_files:
        shutil.copy2(os.path.join(src_dir, fname), tmp_path / fname)

    # 9 派生文件预置空 dict
    derived_files = [
        "kv_store_text_chunks.json", "kv_store_doc_status.json",
        "vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json",
        "kv_store_entity_chunks.json", "kv_store_relation_chunks.json",
        "kv_store_full_entities.json", "kv_store_full_relations.json",
    ]
    for fname in derived_files:
        (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_repair import repair_all

    result = repair_all()

    # 9 派生文件应被重建（存在 + 非空 dict 格式）
    for fname in derived_files:
        assert (tmp_path / fname).exists(), f"{fname} 未被重建"
        data = json.loads((tmp_path / fname).read_text())
        assert isinstance(data, dict), f"{fname} 不是 dict"

    # text_chunks 应有 145 个活跃 chunk（来自真实 GraphML）
    tc = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert len(tc) > 0, "text_chunks 应非空"
```

### - [ ] Step 3: 重写 repair_all

Edit `niu_api/internal/lightrag_repair.py:2433-2615`，重写 repair_all 简化版（删除"备份 9 派生文件"步骤，铁律 1 要求"其他文件全删除"）：

```python
def repair_all() -> dict[str, Any]:
    """v8：3 真相源不可动 + 删 9 派生 + 按需提取重建。

    流程：
    1. 同步 _STORAGE_DIR 到 lightrag_integrity + lightrag_manager
    2. 检测 3 真相源完好性 → 任一损坏 = unrecoverable
    3. 删除 9 个派生文件（铁律 1：不备份，直接删）
    4. 按依赖链重建 9 派生文件（从 GraphML + cache + full_docs 按需提取）
    5. 失败时无法回滚（因为派生文件已删光，真相源从未被修改）

    3 真相源完全不可动（铁律 2）：
    - 不写不改不删（读取是必要的，用于按需提取重建派生文件）
    - 损坏 = unrecoverable
    - 完好 = 一根毫毛不动

    返回扁平结构（向后兼容 Rust format_repair_summary）：
        {
            "text_chunks": {status, ...},
            "doc_status": {status, ...},
            ...
            "_unrecoverable": bool,
            "_unrecoverable_reason": str,
            "_truth_source_check": {...},
            "_deleted": [...],
        }

    注意：repair_all 是同步函数，不能声明 async。
    """
    storage_dir = _storage_dir()
    result: dict[str, Any] = {}

    # 0. 同步 _STORAGE_DIR 到 lightrag_integrity + lightrag_manager
    try:
        from niu_api.internal import lightrag_integrity
        if lightrag_integrity._STORAGE_DIR != _STORAGE_DIR:
            lightrag_integrity._STORAGE_DIR = _STORAGE_DIR
    except Exception:  # noqa: BLE001
        pass
    try:
        import niu_api.internal.lightrag_manager as lightrag_manager
        lightrag_manager._rag_instance = None
        lightrag_manager._init_failed_at = 0
        lightrag_manager._init_error = None
        lightrag_manager.STORAGE_DIR = storage_dir
    except Exception:  # noqa: BLE001
        pass

    # 1. 检测 3 真相源完好性
    truth_check = _check_truth_sources_intact()
    result["_truth_source_check"] = truth_check
    if not truth_check["intact"]:
        result["_unrecoverable"] = True
        reasons = []
        if not truth_check["graphml"]["intact"]:
            reasons.append(f"GraphML: {truth_check['graphml']['reason']}")
        if not truth_check["full_docs"]["intact"]:
            reasons.append(f"full_docs: {truth_check['full_docs']['reason']}")
        if not truth_check["cache"]["intact"]:
            reasons.append(f"cache: {truth_check['cache']['reason']}")
        result["_unrecoverable_reason"] = "3 真相源损坏，无法恢复: " + "; ".join(reasons)
        result["_deleted"] = []  # 真相源损坏时不删派生文件，让用户看到现场
        return result

    # 2. 删除 9 个派生文件（铁律 1：不备份，直接删）
    deleted: list[str] = []
    for fname in _DERIVED_FILES:
        fpath = storage_dir / fname
        if fpath.exists():
            try:
                fpath.unlink()
                deleted.append(fname)
                logger.info(f"[LightRAGRepair] 删除派生文件: {fname}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[LightRAGRepair] 删除 {fname} 失败: {e}")
    result["_deleted"] = deleted

    # 3. 按依赖链重建 9 派生文件
    # 用 getattr 间接查找函数（不直接引用 _REBUILD_ORDER 里的函数对象），
    # 让 monkeypatch 替换模块属性能生效。
    for name, fn in _REBUILD_ORDER:
        try:
            sub_result = fn()
            result[name] = sub_result
            # 任一子任务报 unrecoverable → 顶层标记
            if isinstance(sub_result, dict) and sub_result.get("unrecoverable"):
                result["_unrecoverable"] = True
                result["_unrecoverable_reason"] = (
                    result.get("_unrecoverable_reason", "")
                    + f"; {name}: {sub_result.get('message', '')}"
                )
                logger.error(f"[LightRAGRepair] {name} 报 unrecoverable: {sub_result.get('message')}")
                break  # 任一 unrecoverable 立即停止后续重建
        except Exception as e:  # noqa: BLE001
            logger.error(f"[LightRAGRepair] {name} 重建异常: {e}")
            result[name] = {
                "status": "error",
                "expected": 0,
                "actual": 0,
                "lost": 0,
                "message": f"{name} 重建异常: {e}",
                "unrecoverable": True,
            }
            result["_unrecoverable"] = True
            result["_unrecoverable_reason"] = (
                result.get("_unrecoverable_reason", "")
                + f"; {name} 重建异常: {e}"
            )
            break

    return result
```

**注意**：删除了"备份 9 派生文件"步骤（铁律 1），删除了"回滚"步骤（派生文件已删光，无法回滚；但真相源从未被修改，用户重新跑 repair_all 即可）。

### - [ ] Step 4: 删除 `_rollback_backup` 函数

Edit `niu_api/internal/lightrag_repair.py:2616-2653`，删除 `_rollback_backup`（v8 repair_all 不再调用）。

### - [ ] Step 5: 适配 `_check_truth_sources_intact` 检测标准

Edit `niu_api/internal/lightrag_repair.py:383-419`，确保 `_check_truth_sources_intact` 只检测 3 真相源是否完好（不检测内容，不加任何僵尸检测）：

```python
def _check_truth_sources_intact() -> dict[str, Any]:
    """检测 3 真相源是否完好（铁律 4：只检测 missing/损坏，不加内容检测）。

    返回:
        {
            "intact": bool,
            "graphml": {"intact": bool, "reason": str},
            "full_docs": {"intact": bool, "reason": str},
            "cache": {"intact": bool, "reason": str},
        }
    """
    storage_dir = _storage_dir()
    result = {
        "intact": True,
        "graphml": {"intact": True, "reason": ""},
        "full_docs": {"intact": True, "reason": ""},
        "cache": {"intact": True, "reason": ""},
    }

    # GraphML：必须存在 + xml 解析成功
    graphml_path = storage_dir / _GRAPHML_FILE
    if not graphml_path.exists():
        result["graphml"] = {"intact": False, "reason": "文件不存在"}
        result["intact"] = False
    else:
        try:
            import xml.etree.ElementTree as ET
            ET.parse(graphml_path)
        except Exception as e:
            result["graphml"] = {"intact": False, "reason": f"XML 解析失败: {e}"}
            result["intact"] = False

    # full_docs：必须存在 + JSON 解析成功
    full_docs_path = storage_dir / "kv_store_full_docs.json"
    if not full_docs_path.exists():
        result["full_docs"] = {"intact": False, "reason": "文件不存在"}
        result["intact"] = False
    else:
        loaded = _load_json_dict(full_docs_path)
        if loaded is None:
            result["full_docs"] = {"intact": False, "reason": "JSON 解析失败"}
            result["intact"] = False

    # cache：必须存在 + JSON 解析成功
    cache_path = storage_dir / "kv_store_llm_response_cache.json"
    if not cache_path.exists():
        result["cache"] = {"intact": False, "reason": "文件不存在"}
        result["intact"] = False
    else:
        loaded = _load_json_dict(cache_path)
        if loaded is None:
            result["cache"] = {"intact": False, "reason": "JSON 解析失败"}
            result["intact"] = False

    return result
```

### - [ ] Step 6: Pyright + pytest 真实验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./python/bin/python -m pyright niu_api/internal/lightrag_repair.py 2>&1 | tail -20
./python/bin/python -m pytest tests/test_lightrag_repair_unit.py -xvs -k "repair_all" 2>&1 | tail -40
```

### - [ ] Step 7: 3 真相源 stat + shasum 验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_full_docs.json
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
shasum -a 256 ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
shasum -a 256 ~/.niu/lightrag_storage/kv_store_full_docs.json
shasum -a 256 ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
```

Expected: 3 真相源 mtime + hash 跟 Task 8 开始前一致（repair_all 不动真相源）。

### - [ ] Step 8: 修复权限 + 临时提交

```bash
cd REDACTED_USER_PATH/tools/ai-bot
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
git add -A && git commit -m "refactor(repair): v8-Task8 rewrite repair_all (delete 9 derived no backup + rebuild + no rollback) + simplify _check_truth_sources_intact"
```

---

## Task 9: 重写 run_repair_on_user_request（先停 RegionSync + 不调 get_lightrag/apipeline）

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py:1213-1446`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

v6 的 `run_repair_on_user_request` 含违规代码：
1. 调 `get_lightrag()` 触发 apipeline 初始化（铁律 3）
2. 等 `wait_first_scan_complete` + 二次 repair（违反铁律 4：不加检测逻辑）
3. 调 `get_lightrag_for_repair`（Task 1 已删）

v8 改为：
1. 先停 RegionSync（`stop_background_sync`）避免后台写
2. 设 _repairing=True（让其他线程的 get_lightrag 返回 None）
3. 调 repair_all
4. reset_init_state + 重跑 check_all
5. 不调 get_lightrag/apipeline（让下次用户请求自然触发）
6. 判定 repaired（基于 repair_all._unrecoverable）

### - [ ] Step 1: 临时提交备份

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: before v8-Task9 rewrite run_repair_on_user_request"
```

### - [ ] Step 2: 写失败测试

Edit `tests/test_lightrag_repair_unit.py`，新增：

```python
def test_run_repair_on_user_request_no_get_lightrag_call(tmp_path, monkeypatch):
    """run_repair_on_user_request 不应调 get_lightrag/apipeline（铁律 3）。

    v8：应调 get_region_sync().stop_background_sync() 停 RegionSync（实例方法，非模块函数）。
    """
    import os, shutil
    src_dir = os.path.expanduser("~/.niu/lightrag_storage")
    truth_files = [
        "graph_chunk_entity_relation.graphml",
        "kv_store_full_docs.json",
        "kv_store_llm_response_cache.json",
    ]
    for fname in truth_files:
        shutil.copy2(os.path.join(src_dir, fname), tmp_path / fname)

    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    # 监控 get_lightrag 不应被调（铁律 3）
    # 监控 get_region_sync 被调（v8 停 RegionSync 的正确入口）
    from unittest.mock import patch, MagicMock

    mock_rs = MagicMock()
    with patch("niu_api.internal.lightrag_manager.get_lightrag", side_effect=AssertionError("禁止调 get_lightrag")):
        with patch("agent.injector.region_sync.get_region_sync", return_value=mock_rs) as mock_get_rs:
            from niu_api.internal.lightrag_manager import run_repair_on_user_request

            result = run_repair_on_user_request()

    # 应调 get_region_sync（拿单例）+ stop_background_sync（实例方法）
    mock_get_rs.assert_called()
    mock_rs.stop_background_sync.assert_called_once()
    # finally 块应调 start_background_sync 重启
    mock_rs.start_background_sync.assert_called_once()
    assert "repaired" in result
```

### - [ ] Step 3: 重写 run_repair_on_user_request

Edit `niu_api/internal/lightrag_manager.py:1213-1446`：

```python
def run_repair_on_user_request() -> dict:
    """用户在弹窗点'尝试修复'后调用（通过 /api/kg/lightrag/repair 触发）。

    v8：先停 RegionSync + 不调 get_lightrag/apipeline（铁律 3）。

    修复流程：
        1. 先停 RegionSync（get_region_sync().stop_background_sync）避免后台写
        2. 设 _repairing=True（让其他线程的 get_lightrag 返回 None，作为信号灯兜底）
        3. 调 repair_all
        4. reset_init_state + 重跑 check_all 更新 _integrity_result
        5. 不调 get_lightrag/apipeline（让下次用户请求自然触发）
        6. 判定 repaired（基于 repair_all._unrecoverable）

    RegionSync 停止策略（v8 确认）：
        - `stop_background_sync` / `start_background_sync` 是 `agent.injector.region_sync.RegionSync`
          的实例方法（L602/L615），不是模块级函数，直接调会 NameError
        - 正确调用：`from agent.injector.region_sync import get_region_sync; rs = get_region_sync(); rs.stop_background_sync()`
        - `get_region_sync` 存在于 region_sync.py:690，返回 RegionSync 单例（不存在则创建）
        - RegionSync 内部调 `get_lightrag()`，但 lightrag_manager.get_lightrag() 在 `_repairing=True`
          时返回 None（L925/973），所以即使 stop_background_sync 失败，`_repairing=True` 信号灯
          也能让 RegionSync 的 get_lightrag 拿不到实例，不会写真相源

    Returns:
        {
            "repaired": bool,
            "check_ok": bool,
            "critical_errors": int,
            "major_errors": int,
            "minor_errors": int,
            "repair_result": dict,
            "check_result": dict,
            "_unrecoverable": bool,
        }
    """
    global _integrity_result, _rag_instance, _repairing
    from niu_api.internal.lightrag_repair import repair_all
    from niu_api.internal.lightrag_integrity import check_all

    logger.warning("[LightRAG] 用户选择'尝试修复'，启动 repair_all（v8）")

    # 1. 先停 RegionSync（避免后台写）
    #    stop_background_sync 是 RegionSync 实例方法（region_sync.py:615），不是模块级函数
    #    正确调用：get_region_sync() 拿单例，再调实例方法
    region_sync_stopped = False
    try:
        from agent.injector.region_sync import get_region_sync
        rs = get_region_sync()
        if rs is not None:
            rs.stop_background_sync()
            region_sync_stopped = True
            logger.info("[LightRAG] RegionSync 已停止（通过 get_region_sync().stop_background_sync）")
        else:
            logger.info("[LightRAG] RegionSync 单例为 None（未启动），跳过停止")
    except Exception as e:  # noqa: BLE001
        # stop 失败不阻塞 repair：_repairing=True 信号灯会让 RegionSync 内部的
        # get_lightrag() 返回 None（lightrag_manager.py:925/973 检查 _repairing），
        # 自然不会写真相源
        logger.warning(f"[LightRAG] 停 RegionSync 失败（继续 repair，靠 _repairing 信号灯兜底）: {e}")

    _repairing = True
    try:
        # 2. repair 期间置 _rag_instance = None
        _rag_instance = None

        # 3. 调 repair_all（v8：不备份，直接删 9 派生 + 重建）
        repair_result = repair_all()

        # 4. 检查 unrecoverable
        has_unrecoverable = bool(repair_result.get("_unrecoverable", False)) or any(
            isinstance(v, dict) and v.get("unrecoverable")
            for v in repair_result.values()
            if isinstance(v, dict)
        )

        # 5. reset + 重跑 check_all
        reset_init_state()
        try:
            check_result = check_all()
            _integrity_result = check_result
        except Exception as e:
            logger.warning(f"[LightRAG] 修复后 check_all 失败: {e}")
            check_result = _integrity_result or {}

        # 6. v8：不调 get_lightrag/apipeline（铁律 3）
        # 让下次用户请求自然触发 get_lightrag 初始化（从 repair 后的磁盘重建）

        # 7. 判定 repaired（基于 repair_all._unrecoverable）
        repaired = not has_unrecoverable and not repair_result.get("_unrecoverable", False)

        for vdb_name, vdb_result in repair_result.items():
            if not isinstance(vdb_result, dict):
                continue
            if vdb_result.get("status") == "error":
                repaired = False
                logger.warning(
                    f"[LightRAG] 修复失败项: {vdb_name} - {vdb_result.get('message', '')}"
                )

        critical = check_result.get("critical_errors", 0)
        major = check_result.get("major_errors", 0)
        minor = check_result.get("minor_errors", 0)

        logger.info(
            f"[LightRAG] 修复完成: repaired={repaired}, "
            f"重检: critical={critical}, major={major}, minor={minor}"
        )

        return {
            "repaired": repaired,
            "check_ok": check_result.get("ok", True),
            "critical_errors": critical,
            "major_errors": major,
            "minor_errors": minor,
            "repair_result": repair_result,
            "_unrecoverable": bool(repair_result.get("_unrecoverable", False)),
            "check_result": check_result,
        }
    except Exception as e:
        logger.error(f"[LightRAG] 修复失败: {e}")
        return {
            "repaired": False,
            "check_ok": False,
            "critical_errors": 0,
            "major_errors": 0,
            "minor_errors": 0,
            "repair_result": {"error": str(e)},
            "check_result": _integrity_result,
        }
    finally:
        _repairing = False
        # 尝试重启 RegionSync（下次用户请求自然触发，这里不主动调 get_lightrag）
        # start_background_sync 同样是 RegionSync 实例方法（region_sync.py:602）
        try:
            from agent.injector.region_sync import get_region_sync
            rs = get_region_sync()
            if rs is not None:
                rs.start_background_sync()
                logger.info("[LightRAG] RegionSync 已重启（通过 get_region_sync().start_background_sync）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LightRAG] 重启 RegionSync 失败: {e}")
```

**注意**：
- 删除了"等 wait_first_scan_complete + 二次 repair"（铁律 4：不加检测逻辑）
- `stop_background_sync`/`start_background_sync` 是 `RegionSync` 实例方法（region_sync.py:615/602），必须通过 `get_region_sync()` 拿单例后调实例方法，不能当模块级函数调
- `get_region_sync` 存在于 region_sync.py:690，返回 RegionSync 单例（不存在则创建）
- 停 RegionSync 失败时不阻塞 repair：`_repairing=True` 信号灯会让 RegionSync 内部的 `get_lightrag()` 返回 None（lightrag_manager.py:925/973），自然不会写真相源

### - [ ] Step 4: 确认 stop_background_sync + start_background_sync 实例方法存在

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
grep -n "def stop_background_sync\|def start_background_sync\|def get_region_sync\|class RegionSync" agent/injector/region_sync.py 2>&1
```

Expected（v8 已确认）：
- `class RegionSync` 在 region_sync.py:58
- `def start_background_sync` 在 region_sync.py:602（实例方法）
- `def stop_background_sync` 在 region_sync.py:615（实例方法）
- `def get_region_sync` 在 region_sync.py:690（模块级函数，返回 RegionSync 单例）

如果以上函数不存在或行号不符，需要在 Task 9 实现前先确认正确的停止 RegionSync 的方式。v8 已验证：`get_region_sync()` 拿单例后调 `rs.stop_background_sync()` 是正确路径。

### - [ ] Step 5: Pyright + pytest 真实验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./python/bin/python -m pyright niu_api/internal/lightrag_manager.py 2>&1 | tail -20
./python/bin/python -m pytest tests/test_lightrag_repair_unit.py -xvs -k "run_repair_on_user_request" 2>&1 | tail -40
```

### - [ ] Step 6: 3 真相源 stat + shasum 验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_full_docs.json
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
shasum -a 256 ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
shasum -a 256 ~/.niu/lightrag_storage/kv_store_full_docs.json
shasum -a 256 ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
```

### - [ ] Step 7: 修复权限 + 临时提交

```bash
cd REDACTED_USER_PATH/tools/ai-bot
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
git add -A && git commit -m "refactor(repair): v8-Task9 rewrite run_repair_on_user_request (stop RegionSync + no get_lightrag/apipeline + no二次repair)"
```

---

## Task 10: e2e 真实数据测试（./niu 启动 + 真实 LLM + 145 活跃 chunk 全恢复）

**Files:**
- Manual test

### 背景

Task 10 是最终验收。用真实 `./niu` 启动走完整 repair 流程，验证：
1. 3 真相源 mtime + hash 不变
2. 145 个活跃 chunk 全部恢复（lost==0）
3. 8 个脑区 chunk 正确构造（content + full_doc_id 格式正确）
4. weight 衰减值保留（GraphML 不被修改）
5. 已删实体不复活（GraphML 仍不含已删实体）
6. 9 派生文件全部重建

### - [ ] Step 1: 临时提交备份

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: before v8-Task10 e2e real data test"
```

### - [ ] Step 2: 记录测试前 3 真相源基线

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
echo "=== 测试前 3 真相源基线 ==="
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_full_docs.json
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
shasum -a 256 ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
shasum -a 256 ~/.niu/lightrag_storage/kv_store_full_docs.json
shasum -a 256 ~/.niu/lightrag_storage/kv_store_llm_response_cache.json

echo "=== 测试前 9 派生文件状态 ==="
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_text_chunks.json 2>/dev/null || echo "text_chunks 不存在"
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/vdb_entities.json 2>/dev/null || echo "vdb_entities 不存在"

echo "=== GraphML 活跃 chunk 数 ==="
./python/bin/python -c "
import xml.etree.ElementTree as ET
tree = ET.parse('REDACTED_USER_PATH/.niu/lightrag_storage/graph_chunk_entity_relation.graphml')
root = tree.getroot()
ns = '{http://graphml.graphdrawing.org/xmlns}'
graph = root.find(f'{ns}graph')
active = set()
brainregion = 0
for n in graph.findall(f'{ns}node'):
    etype = src = ''
    for d in n.findall(f'{ns}data'):
        if d.get('key')=='d1': etype = d.text or ''
        elif d.get('key')=='d3': src = d.text or ''
    if etype == 'brainregion': brainregion += 1
    if src: active.update(c for c in src.split('<SEP>') if c)
for e in graph.findall(f'{ns}edge'):
    for d in e.findall(f'{ns}data'):
        if d.get('key')=='d10':
            src = d.text or ''
            if src: active.update(c for c in src.split('<SEP>') if c)
print(f'active chunk_id: {len(active)}')
print(f'brainregion nodes: {brainregion}')
"
```

记录基线输出（必须附真实输出）。

### - [ ] Step 3: 启动 ./niu 触发 check_all + curl 触发 repair_all

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
# 编译最新 Rust 启动器（CLAUDE.md 铁律 8：用 launcher/build.sh，不用 cargo build）
./launcher/build.sh 2>&1 | tail -5

# 启动 ./niu（后台运行，./niu 启动只跑 check_all 检测，不会自动跑 repair_all）
./niu &
NIU_PID=$!
echo "niu PID: $NIU_PID"

# 等 10 秒让 API 启动（check_all 在后台跑）
sleep 10

# Critical：./niu 启动不会自动触发 repair_all，必须通过 /api/kg/lightrag/repair 端点触发
# repair_all 只通过用户点"尝试修复"按钮触发 → 这里用 curl 模拟点击
echo "=== curl 触发 repair_all ==="
curl -sS -X POST "http://127.0.0.1:9876/api/kg/lightrag/repair?target=all" --max-time 300 2>&1 | tail -20

# 等 60 秒让 repair_all 跑完（真实 LLM 调用 + 145 chunk embedding 需要时间）
sleep 60

# 检查进程状态
ps -p $NIU_PID > /dev/null 2>&1 && echo "niu 进程存活" || echo "niu 进程已退出"

# 优雅退出（CLAUDE.md test-process-kill-corruption 铁律：用 SIGTERM，不用 pkill -9）
kill -TERM $NIU_PID 2>/dev/null
sleep 5
ps -p $NIU_PID > /dev/null 2>&1 && echo "niu 进程仍存活，等 SIGKILL" && kill -9 $NIU_PID 2>/dev/null
```

**注意**：`./niu` 启动只跑 check_all 检测（写入 `_integrity_result`），不会自动跑 repair_all。repair_all 只通过 `/api/kg/lightrag/repair` 端点触发（用户点"尝试修复"按钮）。v8 Task 10 必须用 curl 显式触发 repair_all，否则 e2e 测试只验证了 check_all，没验证 repair_all。

### - [ ] Step 4: 验证 3 真相源 + 9 派生文件

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
echo "=== 测试后 3 真相源（必须 mtime + hash 不变）==="
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_full_docs.json
stat -f "%Sm %z %N" ~/.niu/lightrag_storage/kv_store_llm_response_cache.json
shasum -a 256 ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
shasum -a 256 ~/.niu/lightrag_storage/kv_store_full_docs.json
shasum -a 256 ~/.niu/lightrag_storage/kv_store_llm_response_cache.json

echo "=== 测试后 9 派生文件（应全部存在 + 非空）==="
for f in kv_store_text_chunks.json kv_store_doc_status.json vdb_chunks.json vdb_entities.json vdb_relationships.json kv_store_entity_chunks.json kv_store_relation_chunks.json kv_store_full_entities.json kv_store_full_relations.json; do
    stat -f "%Sm %z %N" ~/.niu/lightrag_storage/$f 2>/dev/null || echo "$f 不存在"
done

echo "=== text_chunks 活跃 chunk 恢复数（应 145，lost=0）==="
./python/bin/python -c "
import json
tc = json.load(open('REDACTED_USER_PATH/.niu/lightrag_storage/kv_store_text_chunks.json'))
print(f'text_chunks 条数: {len(tc)}')
# 统计脑区 chunk
brain_count = sum(1 for v in tc.values() if isinstance(v, dict) and str(v.get('full_doc_id','')).startswith('brain_'))
print(f'脑区 chunk 数: {brain_count}')
"

echo "=== vdb_entities 条数（应 2201 = GraphML nodes 数）==="
./python/bin/python -c "
import json
vdb_e = json.load(open('REDACTED_USER_PATH/.niu/lightrag_storage/vdb_entities.json'))
print(f'vdb_entities 条数: {len(vdb_e.get(\"data\", []))}')
"

echo "=== vdb_relationships 条数（应 3725 = GraphML edges 数）==="
./python/bin/python -c "
import json
vdb_r = json.load(open('REDACTED_USER_PATH/.niu/lightrag_storage/vdb_relationships.json'))
print(f'vdb_relationships 条数: {len(vdb_r.get(\"data\", []))}')
"
```

Expected:
- 3 真相源 mtime + hash 跟 Step 2 基线完全一致
- 9 派生文件全部存在
- text_chunks 条数 ≈ 145（lost=0）
- 脑区 chunk 数 = 8
- vdb_entities 条数 = 2201
- vdb_relationships 条数 = 3725

### - [ ] Step 5: 修复权限 + 最终提交

```bash
cd REDACTED_USER_PATH/tools/ai-bot
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \;
git add -A && git commit -m "test(repair): v8-Task10 e2e real data test passed (3 truth sources intact + 145 active chunks recovered + 8 brainregion chunks constructed + 2201 entities + 3725 relationships)"
```

---

## Self-Review Checklist

- [ ] **铁律 1**：repair_all 第一步只保留 3 真相源，其他 9 派生文件全删除（不备份）
- [ ] **铁律 2**：GraphML 是唯一真相源，有多少条恢复多少条；full_docs + cache 是日志类型全量辅助文档，按需提取最后一条匹配记录（多条取 create_time 最大）
- [ ] **铁律 3**：所有写 3 真相源的代码段全删除（6 个违规函数 + _embed_batch fallback + get_lightrag_for_repair + _rebuild_vdb_matrix）
- [ ] **铁律 4**：所有不在 GraphML 读信息做恢复的操作全删除（不加僵尸检测）
- [ ] cache original_prompt 是主补充源（正则提取 ``` 之间 chunk 原文，多条取 create_time 最大）
- [ ] cache 的 return 不当 chunk 原文
- [ ] full_docs 是 fallback（cache 找不到时 chunking 反查）
- [ ] 脑区节点直接从 GraphML 构造（content=`{脑区名}: {d2}`，full_doc_id=`brain_{脑区名}`）
- [ ] tokenizer 独立加载（TiktokenTokenizer，不调 get_lightrag_for_repair）
- [ ] embedding 独立加载（niu_api.internal.embedding.get_model，不调 get_lightrag_for_repair）
- [ ] run_repair_on_user_request 先停 RegionSync（get_region_sync().stop_background_sync 实例方法）+ _repairing 信号灯兜底 + 不调 get_lightrag/apipeline
- [ ] 3 真相源完全不可动（mtime + hash 不变）
- [ ] 145 个活跃 chunk 全部恢复（lost==0）
- [ ] 8 个脑区 chunk 正确构造
- [ ] vdb_entities = 2201（防复活）
- [ ] vdb_relationships = 3725（weight 不写 vdb）
- [ ] 每次测试后真实 stat + shasum 3 真相源（附真实输出，不撒谎）
- [ ] Pyright + pytest 真实跑（附真实输出，不撒谎）

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-07-16-lightrag-rebuild-from-graphml-truth-v8.md`.**

**执行方式（CLAUDE.md 铁律 2）：**
- 主对话是项目经理，不自己改代码
- 每个 Task 派子 Agent 执行，主对话只验收
- 子 Agent 提示词必须包含本方案"subagent 提示词强制要求"章节的全部内容
- 每个 Task 完成后主对话验收：Pyright + pytest + 3 真相源 stat + shasum 真实输出
- 验收通过才进入下一个 Task

**建议执行顺序：** Task 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10

**Task 1 是阻塞依赖**：删违规函数后 Task 4 重写 repair_text_chunks 才能用独立 tokenizer。Task 3 扩展 _load_graphml_nodes 后 Task 4 才能识别脑区节点。
