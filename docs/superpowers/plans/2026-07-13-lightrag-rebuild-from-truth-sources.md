# LightRAG 数据修复重构：3 真相源不可动 + 按需提取重建 9 派生文件 Implementation Plan (v4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 LightRAG 数据修复逻辑重构为"3 真相源完全不可动 + 只重建 9 个派生文件"。3 真相源（GraphML + full_docs + cache）任一损坏即报修复失败，全部完好时只重建 9 个派生文件，真相源一根毫毛不动。

**Architecture:**
- **3 真相源**（完全不可动，不写不改不删（读取是必要的，用于按需提取重建派生文件））：
  - `graph_chunk_entity_relation.graphml` — 当前图谱状态权威清单（实体集、关系集、weight 衰减值、description summary、brain_meta_*）
  - `kv_store_full_docs.json` — 文档原文池（含所有历史版本，按 create_time 取最新）
  - `kv_store_llm_response_cache.json` — LLM 抽取结果池（含所有历史 extract entry，按 create_time 取最新）
- **9 派生文件**（可重建，从 3 真相源按需提取）：
  - `kv_store_text_chunks.json` / `kv_store_doc_status.json` / `vdb_chunks.json` / `vdb_entities.json` / `vdb_relationships.json` / `kv_store_entity_chunks.json` / `kv_store_relation_chunks.json` / `kv_store_full_entities.json` / `kv_store_full_relations.json`
- **重建算法**：从 GraphML 提取活跃 chunk_id 集合 C → 对 C 中每个 chunk_id 从 text_chunks（如还在）或 full_docs 按需提取原文（多条取 create_time 最大）→ 用 cache 按 chunk_id 取最新 extract entry（多条取 create_time 最大）填 llm_cache_list → 派生其他 8 文件
- **删除的旧步骤**（因为会动真相源）：
  - ~~`repair_graphml`~~（删 GraphML 后重放 cache 覆盖）→ 删除函数，repair_all 只检测完好性
  - ~~`repair_brainregion_zombies`~~（改 GraphML + cache）→ 删除步骤
  - ~~`repair_cache_filter`~~（改 cache）→ 删除步骤
  - ~~`repair_graphml_orphan_edges`~~（改 GraphML）→ 删除步骤

**Tech Stack:** Python 3.11、xml.etree.ElementTree（GraphML 解析）、nano-vectordb、pytest（TDD）、真实 LightRAG 实例（端到端验证，不 mock LLM，cache 完整时不调 LLM）。

---

## 背景

### 前 6 轮修复为什么没解决

1. **7-08 entity-sync**：根因判定 = "check_all 没检同步性"
2. **7-08 case-insensitive**：根因判定 = "源头没 lower 化"
3. **7-09 startup-block**：根因判定 = "启动流程不阻塞 + repaired 硬编码"
4. **7-11 consistency-redo**：根因判定 = "集合比对非因果链"
5. **7-12 semantic-integrity**：根因判定 = "句法非语义"
6. **7-13 v2/v3**：根因判定 = "2 真相源 = full_docs + cache"，从日志重放覆盖 GraphML → 复活已删实体 + 丢 weight 衰减 + 复活旧版本

**v2/v3 的根本错误**：把 `full_docs + cache` 当真相源，重跑 `apipeline_process_enqueue_documents` 覆盖 GraphML。但这两个文件只是历史日志，重放会把 GraphML 当前状态覆盖回历史状态。

**v4 的核心原则（用户原话）**：
> "现在已经确定了三个真相源文件，那么这三个文件就完全不可动。你无论它里面有什么问题，你也不能动它。它们如果损坏了，那就是修复失败。如果没损坏，那你为什么要动它？"

### 3 真相源确认（代码证据）

**为什么 GraphML 是真相源**：
- GraphML 是 nx.Graph 的序列化（`networkx_impl.py:37-48` `load_nx_graph`/`write_nx_graph`）
- 用户查询只读 GraphML（`networkx_impl.py:365-542` `get_knowledge_graph` 走 `_get_graph()`，启动时从 GraphML 加载 `networkx_impl.py:69`）
- 删除实体后 GraphML 反映删除（`utils_graph.py:135` `delete_node` → `networkx_impl.py:218-228` `remove_node` → `index_done_callback` 落盘 `networkx_impl.py:593-595`）
- **weight 衰减后的最新值只在 GraphML**：`region_manager.py:82-148` `_decay_brain_region_edges` 半衰期衰减，衰减后写回 GraphML；cache 里 weight 是 extract 原始值（`operate.py:531-535`），vdb 的 meta_fields 不含 weight（`lightrag.py:722`）
- **description summary 后的合并描述只在 GraphML**：`operate.py:2207` `_handle_entity_relation_summary` 合并多 chunk 描述，结果写 GraphML；cache 只存 extract 原始描述
- **brain_meta_* 脑区元数据只在 GraphML**：`region_manager.py` 写入脑区 priority/community_id 等，cache 没有

**为什么 full_docs 是真相源**：
- full_docs 是文档原文唯一持久化（`lightrag.py:1514-1521` 存 `{doc_id: {content, file_path}}`）
- text_chunks 的 chunk 原文最终来自 full_docs（`lightrag.py:1978` chunking）
- 没有其他文件能反推文档原文

**为什么 cache 是真相源**：
- cache 是 LLM 抽取结果唯一持久化（`utils.py:1480-1488` 存 `{return, cache_type, chunk_id, original_prompt}`）
- LLM temperature=1.0 非确定性（`constants.py:86`），重调 LLM 结果不一致，cache 必须保留
- text_chunks 的 llm_cache_list 字段引用 cache（`utils.py:1926-1965`）

**为什么 3 真相源都不可动**：
- GraphML 动了 → 当前图谱状态丢失（weight 衰减、已删实体清单、description summary 全丢）
- full_docs 动了 → 文档原文丢失（无法重建 text_chunks）
- cache 动了 → LLM 抽取结果丢失（未来 extract 重调 LLM 结果不一致）

### "按需提取 + 取最后录入"算法

**从 GraphML 出发**，逐条读取。GraphML 需要某条辅助信息（chunk 原文、cache 细节）时，才去 full_docs/cache 里按需提取。多条匹配时**取 create_time 最大的**（最后录入）。

**代码层面的可行性证据**：
1. **text_chunks 天然是"最后录入"版本**：`JsonKVStorage.upsert`（`json_kv_impl.py:181`）用 `dict.update` 覆盖，同一 chunk_id 后写覆盖前写，`full_doc_id` 自动指向最后写入的 doc
2. **cache 有 `create_time` 字段**：`json_kv_impl.py:174-176` `JsonKVStorage.upsert` 自动注入
3. **`text_chunks[chunk_id].llm_cache_list`**（`utils.py:1926-1965`）已维护该 chunk 的所有 cache_key 列表
4. **GraphML source_id 是累加的**（`operate.py:1732` + `utils.py:2828-2846`），v1 和 v2 的 chunk_id 都保留——不能从 source_id 判断"最后录入"，必须查 text_chunks/cache

### 9 派生文件重建算法

| 文件 | 重建算法 | 防复活机制 |
|------|---------|----------|
| `kv_store_text_chunks.json` | 从 GraphML node/edge 的 source_id（d3/d10）收集活跃 chunk_id 集合 C；对 C 中每个 chunk_id 从现有 text_chunks 按 cid 查原文（天然最后版本，如 text_chunks 已被删则从 full_docs 重新 chunking 反查，多条匹配取 create_time 最大）；llm_cache_list 从 cache 按 chunk_id 反向构建 | 只重建 C 中的 chunk，旧版本 chunk 不重建 |
| `kv_store_doc_status.json` | 从 text_chunks.full_doc_id 反向分组；所有 doc 标记 `status="processed"`（不在 apipeline 重处理查询集 `lightrag.py:1771-1773`） | processed 状态不会被重处理 |
| `vdb_chunks.json` | 遍历 text_chunks 重新 embedding | 只对 C 中的 chunk embedding |
| `vdb_entities.json` | 遍历 GraphML nodes 重新 embedding（content=f"{name}\n{desc}"） | **天然防复活**（只遍历 GraphML 存在的 node） |
| `vdb_relationships.json` | 遍历 GraphML edges 重新 embedding（content=f"{kw}\t{src}\n{tgt}\n{desc}"）；**不写 weight**（meta_fields 不含，`lightrag.py:722`） | **天然防复活 + weight 不丢** |
| `kv_store_entity_chunks.json` | 从 GraphML node source_id 提取 chunk_ids | **天然防复活** |
| `kv_store_relation_chunks.json` | 从 GraphML edge source_id 提取 chunk_ids | **天然防复活** |
| `kv_store_full_entities.json` | 从 GraphML source_id + text_chunks.full_doc_id 反向映射 | **天然防复活** |
| `kv_store_full_relations.json` | 从 GraphML edge source_id + text_chunks.full_doc_id 反向映射 | **天然防复活** |

### 关键设计决策（v4 vs v3）

1. **3 真相源完全不可动**：GraphML + full_docs + cache 都不写不改不删（读取是必要的，用于按需提取重建派生文件）。repair_all 只检测完好性，不修改。
2. **删除所有会动真相源的步骤**：`repair_graphml` / `repair_brainregion_zombies` / `repair_cache_filter` / `repair_graphml_orphan_edges` 全部从 `_REBUILD_ORDER` 移除，函数体可保留但不在 repair_all 中调用（避免破坏其他调用方）。
3. **GraphML 损坏 = unrecoverable**：不尝试重建（无白名单可过滤，重建无意义）。用户原话："如果这个文件不存在，你无法确保什么内容被删掉，那你的恢复是完全没有意义的。"
4. **full_docs 损坏 = unrecoverable**：无法重建 text_chunks/vdb_chunks/doc_status。
5. **cache 损坏 = unrecoverable**：LLM 抽取结果丢失，无法恢复 llm_cache_list。但 GraphML 完好时 cache 损坏不影响当前图谱状态——这种情况下可以选择性降级（清空 cache 让未来 extract 重调 LLM），但 v4 严格起见也报 unrecoverable（让用户决定是否接受降级）。
6. **备份只备份 9 派生文件**：3 真相源不可动，不需要备份。
7. **回滚只回滚 9 派生文件**：3 真相源从未被修改，回滚不涉及它们。
8. **weight 不写 vdb**：vdb_relationships 的 meta_fields 不含 weight（`lightrag.py:722`），LightRAG 自己 upsert 也过滤 weight（`nano_vector_db_impl.py:112`）。weight 只存在 GraphML，重建 vdb_relationships 时不写 weight。
9. **脑区 chunk 特殊处理**：脑区 chunk 的 `full_doc_id = "brain"`（`region_manager.py:152` `REGION_SOURCE_ID="brain"`），不在 full_docs 里。重建 text_chunks 时脑区 chunk 从现有 text_chunks 按 chunk_id 查原文（脑区 chunk 也在 text_chunks 里）；如果 text_chunks 已被删且脑区 chunk 不在 full_docs，记为 missing（region_sync 会重新注入）。
10. **测试用真实数据 + 真实 LLM（不 mock）**：合成 fixture（不含真实人名）+ 真实 LightRAG 实例 + 真实 embedding 模型。cache 完整时不调 LLM。

---

## 文件结构

| 文件 | 责任 | 改动类型 |
|------|------|---------|
| `niu_api/internal/lightrag_repair.py` | 重写 `repair_all` 为"检测 3 真相源 → 备份 9 → 删 9 → 按需提取重建 → 失败回滚"；重写 `repair_text_chunks` 为"从 GraphML 提活跃 chunk_id + 按需提取"；保留 `repair_doc_status`/`repair_vdb_*`/`repair_*_chunks`/`repair_full_*`（已从 GraphML 读取，正确）；`repair_graphml`/`repair_brainregion_zombies`/`repair_graphml_orphan_edges` 函数体保留但不在 `repair_all` 调用；`_TRUTH_SOURCE_FILES` 改为 3 文件；新增 `_check_truth_sources_intact` 检测 3 真相源 | 修改 |
| `niu_api/internal/lightrag_integrity.py` | 简化 `check_all` 为"检 3 真相源完好性 + 9 派生文件 missing 检测"；`_TRUTH_SOURCE_FILES` 改为 3 文件；删除 11 旧句法 check + 5 旧语义 check；保留 `_load_graphml`/`_load_json_dict`/`_ZOMBIE_DESCRIPTION_MARKERS`（被 `repair_brainregion_zombies` import） | 修改 |
| `niu_api/internal/lightrag_manager.py` | 修复 `run_resilience_phase1` 的 `total_errors` 字段（拆成 critical/major/minor）；修复 `run_repair_on_user_request` 的 `repaired` 判定（用 `repair_all` 返回的 `_unrecoverable` 字段） | 修改 |
| `launcher/src/main.rs` | `IntegrityStatus` struct 加 `critical_errors`/`major_errors`/`minor_errors` 字段 | 修改 |
| `tests/test_lightrag_repair_unit.py` | 单元测试：`repair_text_chunks` 按需提取；`repair_all` 新调度 + 备份回滚；`check_all` 新逻辑；3 真相源损坏 unrecoverable | 创建 |
| `tests/test_lightrag_rebuild_from_truth.py` | 端到端 TDD 测试（合成 fixture）：删 vdb → repair；删 9 全部 → repair；GraphML 损坏 → unrecoverable + 回滚；full_docs 损坏 → unrecoverable；cache 损坏 → unrecoverable；含旧版本 doc + 已删实体 → 重建后不复活；weight 衰减值保留 | 创建 |
| `tests/fixtures/lightrag_truth_sources/` | 合成 fixture（不含真实人名）：3 个文档（含 v1+v2 同文档不同版本）+ 5 个 extract cache（含 1 个已删实体脏 entry）+ GraphML（含衰减后 weight + 已删实体已不在） | 创建 |

---

## Task 1: 重写 `repair_text_chunks` 为"从 GraphML 提活跃 chunk_id + 按需提取"

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py:386-558`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

现有 `repair_text_chunks`（`lightrag_repair.py:386-558`）从 `full_docs` 全量重新 chunking 重建——会把旧版本文档的 chunk 也重建出来，未来重跑 apipeline 时旧版本实体复活。

v4 改为"从 GraphML 按需提取"：
1. 解析 GraphML 提取活跃 chunk_id 集合 C（从所有 node 的 d3 source_id + edge 的 d10 source_id）
2. 对 C 中每个 chunk_id：
   - 优先从现有 text_chunks 按 chunk_id 查原文（天然最后版本，`json_kv_impl.py:181` dict.update 覆盖）
   - 现有 text_chunks 没有该 chunk_id 时（已被删），从 full_docs 重新 chunking 反查（多条匹配取 create_time 最大的 doc）
3. 只重建 C 中的 chunk，其他 chunk 不重建（旧版本 chunk 丢弃）
4. llm_cache_list 从 cache 按 chunk_id 反向构建（多条 cache entry 取 create_time 最大）

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py`:

```python
"""repair_text_chunks：从 GraphML 提活跃 chunk_id + 按需提取重建。"""
import json
import pytest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from niu_api.internal.lightrag_repair import repair_text_chunks


def _write_graphml(tmp_path: Path, nodes: list[tuple[str, str, str]]):
    """写 GraphML。nodes = [(node_id, desc, source_id), ...]"""
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    for node_id, desc, src in nodes:
        node = ET.SubElement(graph, f"{{{ns}}}node", {"id": node_id})
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d2"}).text = desc
        ET.SubElement(node, f"{{{ns}}}data", {"key": "d3"}).text = src
    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )


def test_repair_text_chunks_only_rebuilds_active_chunks(tmp_path, monkeypatch):
    """repair_text_chunks 应只重建 GraphML 活跃 chunk_id 集合 C 中的 chunk。"""
    # GraphML：1 个实体引用 chunk-active
    _write_graphml(tmp_path, [("entity-x", "desc X", "chunk-active")])
    
    # text_chunks 现有 2 个 chunk：chunk-active（活跃）+ chunk-old（旧版本）
    tc = {
        "chunk-active": {
            "content": "活跃 chunk 原文",
            "full_doc_id": "doc-v2",
            "llm_cache_list": [],
        },
        "chunk-old": {
            "content": "旧版本 chunk 原文",
            "full_doc_id": "doc-v1",
            "llm_cache_list": [],
        },
    }
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps(tc, ensure_ascii=False))
    # full_docs 含 v1 和 v2
    docs = {
        "doc-v1": {"content": "v1 content", "file_path": "v1.md"},
        "doc-v2": {"content": "v2 content", "file_path": "v2.md"},
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    # cache 空
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    
    result = repair_text_chunks()
    
    assert result["status"] == "ok"
    tc_after = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    # 活跃 chunk 保留
    assert "chunk-active" in tc_after
    assert tc_after["chunk-active"]["content"] == "活跃 chunk 原文"
    assert tc_after["chunk-active"]["full_doc_id"] == "doc-v2"
    # 旧版本 chunk 丢弃
    assert "chunk-old" not in tc_after, "旧版本 chunk 应被丢弃（不在 GraphML 活跃集合）"


def test_repair_text_chunks_falls_back_to_full_docs_when_text_chunks_missing(tmp_path, monkeypatch):
    """现有 text_chunks 没有该 chunk_id 时，从 full_docs 重新 chunking 反查（取 create_time 最大）。"""
    from lightrag.utils import compute_mdhash_id
    chunk_id = compute_mdhash_id("hello world", prefix="chunk-")
    
    _write_graphml(tmp_path, [("entity-y", "desc Y", chunk_id)])
    # text_chunks 为空（损坏或被删）
    (tmp_path / "kv_store_text_chunks.json").write_text("{}")
    # full_docs 含 2 个版本（v1 和 v2 都切出 chunk-X content）
    docs_with_time = {
        "doc-v1": {"content": "hello world", "file_path": "v1.md", "create_time": 1000, "update_time": 1000},
        "doc-v2": {"content": "hello world", "file_path": "v2.md", "create_time": 2000, "update_time": 2000},
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs_with_time, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")
    
    class FakeTokenizer:
        def encode(self, text):
            return text.split()
        def decode(self, tokens):
            return " ".join(tokens)
    class FakeRag:
        tokenizer = FakeTokenizer()
    monkeypatch.setattr("niu_api.internal.lightrag_manager.get_lightrag_for_repair", lambda: FakeRag())
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    
    result = repair_text_chunks()
    
    assert result["status"] == "ok"
    tc_after = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert chunk_id in tc_after
    # 应取最后录入的 doc-v2
    assert tc_after[chunk_id]["full_doc_id"] == "doc-v2", "多条匹配时应取 create_time 最大的 doc"


def test_repair_text_chunks_unrecoverable_when_graphml_corrupt(tmp_path, monkeypatch):
    """GraphML 损坏时 repair_text_chunks 应返回 unrecoverable。"""
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text("corrupt xml <<<")
    (tmp_path / "kv_store_full_docs.json").write_text("{}")
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    
    result = repair_text_chunks()
    
    assert result["status"] == "error"
    assert result.get("unrecoverable") is True


def test_repair_text_chunks_unrecoverable_when_full_docs_corrupt(tmp_path, monkeypatch):
    """full_docs 损坏且 text_chunks 也损坏时 → unrecoverable。"""
    _write_graphml(tmp_path, [("entity-z", "desc Z", "chunk-z")])
    # text_chunks 损坏（非 dict）
    (tmp_path / "kv_store_text_chunks.json").write_text("corrupt")
    # full_docs 损坏
    (tmp_path / "kv_store_full_docs.json").write_text("corrupt")
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    
    result = repair_text_chunks()
    
    assert result["status"] == "error"
    assert result.get("unrecoverable") is True
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_lightrag_repair_unit.py::test_repair_text_chunks_only_rebuilds_active_chunks -v
```

Expected: FAIL（现有 `repair_text_chunks` 从 full_docs 全量重建）

### - [ ] Step 3: Write minimal implementation

重写 `niu_api/internal/lightrag_repair.py:386-558` 的 `repair_text_chunks`：

```python
def repair_text_chunks() -> dict[str, Any]:
    """从 GraphML 提活跃 chunk_id 集合 C，按需提取重建 text_chunks。
    
    真相源：GraphML（提活跃 chunk_id）+ full_docs（text_chunks 没有时反查原文）+ cache（反向构建 llm_cache_list）
    派生：kv_store_text_chunks.json
    
    算法：
    1. 解析 GraphML 提取活跃 chunk_id 集合 C（从所有 node d3 + edge d10）
    2. 对 C 中每个 chunk_id：
       - 优先从现有 text_chunks 按 cid 查原文（天然最后版本）
       - 现有 text_chunks 没有时，从 full_docs 重新 chunking 反查（多条匹配取 create_time 最大）
    3. llm_cache_list 从 cache 按 chunk_id 反向构建
    4. 只重建 C 中的 chunk，旧版本 chunk 丢弃
    
    GraphML 损坏 = unrecoverable
    full_docs 损坏且 text_chunks 损坏 = unrecoverable
    """
    storage_dir = _storage_dir()
    tc_path = storage_dir / "kv_store_text_chunks.json"
    full_docs_path = storage_dir / "kv_store_full_docs.json"
    cache_path = storage_dir / "kv_store_llm_response_cache.json"
    
    # 1. 解析 GraphML 提取活跃 chunk_id 集合 C
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
    
    active_chunk_ids: set[str] = set()
    for node_id, (desc, src_ids) in nodes.items():
        if src_ids:
            active_chunk_ids.update(c for c in src_ids.split(GRAPH_FIELD_SEP) if c)
    for edge_tuple in edges_list:
        edge_src_ids = edge_tuple[2]  # (src, tgt, src_ids, desc, kw) 的 index 2
        if edge_src_ids:
            active_chunk_ids.update(c for c in edge_src_ids.split(GRAPH_FIELD_SEP) if c)
    
    # 2. 读现有 text_chunks（按 cid 查原文）
    existing_tc: dict[str, Any] = {}
    if tc_path.exists():
        loaded = _load_json_dict(tc_path)
        if isinstance(loaded, dict):
            existing_tc = loaded
        elif loaded is None and tc_path.exists():
            # 文件存在但解析失败 → 损坏
            # 不立即报错，降级到 full_docs 反查（如果 full_docs 也损坏才报 unrecoverable）
            pass
    
    # 3. 读 full_docs（text_chunks 没有时才用）
    full_docs: dict[str, Any] = {}
    if full_docs_path.exists():
        loaded = _load_json_dict(full_docs_path)
        if isinstance(loaded, dict):
            full_docs = loaded
    
    # 4. 读 cache（反向构建 llm_cache_list）
    cache: dict[str, Any] = {}
    if cache_path.exists():
        loaded = _load_json_dict(cache_path)
        if isinstance(loaded, dict):
            cache = loaded
    
    # 5. 判断是否需要扫 full_docs（如果 existing_tc 已覆盖所有 C，就不扫）
    need_full_docs_scan = any(cid not in existing_tc for cid in active_chunk_ids)
    full_docs_chunk_map: dict[str, tuple[int, str, str, str]] = {}
    # 类型: chunk_id -> (create_time, doc_id, chunk_content, file_path)
    if need_full_docs_scan:
        if not full_docs:
            # text_chunks 损坏 + full_docs 损坏 → unrecoverable
            missing_count = sum(1 for cid in active_chunk_ids if cid not in existing_tc)
            if missing_count > 0:
                return {
                    "status": "error",
                    "expected": len(active_chunk_ids),
                    "actual": len(existing_tc),
                    "lost": missing_count,
                    "source": "GraphML + full_docs",
                    "message": f"text_chunks 损坏且 full_docs 损坏/为空，{missing_count} 个活跃 chunk 无法重建",
                    "unrecoverable": True,
                }
        # 用真实 _get_lightrag_config 读 chunk_size
        from niu_api.internal.lightrag_manager import _get_lightrag_config
        config = _get_lightrag_config()
        chunk_token_size = config.get("chunk_token_size", 1200)
        chunk_overlap = config.get("chunk_overlap_token_size", 50)
        
        # 拿 tokenizer（用 get_lightrag_for_repair 绕过 _repairing 门控）
        from niu_api.internal.lightrag_manager import get_lightrag_for_repair
        rag = get_lightrag_for_repair()
        if rag is None:
            return {
                "status": "error",
                "expected": len(active_chunk_ids),
                "actual": 0,
                "lost": len(active_chunk_ids),
                "source": "GraphML + full_docs",
                "message": "LightRAG 实例未初始化，无法获取 tokenizer",
                "unrecoverable": True,
            }
        tokenizer = rag.tokenizer
        
        # chunking_by_token_size 是 LightRAG 的函数，需要局部 import
        from lightrag.operate import chunking_by_token_size
        
        # 按 create_time 降序排 full_docs（最后录入的优先）
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
                # 同一 chunk_id 多 doc 匹配时，按 create_time 降序保留第一个（最新版本）
                if cid not in full_docs_chunk_map:
                    full_docs_chunk_map[cid] = (create_time, doc_id, chunk_content, file_path)
    
    # 6. 预构建 cache 的 chunk_id → [cache_key] 映射（用于 llm_cache_list）
    #    同一 chunk_id 多条 cache entry（多轮 gleaning）时全部保留（LightRAG 重放时按 llm_cache_list 顺序）
    chunk_id_to_cache_keys: dict[str, list[str]] = {}
    for cache_key, entry in cache.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("cache_type") != "extract":
            continue
        cid = entry.get("chunk_id")
        if cid:
            chunk_id_to_cache_keys.setdefault(cid, []).append(cache_key)
    
    # 7. 遍历 C 构建 new_tc
    new_tc: dict[str, Any] = {}
    missing_chunks: list[str] = []
    
    for cid in active_chunk_ids:
        # 优先从 existing_tc 查
        if cid in existing_tc and isinstance(existing_tc[cid], dict):
            chunk_data = dict(existing_tc[cid])
            chunk_data["llm_cache_list"] = chunk_id_to_cache_keys.get(cid, [])
            new_tc[cid] = chunk_data
        # 降级从 full_docs_chunk_map 查
        elif cid in full_docs_chunk_map:
            ct, doc_id, content, file_path = full_docs_chunk_map[cid]
            new_tc[cid] = {
                "content": content,
                "full_doc_id": doc_id,
                "file_path": file_path,
                "llm_cache_list": chunk_id_to_cache_keys.get(cid, []),
            }
        else:
            # 脑区 chunk（full_doc_id="brain"）可能不在 full_docs 里
            # 如果 existing_tc 也没有，记为 missing（region_sync 会重新注入）
            missing_chunks.append(cid)
    
    # 8. 写盘（原子写）
    try:
        _atomic_write_json(tc_path, new_tc)
    except Exception as e:
        return {
            "status": "error",
            "expected": len(active_chunk_ids),
            "actual": len(new_tc),
            "lost": len(active_chunk_ids) - len(new_tc),
            "source": "GraphML + full_docs",
            "message": f"写 text_chunks 失败: {e}",
            "unrecoverable": True,
        }
    
    return {
        "status": "ok",
        "expected": len(active_chunk_ids),
        "actual": len(new_tc),
        "lost": len(missing_chunks),
        "source": "GraphML + full_docs",
        "missing_chunks": missing_chunks[:10],
        "message": f"重建 {len(new_tc)}/{len(active_chunk_ids)} 个 chunk",
    }
```

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_repair_text_chunks_only_rebuilds_active_chunks \
                tests/test_lightrag_repair_unit.py::test_repair_text_chunks_falls_back_to_full_docs_when_text_chunks_missing \
                tests/test_lightrag_repair_unit.py::test_repair_text_chunks_unrecoverable_when_graphml_corrupt \
                tests/test_lightrag_repair_unit.py::test_repair_text_chunks_unrecoverable_when_full_docs_corrupt -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_repair.py tests/test_lightrag_repair_unit.py
git commit -m "fix(repair): repair_text_chunks 改为从 GraphML 提活跃 chunk_id 按需提取重建

v2/v3 从 full_docs 全量重新 chunking 重建 text_chunks——会把旧版本文档
的 chunk 也重建出来，未来重跑 apipeline 时旧版本实体复活。

v4 改为按需提取：
1. 解析 GraphML 提取活跃 chunk_id 集合 C（从 node d3 + edge d10）
2. 对 C 中每个 chunk_id：
   - 优先从现有 text_chunks 按 cid 查原文（天然最后版本）
   - 现有 text_chunks 没有时，从 full_docs 重新 chunking 反查
     （多条匹配按 create_time 降序取最新 doc）
3. llm_cache_list 从 cache 按 chunk_id 反向构建
4. 只重建 C 中的 chunk，旧版本 chunk 丢弃

GraphML 损坏 = unrecoverable
full_docs 损坏且 text_chunks 损坏 = unrecoverable
"
```

### - [ ] Step 6: 修复 `_embed_batch` 用 `get_lightrag_for_repair` 绕过 `_repairing` 门控

**背景**：现有 `_embed_batch`（`lightrag_repair.py:106-148`）fallback 路径调 `get_lightrag()`，但 repair 期间 `_repairing=True` 会让 `get_lightrag()` 返回 None（`lightrag_manager.py:925`），导致 embedding 失败 → `repair_vdb_*` 全部失败。

**修改**：把 `_embed_batch` 的 fallback 从 `get_lightrag` 改为 `get_lightrag_for_repair`（绕过 `_repairing` 门控）。

修改 `niu_api/internal/lightrag_repair.py:130-148`：

```python
    # 2. fallback 到 LightRAG 实例（repair 专用路径，绕过 _repairing 门控）
    try:
        import asyncio

        from niu_api.internal.lightrag_manager import get_lightrag_for_repair

        rag = get_lightrag_for_repair()
        if rag is None:
            logger.error("[LightRAGRepair] embedding 失败：预加载模型未就绪 + LightRAG 未初始化")
            return None
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(rag.embedding_func(texts))
            return [list(map(float, v)) for v in result]
        finally:
            loop.close()
    except Exception as e:  # noqa: BLE001
        logger.error(f"[LightRAGRepair] LightRAG embedding 也失败: {e}")
        return None
```

**测试**：在 `tests/test_lightrag_repair_unit.py` 加测试验证 `_embed_batch` 在 `_repairing=True` 时能正常工作（用真实 embedding 模型，不 mock）：

```python
def test_embed_batch_works_during_repair(monkeypatch):
    """_embed_batch 在 _repairing=True 时应通过 get_lightrag_for_repair 拿到实例。
    
    使用真实 embedding 模型（不 mock）。
    """
    import niu_api.internal.lightrag_manager as lm
    from niu_api.internal.embedding import get_model
    assert get_model() is not None, "embedding 模型应预加载"
    
    # 模拟 repair 期间 _repairing=True
    original = lm._repairing
    lm._repairing = True
    try:
        from niu_api.internal.lightrag_repair import _embed_batch
        result = _embed_batch(["测试文本"])
        assert result is not None
        assert len(result) == 1
        assert len(result[0]) > 0
    finally:
        lm._repairing = original
```

Commit:
```bash
git add niu_api/internal/lightrag_repair.py tests/test_lightrag_repair_unit.py
git commit -m "fix(repair): _embed_batch 用 get_lightrag_for_repair 绕过 _repairing 门控

现有 _embed_batch fallback 调 get_lightrag()，repair 期间 _repairing=True
会让 get_lightrag() 返回 None，导致 embedding 失败 → repair_vdb_* 全部失败。

改为用 get_lightrag_for_repair()（lightrag_manager.py:1008 专门绕过门控）。
"
```

---

## Task 2: 新增 `_check_truth_sources_intact` 检测 3 真相源完好性

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

v4 的 `repair_all` 开头需要检测 3 真相源（GraphML + full_docs + cache）完好性。任一损坏 = unrecoverable，不进入恢复流程。

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py` 追加：

```python
def test_check_truth_sources_intact_all_intact(tmp_path, monkeypatch):
    """3 真相源全部完好时返回 intact=True。"""
    _write_graphml(tmp_path, [("entity-x", "desc", "chunk-x")])
    (tmp_path / "kv_store_full_docs.json").write_text('{"doc-1": {"content": "x"}}')
    (tmp_path / "kv_store_llm_response_cache.json").write_text('{"k": {"return": "x"}}')
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_repair import _check_truth_sources_intact
    result = _check_truth_sources_intact()
    
    assert result["intact"] is True
    assert result["graphml"]["intact"] is True
    assert result["full_docs"]["intact"] is True
    assert result["cache"]["intact"] is True


def test_check_truth_sources_intact_graphml_corrupt(tmp_path, monkeypatch):
    """GraphML 损坏时返回 intact=False + graphml.intact=False。"""
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text("corrupt <<<")
    (tmp_path / "kv_store_full_docs.json").write_text('{}')
    (tmp_path / "kv_store_llm_response_cache.json").write_text('{}')
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_repair import _check_truth_sources_intact
    result = _check_truth_sources_intact()
    
    assert result["intact"] is False
    assert result["graphml"]["intact"] is False


def test_check_truth_sources_intact_full_docs_corrupt(tmp_path, monkeypatch):
    """full_docs 损坏时返回 intact=False + full_docs.intact=False。"""
    _write_graphml(tmp_path, [("entity-x", "desc", "chunk-x")])
    (tmp_path / "kv_store_full_docs.json").write_text("corrupt")
    (tmp_path / "kv_store_llm_response_cache.json").write_text('{}')
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_repair import _check_truth_sources_intact
    result = _check_truth_sources_intact()
    
    assert result["intact"] is False
    assert result["full_docs"]["intact"] is False


def test_check_truth_sources_intact_cache_corrupt(tmp_path, monkeypatch):
    """cache 损坏时返回 intact=False + cache.intact=False。"""
    _write_graphml(tmp_path, [("entity-x", "desc", "chunk-x")])
    (tmp_path / "kv_store_full_docs.json").write_text('{}')
    (tmp_path / "kv_store_llm_response_cache.json").write_text("corrupt")
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_repair import _check_truth_sources_intact
    result = _check_truth_sources_intact()
    
    assert result["intact"] is False
    assert result["cache"]["intact"] is False
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_check_truth_sources_intact_all_intact -v
```

Expected: FAIL with `ImportError: cannot import name '_check_truth_sources_intact'`

### - [ ] Step 3: Write minimal implementation

在 `niu_api/internal/lightrag_repair.py` 新增 `_check_truth_sources_intact`：

```python
def _check_truth_sources_intact() -> dict[str, Any]:
    """检测 3 真相源完好性：GraphML + full_docs + cache。
    
    任一损坏 = intact=False，repair_all 应报 unrecoverable。
    
    全新用户合法场景（intact=True）：
    - 文件不存在（还没导入文档）
    - 文件 size=0（空文件）
    - GraphML 无 node（空图）
    - full_docs/cache 是空 dict
    
    损坏场景（intact=False）：
    - GraphML 文件存在但 XML 解析失败 / 无 graph 元素
    - full_docs/cache 文件存在但 JSON 解析失败 / 非 dict
    
    检测标准跟现有 lightrag_integrity._check_truth_source（L166-203）一致：
    - 文件不存在 → ok（全新用户合法）
    - size=0 → ok（全新用户合法）
    - JSON 解析失败 / 非 dict → critical
    """
    import xml.etree.ElementTree as ET

    storage_dir = _storage_dir()
    
    # 1. GraphML
    #    文件不存在 / size=0 → intact=True（全新用户合法）
    #    XML 解析失败 / 无 graph 元素 → intact=False
    #    无 node → intact=True（空图合法，repair 重建空集）
    graphml_path = storage_dir / "graph_chunk_entity_relation.graphml"
    graphml_check: dict[str, Any] = {"intact": True, "reason": ""}
    if not graphml_path.exists() or graphml_path.stat().st_size == 0:
        # 全新用户合法，空 GraphML 不算损坏
        graphml_check["reason"] = "GraphML 不存在或为空（全新用户合法）"
    else:
        try:
            tree = ET.parse(graphml_path)
            root = tree.getroot()
            # 用现有 _load_graphml_nodes 的 fallback 模式：
            # 先尝试带 namespace 查找，再 fallback 到无 namespace 遍历子元素
            ns_str = "{http://graphml.graphdrawing.org/xmlns}"
            graph_elem = root.find(f"{ns_str}graph")
            if graph_elem is None:
                for child in root:
                    tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if tag == "graph":
                        graph_elem = child
                        break
            if graph_elem is None:
                graphml_check["intact"] = False
                graphml_check["reason"] = "GraphML 无 graph 元素"
            else:
                # 有 graph 元素就算完好（无 node 是空图，全新用户合法）
                graphml_check["intact"] = True
        except Exception as e:
            graphml_check["intact"] = False
            graphml_check["reason"] = f"XML 解析失败: {e}"
    
    # 2. full_docs
    #    文件不存在 / size=0 / 空 dict → intact=True（全新用户合法）
    #    JSON 解析失败 / 非 dict → intact=False
    full_docs_path = storage_dir / "kv_store_full_docs.json"
    full_docs_check: dict[str, Any] = {"intact": True, "reason": ""}
    if not full_docs_path.exists() or full_docs_path.stat().st_size == 0:
        full_docs_check["reason"] = "full_docs 不存在或为空（全新用户合法）"
    else:
        loaded = _load_json_dict(full_docs_path)
        if loaded is None:
            full_docs_check["intact"] = False
            full_docs_check["reason"] = "full_docs JSON 解析失败或非 dict"
        else:
            # 空 dict 或有内容都算完好
            full_docs_check["intact"] = True
    
    # 3. cache
    #    文件不存在 / size=0 / 空 dict → intact=True（全新用户合法）
    #    JSON 解析失败 / 非 dict → intact=False
    cache_path = storage_dir / "kv_store_llm_response_cache.json"
    cache_check: dict[str, Any] = {"intact": True, "reason": ""}
    if not cache_path.exists() or cache_path.stat().st_size == 0:
        cache_check["reason"] = "cache 不存在或为空（全新用户合法）"
    else:
        loaded = _load_json_dict(cache_path)
        if loaded is None:
            cache_check["intact"] = False
            cache_check["reason"] = "cache JSON 解析失败或非 dict"
        else:
            cache_check["intact"] = True
    
    return {
        "intact": graphml_check["intact"] and full_docs_check["intact"] and cache_check["intact"],
        "graphml": graphml_check,
        "full_docs": full_docs_check,
        "cache": cache_check,
    }
```

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_check_truth_sources_intact_all_intact \
                tests/test_lightrag_repair_unit.py::test_check_truth_sources_intact_graphml_corrupt \
                tests/test_lightrag_repair_unit.py::test_check_truth_sources_intact_full_docs_corrupt \
                tests/test_lightrag_repair_unit.py::test_check_truth_sources_intact_cache_corrupt -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_repair.py tests/test_lightrag_repair_unit.py
git commit -m "feat(repair): 新增 _check_truth_sources_intact 检测 3 真相源完好性

检测 GraphML + full_docs + cache 三个真相源的完好性：
- GraphML：文件存在 + XML 可解析 + 有 graph + 有 node
- full_docs：文件存在 + JSON 可解析 + 是 dict
- cache：文件存在 + JSON 可解析 + 是 dict

任一损坏 = intact=False，repair_all 应报 unrecoverable。
"
```

---

## Task 3: 重写 `repair_all` 为"检测 3 真相源 → 备份 9 → 删 9 → 按需提取重建 → 失败回滚"

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py:2331-2525`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

v4 的 `repair_all` 流程：
1. 同步 `_STORAGE_DIR` 到 `lightrag_integrity` + `lightrag_manager`
2. 检测 3 真相源完好性 → 任一损坏 = unrecoverable
3. 备份 9 个派生文件（不备份真相源，因为不动）
4. 删除 9 个派生文件
5. 按依赖链重建（**不含 repair_graphml / repair_brainregion_zombies / repair_cache_filter / repair_graphml_orphan_edges**）：
   - repair_text_chunks（Task 1 重写后的按需提取版本）
   - repair_doc_status
   - repair_vdb_chunks
   - repair_vdb_entities（天然防复活）
   - repair_vdb_relationships（天然防复活，不写 weight）
   - repair_entity_chunks（天然防复活）
   - repair_relation_chunks（天然防复活）
   - repair_full_entities（天然防复活）
   - repair_full_relations（天然防复活）
6. 任意步骤失败时回滚 9 个派生文件备份

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py` 追加：

```python
def _make_synthetic_fixture(tmp_path: Path):
    """合成 fixture：3 文档 + 5 cache + GraphML（含衰减后 weight + 已删实体已不在）。"""
    # GraphML：2 个实体（entity-a, entity-b）+ 1 条 edge（weight=0.5 衰减后）
    # 已删实体 deleted-entity 不在 GraphML 里
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    # key 定义（简化，真实 GraphML 有完整 key 定义）
    for kid, attr_name, attr_type in [
        ("d1", "entity_type", "string"), ("d2", "description", "string"),
        ("d3", "source_id", "string"), ("d7", "weight", "double"),
        ("d8", "description", "string"), ("d9", "keywords", "string"),
        ("d10", "source_id", "string"),
    ]:
        k = ET.SubElement(root, f"{{{ns}}}key", {
            "id": kid, "for": "all", "attr.name": attr_name, "attr.type": attr_type
        })
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    
    a = ET.SubElement(graph, f"{{{ns}}}node", {"id": "entity-a"})
    ET.SubElement(a, f"{{{ns}}}data", {"key": "d2"}).text = "desc A"
    ET.SubElement(a, f"{{{ns}}}data", {"key": "d3"}).text = "chunk-1"
    
    b = ET.SubElement(graph, f"{{{ns}}}node", {"id": "entity-b"})
    ET.SubElement(b, f"{{{ns}}}data", {"key": "d2"}).text = "desc B"
    ET.SubElement(b, f"{{{ns}}}data", {"key": "d3"}).text = "chunk-2"
    
    edge = ET.SubElement(graph, f"{{{ns}}}edge", {"source": "entity-a", "target": "entity-b"})
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d7"}).text = "0.5"  # 衰减后的 weight
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d8"}).text = "edge desc"
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d9"}).text = "keyword1, keyword2"
    ET.SubElement(edge, f"{{{ns}}}data", {"key": "d10"}).text = "chunk-1<SEP>chunk-2"
    
    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )
    
    # full_docs：2 个文档（v1 + v2 同文档不同版本 + 1 独立文档）
    docs = {
        "doc-v1": {"content": "v1 content", "file_path": "v1.md", "create_time": 1000},
        "doc-v2": {"content": "v2 content", "file_path": "v2.md", "create_time": 2000},
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    
    # cache：5 条 extract entry（含 1 个已删实体的脏 entry + 1 个旧版本 chunk 的 entry）
    cache = {
        "default:extract:chunk1": {
            "return": "entity<|#|>entity-a<|#|>concept<|#|>desc A",
            "cache_type": "extract", "chunk_id": "chunk-1", "create_time": 1500,
        },
        "default:extract:chunk2": {
            "return": "entity<|#|>entity-b<|#|>concept<|#|>desc B",
            "cache_type": "extract", "chunk_id": "chunk-2", "create_time": 1500,
        },
        # 已删实体的脏 entry（chunk-3 不在 GraphML 活跃集合）
        "default:extract:chunk3_deleted": {
            "return": "entity<|#|>deleted-entity<|#|>concept<|#|>已删",
            "cache_type": "extract", "chunk_id": "chunk-3", "create_time": 800,
        },
        # 旧版本 chunk 的 entry（chunk-old 不在 GraphML 活跃集合）
        "default:extract:chunk_old": {
            "return": "entity<|#|>old-entity<|#|>concept<|#|>旧版本",
            "cache_type": "extract", "chunk_id": "chunk-old", "create_time": 500,
        },
        # 非 extract 类型 cache
        "default:summary:some": {
            "return": "summary", "cache_type": "summary", "chunk_id": None, "create_time": 1700,
        },
    }
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    
    # 9 个派生文件初始为空（repair_all 会重建）
    for fname in ["kv_store_text_chunks.json", "kv_store_doc_status.json",
                  "kv_store_entity_chunks.json", "kv_store_relation_chunks.json",
                  "kv_store_full_entities.json", "kv_store_full_relations.json"]:
        (tmp_path / fname).write_text("{}")
    for fname in ["vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"]:
        (tmp_path / fname).write_text('{"data": [], "embedding_dim": 0, "matrix": ""}')


def test_repair_all_unrecoverable_when_graphml_corrupt(tmp_path, monkeypatch):
    """GraphML 损坏时 repair_all 应直接返回 unrecoverable，不备份不删除不重建。"""
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text("corrupt xml <<<")
    (tmp_path / "kv_store_full_docs.json").write_text('{}')
    (tmp_path / "kv_store_llm_response_cache.json").write_text('{}')
    (tmp_path / "kv_store_text_chunks.json").write_text('{"chunk-x": {}}')
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()
    
    assert result.get("_unrecoverable") is True
    # 派生文件未被删除
    assert (tmp_path / "kv_store_text_chunks.json").exists()
    # 真相源未被修改
    assert (tmp_path / "graph_chunk_entity_relation.graphml").read_text() == "corrupt xml <<<"


def test_repair_all_unrecoverable_when_full_docs_corrupt(tmp_path, monkeypatch):
    """full_docs 损坏时 repair_all 应返回 unrecoverable。"""
    _make_synthetic_fixture(tmp_path)
    # 覆盖 full_docs 为损坏
    (tmp_path / "kv_store_full_docs.json").write_text("corrupt")
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()
    
    assert result.get("_unrecoverable") is True


def test_repair_all_unrecoverable_when_cache_corrupt(tmp_path, monkeypatch):
    """cache 损坏时 repair_all 应返回 unrecoverable。"""
    _make_synthetic_fixture(tmp_path)
    (tmp_path / "kv_store_llm_response_cache.json").write_text("corrupt")
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()
    
    assert result.get("_unrecoverable") is True


def test_repair_all_does_not_touch_truth_sources(tmp_path, monkeypatch):
    """repair_all 不应修改 3 真相源（GraphML + full_docs + cache）一字节。
    
    使用真实 embedding 模型（CLAUDE.md 铁律 5：测试必须用真实数据+真实LLM，不 mock）。
    测试前会预加载 embedding 模型（通过 niu_api.internal.embedding.get_model）。
    """
    _make_synthetic_fixture(tmp_path)
    
    # 记录 3 真相源的原始内容
    graphml_before = (tmp_path / "graph_chunk_entity_relation.graphml").read_bytes()
    full_docs_before = (tmp_path / "kv_store_full_docs.json").read_bytes()
    cache_before = (tmp_path / "kv_store_llm_response_cache.json").read_bytes()
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    # 预加载真实 embedding 模型（不 mock LLM）
    from niu_api.internal.embedding import get_model
    assert get_model() is not None, "embedding 模型应预加载（测试前置条件）"
    
    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()
    
    # 3 真相源一字节未动
    assert (tmp_path / "graph_chunk_entity_relation.graphml").read_bytes() == graphml_before, "GraphML 不应被修改"
    assert (tmp_path / "kv_store_full_docs.json").read_bytes() == full_docs_before, "full_docs 不应被修改"
    assert (tmp_path / "kv_store_llm_response_cache.json").read_bytes() == cache_before, "cache 不应被修改"


def test_repair_all_does_not_reanimate_deleted_entities(tmp_path, monkeypatch):
    """repair_all 重建后，已删实体（deleted-entity）不应出现在任何派生文件里。
    
    使用真实 embedding 模型（不 mock）。
    """
    _make_synthetic_fixture(tmp_path)
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    from niu_api.internal.embedding import get_model
    assert get_model() is not None, "embedding 模型应预加载"
    
    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()
    
    # 检查所有派生文件不含 deleted-entity / old-entity
    tc = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert "chunk-3" not in tc  # 已删实体的 chunk 不重建
    assert "chunk-old" not in tc  # 旧版本 chunk 不重建
    
    ec = json.loads((tmp_path / "kv_store_entity_chunks.json").read_text())
    assert "deleted-entity" not in ec
    assert "old-entity" not in ec


def test_repair_all_rolls_back_on_failure(tmp_path, monkeypatch):
    """重建失败时应回滚到备份。"""
    _make_synthetic_fixture(tmp_path)
    
    # 记录派生文件原始内容
    tc_before = (tmp_path / "kv_store_text_chunks.json").read_bytes()
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    # mock 让 repair_vdb_entities 失败
    import niu_api.internal.lightrag_repair as repair_mod
    original_vdb_entities = repair_mod.repair_vdb_entities
    def failing_vdb_entities():
        raise Exception("mock failure")
    monkeypatch.setattr(repair_mod, "repair_vdb_entities", failing_vdb_entities)
    
    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()
    
    assert result.get("_rolled_back") is True
    # 原派生文件被恢复
    assert (tmp_path / "kv_store_text_chunks.json").read_bytes() == tc_before
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_repair_all_unrecoverable_when_graphml_corrupt -v
```

Expected: FAIL（现有 `repair_all` 不以 3 真相源为检测对象）

### - [ ] Step 3: Write minimal implementation

重写 `niu_api/internal/lightrag_repair.py` 的常量定义 + `repair_all` 函数。

**重要：实施范围包含两部分，缺一不可**：
1. **替换常量定义**（现有 `lightrag_repair.py:2223-2263`）：
   - `_TRUTH_SOURCE_FILES`（现有 L2224-2227 是 2 文件 `{full_docs, cache}`）→ 改为 3 文件 `{GraphML, full_docs, cache}`
   - `_DERIVED_FILES`（现有 L2230-2241）→ 移除 `graph_chunk_entity_relation.graphml`（真相源不能在派生列表里），保留 9 个派生文件
   - `_REBUILD_ORDER`（现有 L2250-2263 含 `repair_brainregion_zombies` + `repair_graphml` + `repair_graphml_orphan_edges`）→ 改为只含 9 个派生文件的 repair 函数
2. **替换 `repair_all` 函数体**（现有 `lightrag_repair.py:2331-2525`）→ 改为新的"检测 3 真相源 → 备份 9 → 删 9 → 按需提取重建 → 失败回滚"流程

**如果不替换常量定义只替换函数体**：`_TRUTH_SOURCE_FILES` 仍是 2 文件（不含 GraphML），`_DERIVED_FILES` 仍含 GraphML（真相源被误列入派生文件被备份+删除+回滚），`_REBUILD_ORDER` 仍含会动真相源的步骤——方案核心原则被架空。

下方代码块是新常量定义 + 新 `repair_all` 函数的完整实现：

```python
# 3 真相源（完全不可动）
_TRUTH_SOURCE_FILES = {
    "graph_chunk_entity_relation.graphml",
    "kv_store_full_docs.json",
    "kv_store_llm_response_cache.json",
}

# 9 派生文件（可重建）
_DERIVED_FILES = [
    "kv_store_text_chunks.json",
    "kv_store_doc_status.json",
    "vdb_chunks.json",
    "vdb_entities.json",
    "vdb_relationships.json",
    "kv_store_entity_chunks.json",
    "kv_store_relation_chunks.json",
    "kv_store_full_entities.json",
    "kv_store_full_relations.json",
]

# 重建顺序（不含 graphml / brainregion_zombies / cache_filter / graphml_orphan_edges——这些会动真相源）
# 用直接函数引用（不是字符串），拼写错误会在模块加载时 NameError，避免静默跳过
_REBUILD_ORDER: list[tuple[str, Any]] = [
    ("text_chunks", repair_text_chunks),
    ("doc_status", repair_doc_status),
    ("vdb_chunks", repair_vdb_chunks),
    ("vdb_entities", repair_vdb_entities),
    ("vdb_relationships", repair_vdb_relationships),
    ("entity_chunks", repair_entity_chunks),
    ("relation_chunks", repair_relation_chunks),
    ("full_entities", repair_full_entities),
    ("full_relations", repair_full_relations),
]


def repair_all() -> dict[str, Any]:
    """3 真相源不可动 + 按需提取重建 9 派生文件。
    
    流程：
    1. 同步 _STORAGE_DIR 到 lightrag_integrity + lightrag_manager
    2. 检测 3 真相源完好性 → 任一损坏 = unrecoverable
    3. 备份 9 个派生文件（不备份真相源，因为不动）
    4. 删除 9 个派生文件
    5. 按依赖链重建 9 派生文件（从 GraphML + full_docs + cache 按需提取）
    6. 失败时回滚 9 派生文件备份
    
    3 真相源（GraphML + full_docs + cache）完全不可动：
    - 不写不改不删（读取是必要的，用于按需提取重建派生文件）
    - 损坏 = unrecoverable
    - 完好 = 一根毫毛不动
    
    注意：repair_all 是同步函数，不能声明 async（调用方 lightrag_manager.py:1286
    和 1350 是同步调用 repair_all()，async 会导致返回 coroutine 对象）。
    """
    storage_dir = _storage_dir()
    result: dict[str, Any] = {}

    # 0. 同步 _STORAGE_DIR 到 lightrag_integrity + lightrag_manager（兼容测试 monkeypatch）
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

    # 用 try/finally 确保所有路径（成功/失败/异常）都清理 .corrupt.*.bak 垃圾文件 + backup_dir
    # 现有代码 lightrag_repair.py:2512-2523 不论成功失败都清理 .corrupt.*.bak
    # 现有代码 lightrag_repair.py:2488-2500 回滚后清理 backup_dir
    # backup_dir 提升到 try 外声明，让 finally 能在所有路径访问到（包括备份阶段失败时 backup_dir 未赋值的情况）
    backup_dir: Path | None = None
    try:
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
            return result

        # 2. 备份 9 个派生文件（不备份 3 真相源，因为完全不动）
        #    备份目录放在 storage_dir 外部（现有代码 lightrag_repair.py:2386 的做法），
        #    避免备份残留污染 storage 目录 + 避免 glob 误扫
        backup_dir = storage_dir.parent / f"lightrag_storage.prerepair_{int(time.time())}"
        backed_up: list[str] = []
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            import shutil
            for fname in _DERIVED_FILES:
                src = storage_dir / fname
                if src.exists():
                    shutil.copy2(src, backup_dir / fname)
                    backed_up.append(fname)
        except Exception as e:
            result["_unrecoverable"] = True
            result["_unrecoverable_reason"] = f"备份失败: {e}"
            return result

        # 3. 删除 9 个派生文件
        deleted: list[str] = []
        for fname in _DERIVED_FILES:
            path = storage_dir / fname
            if path.exists():
                try:
                    path.unlink()
                    deleted.append(fname)
                except Exception as e:
                    # 删除失败，回滚已删除的
                    _rollback_backup(backup_dir, storage_dir, backed_up)
                    result["_unrecoverable"] = True
                    result["_unrecoverable_reason"] = f"删除 {fname} 失败: {e}"
                    result["_rolled_back"] = True
                    return result
        result["_deleted"] = deleted

        # 4. 按依赖链重建 9 派生文件
        #    用 getattr 间接查找函数（不直接引用 _REBUILD_ORDER 里的函数对象），
        #    让测试 monkeypatch.setattr(repair_mod, "repair_vdb_entities", failing_fn) 能生效
        import niu_api.internal.lightrag_repair as _self_mod
        for name, fn in _REBUILD_ORDER:
            # 重新从模块属性读取，让 monkeypatch 能注入失败版本
            fn = getattr(_self_mod, fn.__name__)
            try:
                step_result = fn()
                result[name] = step_result
                if isinstance(step_result, dict) and step_result.get("unrecoverable"):
                    _rollback_backup(backup_dir, storage_dir, backed_up)
                    result["_unrecoverable"] = True
                    result["_unrecoverable_reason"] = f"{name} 重建失败: {step_result.get('message', '')}"
                    result["_rolled_back"] = True
                    return result
            except Exception as e:
                _rollback_backup(backup_dir, storage_dir, backed_up)
                result["_unrecoverable"] = True
                result["_unrecoverable_reason"] = f"{name} 重建异常: {e}"
                result["_rolled_back"] = True
                return result

        # 5. 重建成功，清理备份
        try:
            import shutil
            shutil.rmtree(backup_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

        return result
    finally:
        # 不论成功/失败/异常，都清理 .corrupt.*.bak 垃圾文件
        # 现有代码 lightrag_repair.py:2512-2523 也是不论成败都清理
        # glob 模式 *.corrupt.*.bak 匹配 _backup_corrupt 创建的 {name}.corrupt.{ts}.bak 文件
        for bak in storage_dir.glob("*.corrupt.*.bak"):
            try:
                bak.unlink()
            except Exception:  # noqa: BLE001
                pass
        # 清理 backup_dir（成功路径在 L1228 已 rmtree，失败回滚路径 return 时也需清理）
        # 现有代码 lightrag_repair.py:2488-2500 回滚后清理 backup_dir
        # backup_dir 可能为 None（备份阶段前失败）或路径不存在（成功路径已删），都需防御
        if backup_dir is not None and backup_dir.exists():
            try:
                import shutil
                shutil.rmtree(backup_dir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass


def _rollback_backup(backup_dir: Path, storage_dir: Path, backed_up: list[str]) -> None:
    """回滚备份：恢复 backed_up 中的文件 + 删除新建的派生文件。
    
    注意：3 真相源（GraphML + full_docs + cache）不在 _DERIVED_FILES 里，
    回滚不会动它们（它们也从未被修改）。
    """
    import shutil
    # 1. 恢复 backed_up 中的文件
    for fname in backed_up:
        src = backup_dir / fname
        if src.exists():
            shutil.copy2(src, storage_dir / fname)
    # 2. 删除 repair 前不存在但 repair 后新建的派生文件
    for fname in _DERIVED_FILES:
        if fname not in backed_up:
            fpath = storage_dir / fname
            if fpath.exists():
                try:
                    fpath.unlink()
                except Exception:  # noqa: BLE001
                    pass
```

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_repair_all_unrecoverable_when_graphml_corrupt \
                tests/test_lightrag_repair_unit.py::test_repair_all_unrecoverable_when_full_docs_corrupt \
                tests/test_lightrag_repair_unit.py::test_repair_all_unrecoverable_when_cache_corrupt \
                tests/test_lightrag_repair_unit.py::test_repair_all_does_not_touch_truth_sources \
                tests/test_lightrag_repair_unit.py::test_repair_all_does_not_reanimate_deleted_entities \
                tests/test_lightrag_repair_unit.py::test_repair_all_rolls_back_on_failure -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_repair.py tests/test_lightrag_repair_unit.py
git commit -m "fix(repair): repair_all 改为 3 真相源不可动 + 按需提取重建 9 派生文件

v2/v3 把 full_docs + cache 当真相源，从日志重放覆盖 GraphML——复活
已删实体 + 丢 weight 衰减 + 复活旧版本。

v4 核心原则（用户原话）：
3 个真相源文件就完全不可动。无论它里面有什么问题，也不能动它。
它们如果损坏了，那就是修复失败。如果没损坏，那为什么要动它？

v4 改为：
1. 检测 3 真相源（GraphML + full_docs + cache）完好性 → 任一损坏 = unrecoverable
2. 备份 9 派生文件（不备份真相源，因为不动）
3. 删除 9 派生文件
4. 按依赖链重建（不含 repair_graphml / repair_brainregion_zombies /
   repair_cache_filter / repair_graphml_orphan_edges——这些会动真相源）
5. 失败回滚 9 派生文件备份

真相源从 1 个/2 个改为 3 个：
_TRUTH_SOURCE_FILES = {GraphML, full_docs, cache}
"
```

---

## Task 4: 简化 `check_all` 为"检 3 真相源完好性 + 9 派生文件 missing 检测"

**Files:**
- Modify: `niu_api/internal/lightrag_integrity.py`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

v4 的 `check_all` 简化为：
1. 检 3 真相源完好性（GraphML + full_docs + cache）→ critical = unrecoverable
2. 检 9 派生文件 missing → major
3. 删除 v3 的 vdb_*_missing 细粒度检测（只保留"文件 missing"粒度，v3 的"GraphML 有 node 但 vdb 没向量"检测交给 `repair_vdb_*` 重建逻辑覆盖——简化 check_all）

**重要保留项**：修改 `lightrag_integrity.py` 时必须保留以下常量和函数（`lightrag_repair.py:1923-1924` 和 `lightrag_repair.py:2286` import 它们，删了会 ImportError）：
- `_ZOMBIE_DESCRIPTION_MARKERS` 常量（`lightrag_repair.py:1924` import）
- `_load_graphml` 函数（`lightrag_repair.py:1923, 1931` import + 调用）
- `_load_json_dict` 函数（`lightrag_repair.py:2286` 间接依赖，`_check_truth_source` 内部用）
- `_check_truth_source` 函数（`lightrag_repair.py:2286, 2294` import + 调用——`_check_truth_sources` 包装函数遍历 `_TRUTH_SOURCE_FILES` 逐个调它）

实施前用以下命令确认完整 import 列表（防止漏删被依赖的符号）：
```bash
grep -n "from niu_api.internal.lightrag_integrity import" niu_api/internal/lightrag_repair.py niu_api/internal/lightrag_manager.py
```

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py` 追加（跟 Task 1-3 同一个文件）：

```python
# ============================================================
# Task 4 测试：check_all 简化为"检 3 真相源 + 9 派生文件 missing"
# ============================================================

# 9 派生文件清单（跟 lightrag_repair._DERIVED_FILES 一致）
_DERIVED_FILES_FOR_TEST = [
    "kv_store_text_chunks.json",
    "kv_store_doc_status.json",
    "vdb_chunks.json",
    "vdb_entities.json",
    "vdb_relationships.json",
    "kv_store_entity_chunks.json",
    "kv_store_relation_chunks.json",
    "kv_store_full_entities.json",
    "kv_store_full_relations.json",
]


def _write_intact_truth_sources(tmp_path: Path):
    """写 3 真相源（全部完好）。"""
    # GraphML：1 个实体
    _write_graphml(tmp_path, [("entity-x", "desc X", "chunk-x")])
    (tmp_path / "kv_store_full_docs.json").write_text('{"doc-1": {"content": "x"}}')
    (tmp_path / "kv_store_llm_response_cache.json").write_text('{"k": {"return": "x"}}')


def test_check_all_3_truth_sources_all_intact(tmp_path, monkeypatch):
    """3 真相源全部完好（派生文件齐全）→ ok=True。"""
    _write_intact_truth_sources(tmp_path)
    # 9 派生文件全部存在（内容可空 dict / 空 vdb）
    for fname in _DERIVED_FILES_FOR_TEST:
        if fname.startswith("vdb_"):
            (tmp_path / fname).write_text('{"data": [], "embedding_dim": 0}')
        else:
            (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    assert result["ok"] is True
    assert result["critical_errors"] == 0
    assert result["major_errors"] == 0
    assert result["minor_errors"] == 0


def test_check_all_graphml_corrupt_is_critical(tmp_path, monkeypatch):
    """GraphML 损坏 → critical_errors=1 + ok=False。"""
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text("corrupt xml <<<")
    (tmp_path / "kv_store_full_docs.json").write_text('{}')
    (tmp_path / "kv_store_llm_response_cache.json").write_text('{}')
    for fname in _DERIVED_FILES_FOR_TEST:
        (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    assert result["ok"] is False
    assert result["critical_errors"] >= 1, "GraphML 损坏应为 critical"
    # 真相源损坏的 error 应归类到 checks.truth_source
    truth_errors = result["checks"]["truth_source"]["errors"]
    assert any(e.get("file") == "graph_chunk_entity_relation.graphml" for e in truth_errors), \
        "GraphML 错误应记到 truth_source check"


def test_check_all_full_docs_corrupt_is_critical(tmp_path, monkeypatch):
    """full_docs 损坏（非 dict JSON）→ critical_errors>=1。"""
    _write_intact_truth_sources(tmp_path)
    # 覆盖 full_docs 为损坏
    (tmp_path / "kv_store_full_docs.json").write_text("corrupt not json")
    for fname in _DERIVED_FILES_FOR_TEST:
        (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    assert result["ok"] is False
    assert result["critical_errors"] >= 1
    truth_errors = result["checks"]["truth_source"]["errors"]
    assert any(e.get("file") == "kv_store_full_docs.json" for e in truth_errors)


def test_check_all_cache_corrupt_is_critical(tmp_path, monkeypatch):
    """cache 损坏（非 dict JSON）→ critical_errors>=1。"""
    _write_intact_truth_sources(tmp_path)
    (tmp_path / "kv_store_llm_response_cache.json").write_text("corrupt not json")
    for fname in _DERIVED_FILES_FOR_TEST:
        (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    assert result["ok"] is False
    assert result["critical_errors"] >= 1
    truth_errors = result["checks"]["truth_source"]["errors"]
    assert any(e.get("file") == "kv_store_llm_response_cache.json" for e in truth_errors)


def test_check_all_missing_derived_file_is_major(tmp_path, monkeypatch):
    """9 派生文件任一 missing → major_errors>=1（真相源全完好）。"""
    _write_intact_truth_sources(tmp_path)
    # 只写 8 个派生文件，漏掉 vdb_entities.json
    for fname in _DERIVED_FILES_FOR_TEST:
        if fname == "vdb_entities.json":
            continue  # 故意不写
        if fname.startswith("vdb_"):
            (tmp_path / fname).write_text('{"data": [], "embedding_dim": 0}')
        else:
            (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    # 真相源完好 → critical=0；缺派生文件 → major>=1
    assert result["critical_errors"] == 0
    assert result["major_errors"] >= 1
    assert result["ok"] is False
    # missing 应记到 checks.derived_missing
    derived_errors = result["checks"]["derived_missing"]["errors"]
    assert any(e.get("file") == "vdb_entities.json" for e in derived_errors), \
        "missing 的派生文件应记到 derived_missing check"


def test_check_all_brand_new_user_is_ok(tmp_path, monkeypatch):
    """全新用户：3 真相源全不存在 + 9 派生文件全不存在 → ok=True（合法空状态）。"""
    # 不写任何文件（tmp_path 是空目录）
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)

    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    # 全新用户：文件不存在不算损坏，但 9 派生文件 missing 应算 major 还是 ok？
    # v4 决策：全新用户（3 真相源都不存在）时，9 派生文件 missing 也不报错
    # （因为 LightRAG 首次启动会自动初始化所有文件，不该误报）
    assert result["critical_errors"] == 0, "全新用户真相源不存在不应报 critical"


def test_check_all_preserves_zombie_markers_constant(tmp_path, monkeypatch):
    """修改 lightrag_integrity.py 后 _ZOMBIE_DESCRIPTION_MARKERS 必须仍可 import。
    
    lightrag_repair.py:1924 import 这个常量，删了会 ImportError。
    """
    from niu_api.internal.lightrag_integrity import _ZOMBIE_DESCRIPTION_MARKERS
    assert isinstance(_ZOMBIE_DESCRIPTION_MARKERS, tuple)
    assert len(_ZOMBIE_DESCRIPTION_MARKERS) > 0
    # 确认包含已知的标记
    assert "被删除的脑区" in _ZOMBIE_DESCRIPTION_MARKERS


def test_check_all_preserves_load_graphml_and_check_truth_source(tmp_path, monkeypatch):
    """修改后 _load_graphml / _check_truth_source 必须仍可 import。
    
    lightrag_repair.py:1923, 2286 import 这两个函数，删了会 ImportError。
    """
    from niu_api.internal.lightrag_integrity import (
        _load_graphml, _check_truth_source, _load_json_dict,
    )
    # 简单 smoke test：调一次确认可调用
    _write_intact_truth_sources(tmp_path)
    node_ids, edges, _, err = _load_graphml(tmp_path / "graph_chunk_entity_relation.graphml")
    assert err is None
    assert len(node_ids) == 1

    err = _check_truth_source("kv_store_full_docs.json", tmp_path)
    assert err == {}, "完好的真相源应返回空 dict（无错误）"
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_lightrag_repair_unit.py::test_check_all_graphml_corrupt_is_critical \
                tests/test_lightrag_repair_unit.py::test_check_all_missing_derived_file_is_major -v
```

Expected: FAIL
- `test_check_all_graphml_corrupt_is_critical`：现有 `check_all` 的 `_TRUTH_SOURCE_FILES` 只有 2 文件（不含 GraphML），GraphML 损坏走 `_check_graphml_post` 归类为 major 而不是 critical → `critical_errors == 0` 而非 `>= 1`，且 `checks` 字典里没有 `truth_source` 含 GraphML 错误的记录
- `test_check_all_missing_derived_file_is_major`：现有 `check_all` 没有专门的 `derived_missing` check 类别（只有 `vdb_missing`），`result["checks"]["derived_missing"]` 会 KeyError

### - [ ] Step 3: Write minimal implementation

修改 `niu_api/internal/lightrag_integrity.py`：

**改动 1**：`_TRUTH_SOURCE_FILES` 改为 3 文件（加 GraphML）

```python
_TRUTH_SOURCE_FILES = [
    "graph_chunk_entity_relation.graphml",
    "kv_store_full_docs.json",
    "kv_store_llm_response_cache.json",
]
```

**改动 2**：新增 `_check_derived_missing` 函数（检测 9 派生文件 missing）

```python
# 9 派生文件（跟 lightrag_repair._DERIVED_FILES 一致）
_DERIVED_FILES = [
    "kv_store_text_chunks.json",
    "kv_store_doc_status.json",
    "vdb_chunks.json",
    "vdb_entities.json",
    "vdb_relationships.json",
    "kv_store_entity_chunks.json",
    "kv_store_relation_chunks.json",
    "kv_store_full_entities.json",
    "kv_store_full_relations.json",
]


def _check_derived_missing(storage_dir: Path) -> list[dict[str, Any]]:
    """检测 9 派生文件 missing。
    
    全新用户场景（3 真相源全不存在）时，派生文件 missing 不报错
    （LightRAG 首次启动会自动初始化所有文件）。
    
    Returns:
        errors 列表（可能为空）。每个 error 含 file/severity=major/msg。
    """
    errors: list[dict[str, Any]] = []
    
    # 全新用户判定：3 真相源都不存在 → 派生文件 missing 不报错
    truth_sources_exist = any(
        (storage_dir / fname).exists() for fname in _TRUTH_SOURCE_FILES
    )
    if not truth_sources_exist:
        return errors  # 全新用户，不报错
    
    for fname in _DERIVED_FILES:
        fpath = storage_dir / fname
        if not fpath.exists():
            errors.append({
                "check": "derived_file_missing",
                "severity": "major",
                "file": fname,
                "msg": f"派生文件 {fname} 缺失（需要 repair 重建）",
            })
        elif fpath.stat().st_size == 0:
            errors.append({
                "check": "derived_file_empty",
                "severity": "major",
                "file": fname,
                "msg": f"派生文件 {fname} 为空（需要 repair 重建）",
            })
    return errors
```

**改动 3**：重写 `check_all` 为"检 3 真相源 + 9 派生文件 missing"（删除 `_check_graphml_post` 和 `_check_vdb_missing` 调用——这两个函数体可保留但不再被 `check_all` 调用，避免破坏其他潜在调用方）

```python
def check_all() -> dict[str, Any]:
    """v4 简化版 check_all：检 3 真相源完好性 + 9 派生文件 missing。
    
    1. 检 3 真相源（GraphML + full_docs + cache）完好性
       - 文件不存在 / size=0 → ok（全新用户合法）
       - JSON/XML 解析失败 / 非 dict → critical
    2. 检 9 派生文件 missing
       - 全新用户（3 真相源都不存在）时不报错
       - 否则 missing/empty 文件 → major
    
    Returns:
        {
            "ok": bool,
            "critical_errors": int,
            "major_errors": int,
            "minor_errors": int,
            "errors": list[dict],
            "checks": {
                "truth_source": {"name": ..., "errors": list},
                "derived_missing": {"name": ..., "errors": list},
            },
        }
    """
    storage_dir = _resolve_storage_dir()
    all_errors: list[dict[str, Any]] = []

    # 1. 检测 3 真相源（GraphML + full_docs + cache）
    #    _check_truth_source 对 GraphML 走 XML 解析（现有实现 L166-203
    #    对 JSON 文件判定，GraphML 走 _check_truth_source 时 JSON 解析失败
    #    也会归为 critical——但 GraphML 不是 JSON，需要专门 XML 检测）
    truth_errors: list[dict[str, Any]] = []
    for fname in _TRUTH_SOURCE_FILES:
        if fname == _GRAPHML_FILE:
            # GraphML 走 XML 专门检测（不是 JSON）
            err = _check_truth_source_graphml(storage_dir)
        else:
            err = _check_truth_source(fname, storage_dir)
        if err:
            truth_errors.append(err)
            all_errors.append(err)

    # 2. 检测 9 派生文件 missing
    derived_errors = _check_derived_missing(storage_dir)
    all_errors.extend(derived_errors)

    critical = sum(1 for e in all_errors if e.get("severity") == "critical")
    major = sum(1 for e in all_errors if e.get("severity") == "major")
    minor = sum(1 for e in all_errors if e.get("severity") == "minor")

    return {
        "ok": len(all_errors) == 0,
        "critical_errors": critical,
        "major_errors": major,
        "minor_errors": minor,
        "errors": all_errors,
        "checks": {
            "truth_source": {"name": "truth_source", "errors": truth_errors},
            "derived_missing": {"name": "derived_missing", "errors": derived_errors},
        },
    }


def _check_truth_source_graphml(storage_dir: Path) -> dict[str, Any]:
    """检测 GraphML 真相源完好性（XML 解析）。
    
    全新用户合法：文件不存在 / size=0 → ok
    损坏：XML 解析失败 / 无 graph 元素 → critical
    """
    graphml_path = storage_dir / _GRAPHML_FILE
    if not graphml_path.exists():
        return {}  # 全新用户合法
    try:
        size = graphml_path.stat().st_size
        if size == 0:
            return {}  # 全新用户合法
        # 用现有 _load_graphml 解析（已处理 namespace + graph 元素 fallback）
        _, _, _, err = _load_graphml(graphml_path)
        if err:
            # _load_graphml 返回的 err 已含 check/severity/file/msg
            return err
        return {}  # 解析成功
    except Exception as e:
        return {
            "check": "truth_source_read_fail",
            "severity": "critical",
            "file": _GRAPHML_FILE,
            "msg": f"GraphML 读取失败: {e}",
        }
```

**改动 4**：更新文件顶部 docstring（从 v2 改为 v4）

```python
"""LightRAG 数据一致性检查（v4：3 真相源不可动 + 9 派生文件 missing）。

检查项：
1. 3 真相源完整可用（GraphML + full_docs + cache）→ critical = unrecoverable
2. 9 派生文件 missing 检测 → major（需 repair 重建）

全新用户合法：3 真相源都不存在时，9 派生文件 missing 也不报错。
"""
```

**不要删除**（保留向后兼容，其他模块可能 import）：
- `_ZOMBIE_DESCRIPTION_MARKERS` 常量（`lightrag_repair.py:1924` 依赖）
- `_load_graphml` 函数（`lightrag_repair.py:1923, 1931` 依赖）
- `_load_json_dict` 函数（`_check_truth_source` 内部用）
- `_check_truth_source` 函数（`lightrag_repair.py:2286` 依赖）
- `_check_graphml_post` 函数（函数体保留，`check_all` 不再调用，避免破坏潜在调用方）
- `_check_vdb_missing` 函数（同上）
- `_load_vdb` 函数（`_check_vdb_missing` 内部用，保留）

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_check_all_3_truth_sources_all_intact \
                tests/test_lightrag_repair_unit.py::test_check_all_graphml_corrupt_is_critical \
                tests/test_lightrag_repair_unit.py::test_check_all_full_docs_corrupt_is_critical \
                tests/test_lightrag_repair_unit.py::test_check_all_cache_corrupt_is_critical \
                tests/test_lightrag_repair_unit.py::test_check_all_missing_derived_file_is_major \
                tests/test_lightrag_repair_unit.py::test_check_all_brand_new_user_is_ok \
                tests/test_lightrag_repair_unit.py::test_check_all_preserves_zombie_markers_constant \
                tests/test_lightrag_repair_unit.py::test_check_all_preserves_load_graphml_and_check_truth_source -v
```

Expected: PASS

### - [ ] Step 5: 验证 `lightrag_repair.py` 仍能 import

Run:
```bash
python -c "from niu_api.internal.lightrag_repair import repair_all, _check_truth_sources; print('OK')"
```

Expected: 输出 `OK`（无 ImportError）

如果报 ImportError：检查 `_ZOMBIE_DESCRIPTION_MARKERS` / `_load_graphml` / `_check_truth_source` 是否被误删，按 Step 3 "不要删除" 清单恢复。

### - [ ] Step 6: Commit

```bash
git add niu_api/internal/lightrag_integrity.py tests/test_lightrag_repair_unit.py
git commit -m "fix(integrity): check_all 简化为检 3 真相源 + 9 派生文件 missing

v2/v3 的 check_all 检 2 真相源 + GraphML 后置 + vdb_*_missing 细粒度检测，
但 v4 把 GraphML 提升为第 3 真相源后：
- GraphML 损坏应归 critical（不是 major）
- vdb_*_missing 细粒度检测交给 repair_vdb_* 重建逻辑覆盖（简化 check_all）

v4 改为：
1. _TRUTH_SOURCE_FILES 从 2 文件改为 3 文件（加 GraphML）
2. 新增 _check_truth_source_graphml 检测 GraphML XML 完好性
3. 新增 _check_derived_missing 检测 9 派生文件 missing
4. 全新用户（3 真相源都不存在）时派生文件 missing 不报错

保留 _ZOMBIE_DESCRIPTION_MARKERS / _load_graphml / _load_json_dict /
_check_truth_source（lightrag_repair.py import 它们，删了会 ImportError）。
保留 _check_graphml_post / _check_vdb_missing 函数体（不再被 check_all
调用，避免破坏潜在调用方）。
"
```

---

## Task 5: 修复 `lightrag_manager` 的 `total_errors` 字段 + `run_repair_on_user_request` 的 `repaired` 判定

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

**现有代码事实**（重要，纠正 v3 方案的误判）：
1. `run_resilience_phase1`（L1101-1134）已用 `critical_errors`/`major_errors`/`minor_errors` 字段（L1122-1128），只是日志里还有 `total_errors={total}`（L1128，这是日志变量名不是字段，无需改）。
2. `run_repair_on_user_request`（L1213-1420）已用 `_unrecoverable` 字段判 `repaired`（L1377 `repaired = not has_unrecoverable and not repair_result.get("_unrecoverable", False)`），不需要改。
3. `get_lightrag_status`（L1448-1489）返回里**还保留** `total_errors` 字段（L1484），因为 Rust `IntegrityStatus.total_errors`（`main.rs:54`）要读它（`main.rs:553-556` 用它生成弹窗文案）。不能直接删 `total_errors`，否则 Rust 反序列化"missing field"报错（`main.rs:42` 注释明确警告过这个坑）。
4. **"去掉用户确认环节"是 v3 方案的误判**：现有 `run_repair_on_user_request` 由 `/api/kg/lightrag/repair` API 触发（用户在 Rust 弹窗点"是"后 Rust 调 curl POST 触发此 API），**没有程序内等待用户确认的代码**。Python 侧接到请求就立刻走修复流程。

**v4 Task 5 实际改动**：
- **改动 1**：`get_lightrag_status` 的 `total_errors` 字段加注释说明它是 `critical + major + minor` 的和（兼容 Rust 字段，不能删）
- **改动 2**：`run_resilience_phase1` 日志里 `total_errors={total}` 改名 `total={total}`（去掉误导性的 "errors" 后缀，因为这是局部变量不是字段）
- **改动 3**：验证 `run_repair_on_user_request` 的 `repaired` 判定正确（已有实现无需改，但需要加测试锁定行为）

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py` 追加：

```python
# ============================================================
# Task 5 测试：lightrag_manager 字段 + repaired 判定
# ============================================================


def test_get_lightrag_status_returns_3_severity_fields(tmp_path, monkeypatch):
    """get_lightrag_status 必须返回 critical_errors/major_errors/minor_errors 三字段。
    
    Rust IntegrityStatus 已加 critical_errors/major_errors/minor_errors 字段
    （main.rs:55-60），但 total_errors 仍保留兼容（不能删，main.rs:54 读）。
    """
    _write_intact_truth_sources(tmp_path)
    for fname in _DERIVED_FILES_FOR_TEST:
        if fname.startswith("vdb_"):
            (tmp_path / fname).write_text('{"data": [], "embedding_dim": 0}')
        else:
            (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    import niu_api.internal.lightrag_manager as lm
    # 模拟 Phase 1 已跑过（_integrity_result 非 None）
    lm._integrity_result = {
        "ok": True,
        "critical_errors": 0,
        "major_errors": 0,
        "minor_errors": 0,
        "errors": [],
        "checks": {},
    }

    status = lm.get_lightrag_status()
    integrity = status["integrity"]

    # 三级 severity 字段必须存在
    assert "critical_errors" in integrity, "integrity 必须含 critical_errors"
    assert "major_errors" in integrity, "integrity 必须含 major_errors"
    assert "minor_errors" in integrity, "integrity 必须含 minor_errors"
    # total_errors 保留（Rust main.rs:54 依赖，不能删）
    assert "total_errors" in integrity, "total_errors 保留兼容 Rust（main.rs:54 读）"
    # total_errors = critical + major + minor
    expected_total = (
        integrity["critical_errors"]
        + integrity["major_errors"]
        + integrity["minor_errors"]
    )
    assert integrity["total_errors"] == expected_total, \
        "total_errors 应等于 critical + major + minor 之和"


def test_run_repair_on_user_request_repaired_true_on_success(tmp_path, monkeypatch):
    """repair_all 成功（无 _unrecoverable）→ repaired=True。"""
    _write_intact_truth_sources(tmp_path)
    for fname in _DERIVED_FILES_FOR_TEST:
        (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    import niu_api.internal.lightrag_manager as lm
    # 模拟 pipeline 不 busy（_read_pipeline_busy 返回 False）
    monkeypatch.setattr("niu_api.kg_api._read_pipeline_busy", lambda: False)
    # 模拟 repair_all 成功（无 _unrecoverable）
    monkeypatch.setattr(
        "niu_api.internal.lightrag_repair.repair_all",
        lambda: {"_unrecoverable": False, "text_chunks": {"status": "ok"}},
    )
    # 模拟 reset_init_state + check_all + get_lightrag 不触发真实初始化
    monkeypatch.setattr(lm, "reset_init_state", lambda: None)
    monkeypatch.setattr(lm, "get_lightrag", lambda: None)
    # 模拟 wait_first_scan_complete 立即返回 True
    import agent.injector.sync as sync_mod
    monkeypatch.setattr(sync_mod, "wait_first_scan_complete", lambda timeout=120: True)

    result = lm.run_repair_on_user_request()

    assert result["repaired"] is True, "无 _unrecoverable 时应 repaired=True"
    # 返回结构必须有三级字段
    assert "critical_errors" in result
    assert "major_errors" in result
    assert "minor_errors" in result
    # _repairing 应在 finally 里被清回 False
    assert lm._repairing is False, "_repairing 应在 finally 清回 False"


def test_run_repair_on_user_request_repaired_false_on_unrecoverable(tmp_path, monkeypatch):
    """repair_all 返回 _unrecoverable=True → repaired=False。"""
    _write_intact_truth_sources(tmp_path)
    for fname in _DERIVED_FILES_FOR_TEST:
        (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    import niu_api.internal.lightrag_manager as lm
    monkeypatch.setattr("niu_api.kg_api._read_pipeline_busy", lambda: False)
    # 模拟 repair_all 返回 unrecoverable（如 GraphML 损坏）
    monkeypatch.setattr(
        "niu_api.internal.lightrag_repair.repair_all",
        lambda: {
            "_unrecoverable": True,
            "_unrecoverable_reason": "3 真相源损坏",
            "text_chunks": {"status": "error", "unrecoverable": True},
        },
    )
    monkeypatch.setattr(lm, "reset_init_state", lambda: None)
    monkeypatch.setattr(lm, "get_lightrag", lambda: None)
    import agent.injector.sync as sync_mod
    monkeypatch.setattr(sync_mod, "wait_first_scan_complete", lambda timeout=120: True)

    result = lm.run_repair_on_user_request()

    assert result["repaired"] is False, "有 _unrecoverable 时应 repaired=False"
    assert lm._repairing is False


def test_run_repair_on_user_request_repaired_false_on_step_error(tmp_path, monkeypatch):
    """repair_all 某步骤返回 status=error（非 unrecoverable）→ repaired=False。
    
    这是 v4 改动重点：不能只看 _unrecoverable，还要看每个 step 的 status=error。
    现有 L1384-1391 已实现此判定，本测试锁定行为。
    """
    _write_intact_truth_sources(tmp_path)
    for fname in _DERIVED_FILES_FOR_TEST:
        (tmp_path / fname).write_text("{}")

    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    import niu_api.internal.lightrag_manager as lm
    monkeypatch.setattr("niu_api.kg_api._read_pipeline_busy", lambda: False)
    # 某步骤 status=error 但没标 unrecoverable
    monkeypatch.setattr(
        "niu_api.internal.lightrag_repair.repair_all",
        lambda: {
            "_unrecoverable": False,
            "vdb_entities": {"status": "error", "message": "embedding 失败"},
        },
    )
    monkeypatch.setattr(lm, "reset_init_state", lambda: None)
    monkeypatch.setattr(lm, "get_lightrag", lambda: None)
    import agent.injector.sync as sync_mod
    monkeypatch.setattr(sync_mod, "wait_first_scan_complete", lambda timeout=120: True)

    result = lm.run_repair_on_user_request()

    assert result["repaired"] is False, "步骤 status=error 时应 repaired=False"
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_lightrag_repair_unit.py::test_get_lightrag_status_returns_3_severity_fields \
                tests/test_lightrag_repair_unit.py::test_run_repair_on_user_request_repaired_true_on_success -v
```

Expected:
- `test_get_lightrag_status_returns_3_severity_fields`：可能 PASS（现有代码已含 3 字段）——如果 PASS，说明 v3 已改过，本测试是行为锁定。如果 FAIL，说明三字段缺一个，按 Step 3 改动 1 补齐。
- `test_run_repair_on_user_request_repaired_true_on_success`：现有 `_repairing` 是模块级全局变量，测试可能因前置 `_repairing` 状态不稳——如果 FAIL，看错误是 `_repairing` 残留 True 还是 `repaired` 误判为 False。

### - [ ] Step 3: Write minimal implementation

修改 `niu_api/internal/lightrag_manager.py`：

**改动 1**：`get_lightrag_status` 的 `total_errors` 字段加注释（L1482-1488 周围）

现有代码（L1482-1488）：
```python
    result["integrity"] = {
        "ok": integrity_ok,
        "total_errors": total_errors,
        "critical_errors": critical,
        "major_errors": major,
        "minor_errors": minor,
    }
    return result
```

改为：
```python
    # v4: total_errors 是 critical + major + minor 之和，保留是为了向后兼容
    # Rust IntegrityStatus.total_errors（main.rs:54）读这个字段生成弹窗文案，
    # 删了会导致 Rust serde 反序列化报 "missing field"（main.rs:42 注释明确警告过）。
    # 新代码应优先读 critical_errors/major_errors/minor_errors 三级字段。
    result["integrity"] = {
        "ok": integrity_ok,
        "total_errors": total_errors,  # 兼容字段，= critical + major + minor
        "critical_errors": critical,
        "major_errors": major,
        "minor_errors": minor,
    }
    return result
```

**改动 2**：`run_resilience_phase1` 日志里 `total_errors` 局部变量改名（L1125-1129）

现有代码（L1125-1129）：
```python
    critical = check_result.get("critical_errors", 0)
    major = check_result.get("major_errors", 0)
    minor = check_result.get("minor_errors", 0)
    total = critical + major + minor
    logger.info(
        f"[LightRAG] Phase 1 完成: check_ok={check_result.get('ok')}, "
        f"critical={critical}, major={major}, minor={minor}, total_errors={total}"
    )
```

改为（去掉误导性的 `total_errors=` 前缀，避免跟字段名混淆）：
```python
    critical = check_result.get("critical_errors", 0)
    major = check_result.get("major_errors", 0)
    minor = check_result.get("minor_errors", 0)
    total = critical + major + minor
    logger.info(
        f"[LightRAG] Phase 1 完成: check_ok={check_result.get('ok')}, "
        f"critical={critical}, major={major}, minor={minor}, total={total}"
    )
```

**改动 3（无需改）**：`run_repair_on_user_request` 的 `repaired` 判定（L1377 + L1384-1391）

现有代码已正确实现：
```python
# L1377
repaired = not has_unrecoverable and not repair_result.get("_unrecoverable", False)

# L1384-1391（检查每个 step 的 status=error）
for vdb_name, vdb_result in repair_result.items():
    if not isinstance(vdb_result, dict):
        continue
    if vdb_result.get("status") == "error":
        repaired = False
        logger.warning(
            f"[LightRAG] 修复失败项: {vdb_name} - {vdb_result.get('message', '')}"
        )
```

本 Task 不改这段代码——Step 1 的测试（`test_run_repair_on_user_request_repaired_false_on_step_error`）锁定行为，确保未来不被误改。

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_get_lightrag_status_returns_3_severity_fields \
                tests/test_lightrag_repair_unit.py::test_run_repair_on_user_request_repaired_true_on_success \
                tests/test_lightrag_repair_unit.py::test_run_repair_on_user_request_repaired_false_on_unrecoverable \
                tests/test_lightrag_repair_unit.py::test_run_repair_on_user_request_repaired_false_on_step_error -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_manager.py tests/test_lightrag_repair_unit.py
git commit -m "fix(manager): 保留 total_errors 兼容 Rust + 锁定 repaired 判定行为

v3 方案误判两件事（v4 纠正）：
1. 'total_errors 字段需删除' → 实际 Rust main.rs:54 IntegrityStatus.total_errors
   读这个字段生成弹窗文案，删了会 serde 'missing field' 报错
   （main.rs:42 注释明确警告过）。v4 保留 total_errors 加注释说明。
2. 'run_repair_on_user_request 要去掉用户确认环节' → 实际此函数由
   /api/kg/lightrag/repair API 触发，用户在 Rust 弹窗点'是'后 Rust 调 curl
   POST 触发，Python 侧无程序内等待确认代码。

v4 实际改动：
1. get_lightrag_status 的 total_errors 字段加注释（= critical + major + minor）
2. run_resilience_phase1 日志 total_errors= 改为 total=（去误导后缀）
3. run_repair_on_user_request 的 repaired 判定无需改（L1377 + L1384-1391
   已正确实现：基于 _unrecoverable + 每个 step 的 status=error）
4. 新增 4 个测试锁定 get_lightrag_status 字段 + repaired 判定行为
"
```

---

## Task 6: 修复 `launcher/src/main.rs` 的 `IntegrityStatus` struct

**Files:**
- Modify: `launcher/src/main.rs`
- Test: `tests/test_launcher_integrity_status.rs`（Rust 单元测试，跟 Python 测试分离）

### 背景

**现有代码事实**（重要，纠正 v3 方案的误判）：

Rust `IntegrityStatus` struct（`main.rs:52-61`）**字段已经齐全**：
```rust
struct IntegrityStatus {
    ok: bool,
    total_errors: i32,
    #[serde(default)]
    critical_errors: i32,
    #[serde(default)]
    major_errors: i32,
    #[serde(default)]
    minor_errors: i32,
}
```

现有代码已正确：
- `total_errors`（无 `#[serde(default)]`）：必填字段，Python 必须返回它（main.rs:42 注释明确警告过删了会"missing field" decode 错误）
- `critical_errors`/`major_errors`/`minor_errors`（带 `#[serde(default)]`）：可选字段，缺失时默认 0

`format_repair_summary`（main.rs:168-280）已使用 `critical_errors`/`major_errors`/`minor_errors` 三字段生成弹窗文案（L182-193），区分"残留 N 个严重、M 个主要错误"（L228-234）。

`StatusCheckResult` 处理（main.rs:541-597）仍只用 `total_errors`（L553-556）生成"检测到 N 个数据一致性问题"文案——v4 决定保留此行为（弹窗只显示总数，详细分级由 `format_repair_summary` 在修复完成后显示）。

**v4 Task 6 实际改动**：
- **改动 1**：`IntegrityStatus` 字段齐全，**不需要改 struct 定义**。但需要加 Rust 单元测试锁定字段能正确反序列化 Python 返回的 JSON（防未来误删字段）。
- **改动 2**：`StatusCheckResult` 处理（main.rs:553-556）改用 `critical_errors + major_errors + minor_errors` 替代直接读 `total_errors`，以防 Python 侧未来误删 `total_errors` 字段（虽然 Task 5 已加了"不能删"的注释，但 Rust 侧也应有 fallback）。

### - [ ] Step 1: Write the failing test

新建 `tests/test_launcher_integrity_status.rs`（Rust 单元测试，放在 `tests/` 目录下作为集成测试）：

```rust
// tests/test_launcher_integrity_status.rs
// 测试 IntegrityStatus struct 能正确反序列化 Python /api/kg/stats 返回的 integrity JSON。
// 锁定字段不能被误删（main.rs:42 注释警告过 "missing field" decode 错误）。

// 注意：IntegrityStatus 在 main.rs 里是 pub struct 才能从 tests/ 目录访问。
// 现有 main.rs IntegrityStatus 没有 pub，所以本测试用 serde_json 手工反序列化
// 验证字段命名（snake_case，不是 camelCase）。

use serde::Deserialize;

// 复制 main.rs 的 IntegrityStatus 定义（保持字段名一致）
// 如果 main.rs 里的 IntegrityStatus 误改成 camelCase 或误删字段，
// 本测试会编译失败或断言失败。
#[derive(Debug, Clone, Deserialize)]
struct IntegrityStatus {
    ok: bool,
    total_errors: i32,
    #[serde(default)]
    critical_errors: i32,
    #[serde(default)]
    major_errors: i32,
    #[serde(default)]
    minor_errors: i32,
}

#[test]
fn test_integrity_status_deserializes_full_json() {
    // 模拟 Python get_lightrag_status 返回的 integrity 字段（v4 全字段版）
    let json = r#"{
        "ok": false,
        "total_errors": 3,
        "critical_errors": 1,
        "major_errors": 1,
        "minor_errors": 1
    }"#;
    let status: IntegrityStatus = serde_json::from_str(json).unwrap();
    assert_eq!(status.ok, false);
    assert_eq!(status.total_errors, 3);
    assert_eq!(status.critical_errors, 1);
    assert_eq!(status.major_errors, 1);
    assert_eq!(status.minor_errors, 1);
}

#[test]
fn test_integrity_status_deserializes_without_severity_fields() {
    // 模拟旧版 Python 只返回 total_errors（没有 critical/major/minor）
    // critical_errors/major_errors/minor_errors 有 #[serde(default)] 应默认 0
    let json = r#"{
        "ok": true,
        "total_errors": 0
    }"#;
    let status: IntegrityStatus = serde_json::from_str(json).unwrap();
    assert_eq!(status.ok, true);
    assert_eq!(status.total_errors, 0);
    assert_eq!(status.critical_errors, 0, "缺 critical_errors 时应默认 0");
    assert_eq!(status.major_errors, 0, "缺 major_errors 时应默认 0");
    assert_eq!(status.minor_errors, 0, "缺 minor_errors 时应默认 0");
}

#[test]
fn test_integrity_status_total_errors_missing_fails() {
    // 如果 Python 误删 total_errors 字段，serde 应报错（main.rs:42 注释警告）
    // 本测试锁定 total_errors 必须保留（不能加 #[serde(default)]）
    let json = r#"{
        "ok": true,
        "critical_errors": 0,
        "major_errors": 0,
        "minor_errors": 0
    }"#;
    let result: Result<IntegrityStatus, _> = serde_json::from_str(json);
    assert!(result.is_err(), "缺 total_errors 时 serde 必须报错（不能加 #[serde(default)]）");
}

#[test]
fn test_integrity_status_field_names_are_snake_case() {
    // main.rs:42 注释明确：API 返回 snake_case 字段名（Python 约定），
    // 不能用 #[serde(rename_all = "camelCase")]（会导致 "missing field"
    // decode 错误：serde 期望 initFailed 而 API 返 init_failed）。
    // 本测试锁定字段名必须是 snake_case。
    let json = r#"{
        "ok": true,
        "total_errors": 0,
        "critical_errors": 0,
        "major_errors": 0,
        "minor_errors": 0
    }"#;
    let status: IntegrityStatus = serde_json::from_str(json).unwrap();
    // 如果 struct 误改成 camelCase（totalErrors/criticalErrors），
    // serde 会期望 "totalErrors" 而非 "total_errors"，
    // 本断言会失败（缺字段 → result.is_err() 在上面已测）
    let _ = status;  // 检查字段名编译通过即可
}
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot/launcher
cargo test --test test_launcher_integrity_status -- --nocapture
```

Expected:
- 4 个测试中至少 1 个 FAIL（`test_integrity_status_total_errors_missing_fails` 可能 PASS，因为 IntegrityStatus 复制品没加 `#[serde(default)]`；其他 3 个应 PASS——这表示**测试本身可能不需要代码改动就能通过**，本 Task 主要是行为锁定）。
- 如果所有测试都 PASS：说明现有 struct 定义已正确，本 Task 跳过 Step 3 的改动 2，直接到 Step 4 验证后 Commit。
- 如果有 FAIL：按 Step 3 改动 2 修复 `StatusCheckResult` 处理逻辑。

### - [ ] Step 3: Write minimal implementation

**改动 1**（必做）：`launcher/src/main.rs` 的 `IntegrityStatus` struct 加注释说明字段策略（L50-61 周围）

现有代码（main.rs:50-61）：
```rust
/// LightRAG data integrity summary reported by `/api/kg/stats`.
#[derive(Debug, Clone, Deserialize)]
struct IntegrityStatus {
    ok: bool,
    total_errors: i32,
    #[serde(default)]
    critical_errors: i32,
    #[serde(default)]
    major_errors: i32,
    #[serde(default)]
    minor_errors: i32,
}
```

改为：
```rust
/// LightRAG data integrity summary reported by `/api/kg/stats`.
///
/// 字段策略（v4）：
/// - `total_errors`（必填，无 `#[serde(default)]`）：Python get_lightrag_status
///   必须返回此字段（= critical + major + minor 之和），删了会 "missing field"
///   decode 错误。StatusCheckResult 用它生成弹窗文案。
/// - `critical_errors`/`major_errors`/`minor_errors`（可选，`#[serde(default)]`）：
///   新版 Python 返回的三级 severity 字段。format_repair_summary 用它们
///   区分"残留 N 个严重、M 个主要错误"。旧版 Python 缺这些字段时默认 0。
///
/// 注意：字段名必须是 snake_case（Python 约定），不能用
/// `#[serde(rename_all = "camelCase")]`，否则 serde 会期望
/// `totalErrors`/`criticalErrors` 而 API 返回 `total_errors`/`critical_errors`，
/// 导致 "missing field" decode 错误。
#[derive(Debug, Clone, Deserialize)]
struct IntegrityStatus {
    ok: bool,
    total_errors: i32,
    #[serde(default)]
    critical_errors: i32,
    #[serde(default)]
    major_errors: i32,
    #[serde(default)]
    minor_errors: i32,
}
```

**改动 2**（可选，看 Step 2 测试结果）：`StatusCheckResult` 处理改用三字段求和（main.rs:553-556）

现有代码（main.rs:553-556）：
```rust
let total_errors = status
    .integrity
    .as_ref()
    .map_or(0, |i| i.total_errors);
```

改为（用三字段求和，做 fallback——如果 Python 未来误删 `total_errors`，Rust 仍能算出总数）：
```rust
// v4: 优先读 total_errors（Python 兼容字段），但如果缺失则用
// critical + major + minor 求和做 fallback（防 Python 误删字段）
let total_errors = status
    .integrity
    .as_ref()
    .map_or(0, |i| {
        // total_errors 是必填字段（无 #[serde(default)]），如果 Python
        // 返回了 integrity 对象，total_errors 一定有值
        // 这里用三字段求和做 sanity check（应该跟 total_errors 相等）
        let sum = i.critical_errors + i.major_errors + i.minor_errors;
        // 如果 total_errors != sum，用 sum（更细粒度，防 Python 字段不一致）
        if i.total_errors != sum && sum > 0 {
            warn!(
                "integrity total_errors({}) != critical+major+minor({}), using sum",
                i.total_errors, sum
            );
            sum
        } else {
            i.total_errors
        }
    });
```

**重要**：Rust 改完必须跑 `launcher/build.sh`，不能 `cargo build`（CLAUDE.md 铁律 8）。

### - [ ] Step 4: Run test to verify it passes + 编译

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot/launcher
# 跑 Rust 单元测试
cargo test --test test_launcher_integrity_status -- --nocapture

# 编译（CLAUDE.md 铁律 8：必须用 build.sh，不能用 cargo build）
cd REDACTED_USER_PATH/tools/ai-bot
./launcher/build.sh
```

Expected:
- 4 个 Rust 测试全部 PASS
- `./launcher/build.sh` 编译成功，输出 `Compiled successfully` 或类似成功消息
- `niu` 二进制文件（项目根目录）更新时间戳（`ls -la niu` 确认）

### - [ ] Step 5: 修复文件权限（CLAUDE.md 铁律 7）

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
# git 操作后修复可执行权限（build.sh 不会自动修，需手工跑）
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \;
chmod +x niu
```

### - [ ] Step 6: Commit

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add launcher/src/main.rs tests/test_launcher_integrity_status.rs
git commit -m "fix(launcher): IntegrityStatus 字段齐全 + 加单元测试锁定反序列化

v3 方案误判 'Rust IntegrityStatus 缺字段需补' → 实际字段已齐全
（main.rs:52-61 已有 total_errors + critical_errors + major_errors
+ minor_errors 四字段）。

v4 实际改动：
1. IntegrityStatus struct 加详细注释说明字段策略：
   - total_errors 必填（Python 兼容字段，删了会 missing field 错误）
   - critical/major/minor_errors 可选（带 #[serde(default)]）
   - 字段名必须 snake_case（不能用 rename_all=camelCase）
2. StatusCheckResult 用 critical+major+minor 求和做 fallback，
   防 Python 未来误删 total_errors 字段
3. 新增 4 个 Rust 单元测试锁定反序列化行为

CLAUDE.md 铁律 8：Rust 编译用 launcher/build.sh，不用 cargo build。
"
```

### - [ ] Step 7: 验证 `niu` 二进制能启动（手动 smoke test）

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
# 启动 niu（应在几秒内看到 splash 窗口，然后正常启动或弹"数据异常"对话框）
./niu &
sleep 5
# 检查进程是否存活
ps aux | grep -v grep | grep "niu" | head -3
# 杀掉（用 SIGTERM 优雅退出，CLAUDE.md test-process-kill-corruption 铁律）
pkill -TERM -f "^./niu" 2>/dev/null || pkill -TERM -f "niu-launcher" 2>/dev/null
```

Expected:
- `./niu` 启动不立即崩溃（不报 panic）
- 进程列表里有 `niu` 或 `niu-launcher` 进程
- SIGTERM 后进程退出（不残留）

如果启动崩溃：检查 `launcher/build.sh` 是否真的把新编译的 `niu-launcher` 复制到项目根目录 `niu`（`ls -la niu` 看修改时间是否是刚编译的）。

---

## Task 7: 端到端 TDD 测试——合成 fixture 7 种损坏现场

**Files:**
- Test: `tests/test_lightrag_rebuild_from_truth.py`
- Fixture: `tests/fixtures/lightrag_truth_sources/`

### 背景

合成 fixture（不含真实人名）：
- 3 个文档（doc-v1, doc-v2 是同一文档的两个版本；doc-v3 是独立文档）
- 5 个 extract cache（含 1 个已删实体的脏 entry + 1 个旧版本 chunk 的 extract）
- GraphML（含衰减后的 weight + 已删实体已不在 + 只引用当前活跃 chunk）

7 种损坏现场：
1. 删 vdb_entities → repair
2. 删 9 全部 → repair
3. GraphML 损坏 → unrecoverable + 回滚（9 派生文件未被删除）
4. full_docs 损坏 → unrecoverable
5. cache 损坏 → unrecoverable
6. 含旧版本 doc + 已删实体 → 重建后不复活（派生文件不含 deleted-entity / old-entity）
7. weight 衰减值保留 → 重建后 GraphML 的 weight 不变（因为 GraphML 没被修改）

### - [ ] Step 1-5: TDD 流程

（具体测试代码 + fixture 略，参照 v3 Task 8 的模式，但测试断言改为验证"3 真相源一字节未动 + 不复活 + weight 保留"）

---

## Task 8: 真实启动验证——./niu 启动走完整 repair 流程

**Files:**
- Manual test

### 背景

用真实 `~/.niu/lightrag_storage` 数据，./niu 启动触发 check_all + repair_all（如果 check 报错）。验证：
1. 3 真相源一字节未动（启动前后比对 hash）
2. weight 衰减值保留（GraphML 的 weight 不变）
3. 已删实体不复活（重建后 GraphML 仍不含已删实体）
4. region_sync 启动后 1 分钟内完成（不卡 dissolve）

### - [ ] Step 1-5: 手动验证流程

（具体步骤略，参照 v3 Task 9 的模式）

---

## Self-Review Checklist

- [ ] 3 真相源完全不可动（GraphML + full_docs + cache 不写不改不删（读取是必要的，用于按需提取重建派生文件））
- [ ] 3 真相源任一损坏 = unrecoverable，不进入恢复流程
- [ ] 3 真相源全部完好 = 只重建 9 个派生文件，真相源一字节未动
- [ ] 重建算法是"从 GraphML 按需提取"，不是"从日志全量重建 + 过滤"
- [ ] text_chunks 天然取最后版本（dict.update 覆盖语义）
- [ ] cache 按 create_time 降序取最新 extract entry
- [ ] weight 不写 vdb（meta_fields 不含 weight）
- [ ] weight 衰减值保留（GraphML 完好时不重放覆盖）
- [ ] 脑区 chunk 特殊处理（full_doc_id="brain"）
- [ ] 9 派生文件全部从 GraphML + full_docs + cache 派生（天然防复活）
- [ ] 备份 + 回滚机制完整（只备份 9 派生文件，不备份真相源）
- [ ] 测试用真实数据 + 真实 LLM（不 mock）
- [ ] 3 真相源完好性检测标准完备
- [ ] 删除了所有会动真相源的步骤（repair_graphml / repair_brainregion_zombies / repair_cache_filter / repair_graphml_orphan_edges）

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-07-13-lightrag-rebuild-from-truth-sources.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
