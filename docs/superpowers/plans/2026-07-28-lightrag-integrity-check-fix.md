# LightRAG 检测逻辑误判修复方案 v2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 LightRAG 启动检测把"派生文件缺失"误判为"知识图谱损坏"的 10 天未解 bug，且修复"用户点尝试修复仍失败"的真正根因（partial 真相源状态被误判为不可恢复）。

**Architecture:** 改 3 处核心逻辑：
1. `niu_api/internal/lightrag_integrity.py`：把 vdb_* 从 `_DERIVED_FILES` 清单移出，让 `_check_derived_missing` 只检查 6 个 kv_store 派生（且全部不再报 major，返回空 errors）；保留并启用 `_check_vdb_missing`（数据一致性检查：GraphML node/edge ⊆ vdb 向量集合）
2. `niu_api/internal/lightrag_repair.py`：修 `_check_truth_sources_intact` 的 partial 误判——GraphML 有内容 + full_docs/cache 缺失是脑区/Skills 合法状态，不判为 `intact=False`
3. `tests/test_lightrag_repair_unit.py`：更新两个现有测试的断言（派生缺失不再报 major；partial 真相源不再 unrecoverable）

**Tech Stack:** Python 3.11 + pytest + pytest-asyncio + LightRAG fork 版（/Users/lilei/tools/LightRAG/）

---

## 1. 背景与诊断（基于完整逻辑链条分析）

### 1.1 用户原话

> "现在的知识图谱文件是缺失，但不是损坏，它是正常缺失，因为你还没有入库内容呢。它正常缺失没损坏，你为什么非得说它是坏的呢？"

> "还是需要去读知识图谱的源代码，分析什么是真损坏，如何检查判断它是真损坏，有没有一个好的方法，而不是只靠文件缺失不缺失来判断。"

> "我不同意重建这些空文件。你重建它干什么？"

### 1.2 真损坏的本质（基于 LightRAG 源码研究）

LightRAG fork 版源码证明**所有存储在文件缺失时都合法初始化为空**，不报错：

- `JsonKVStorage.initialize()` (`lightrag/kg/json_kv_impl.py:62`): `load_json(self._file_name) or {}` — 文件缺失自动当空 dict
- `NetworkXStorage.__post_init__` (`lightrag/kg/networkx_impl.py:78`): `preloaded_graph or nx.Graph()` — GraphML 缺失即空图
- `NanoVectorDBStorage.__post_init__` (`lightrag/kg/nano_vector_db_impl.py:61`): 文件缺失自动建空库

**因此文件缺失从来不是损坏**。真损坏的本质是**同一逻辑对象在不同存储中的数据不一致**：
- GraphML 里有 node X 但 `vdb_entities` 没有 X 的向量 → 查询 X 时 `_get_node_data` (`operate.py:4417`) 拿不到向量召回 → 查询失败
- GraphML 里有 edge (A,B) 但 `vdb_relationships` 无对应向量 → `_get_edge_data` (`operate.py:4694`) 失败

### 1.3 当前 bug 的完整逻辑链条

**第一层（检测误判）**：
- `_DERIVED_FILES` 清单（`lightrag_integrity.py:29-39`）把 3 vdb + 6 kv_store 派生全列进去
- `_check_derived_missing`（L327-361）对清单里所有 9 文件做存在性检查，缺任一就报 major
- 用户现场缺 5 个 kv_store 派生（doc_status / entity_chunks / relation_chunks / full_entities / full_relations）→ 报 5 个 major → `check_all` 返回 `ok=False`
- `run_resilience_phase1`（`lightrag_manager.py:1359-1392`）读到 `need_repair=True` → lifespan 跳过所有 LightRAG 依赖的初始化
- Rust launcher（`launcher/src/main.rs:574-634`）检测到 `integrity.ok=False` → 弹 rfd 弹窗

**第二层（修复失败）**：
- 用户点"尝试修复" → `run_repair_on_user_request` → `repair_all` → `_check_truth_sources_intact`（`lightrag_repair.py:419-536`）
- L509-528 的 partial 判定：只要 3 真相源中"部分 has_content + 部分 absent"就判 `intact=False`
- 用户现场 GraphML has_content + full_docs/cache absent（脑区/Skills 路径下本来就不该有这俩）→ 触发 partial 误判 → `_unrecoverable=True`
- `repair_all` 立即 return，不执行任何重建 → 修复失败

### 1.4 脑区/Skills 注入路径写入的文件（源码研究确认）

`ainsert_custom_kg` (`lightrag/lightrag.py:2376-2602`) 脑区/Skills 路径只写：
- `graph_chunk_entity_relation.graphml`（脑区节点+Skills 实体）
- `vdb_chunks.json` / `vdb_entities.json` / `vdb_relationships.json`（向量索引）
- 可选：`kv_store_text_chunks.json`（chunk 文本，脑区路径会写）

**不写**：
- `kv_store_full_docs.json`（文档原文池，文档入库才有）
- `kv_store_llm_response_cache.json`（LLM 抽取结果缓存，文档入库才有）
- `kv_store_doc_status.json`（文档状态，文档入库才有）
- `kv_store_entity_chunks.json` / `kv_store_relation_chunks.json`（实体- chunk 映射，文档入库 pipeline 才写）
- `kv_store_full_entities.json` / `kv_store_full_relations.json`（Phase 3 派生，文档入库 pipeline 才写）

**`_insert_done`（L2339-2360）虽对所有 storage 调 `index_done_callback`，但 `JsonKVStorage.index_done_callback`（`json_kv_impl.py:77-104`）只在 `storage_updated.value=True` 时写盘——没 upsert 过的 storage 不写文件。** 所以脑区路径下这 7 个文件缺失是设计使然。

### 1.5 真正需要修复的场景（必须保留 major/critical）

1. **3 真相源任一 corrupt**（JSON/XML 解析失败、非 dict）→ critical（已有逻辑正确，`_check_truth_source` L179-216 + `_check_truth_source_graphml` L364-389）
2. **GraphML 有 node 但 vdb_entities 缺对应向量** → major（数据不一致，`_check_vdb_missing` L258-324 已实现正确）
3. **GraphML 有 edge 但 vdb_relationships 缺对应向量** → major（同上）

**派生 kv_store 文件缺失**（doc_status / entity_chunks / relation_chunks / full_entities / full_relations / text_chunks）不是损坏——LightRAG 内部 `JsonKVStorage.initialize` 把缺失文件当空 dict，运行时按需 upsert。**本方案不主动重建、不写空文件**。

### 1.6 第一轮审查发现的 3 个致命遗漏（本 v2 方案必须修复）

| # | 审查发现 | v1 方案遗漏 | v2 方案修复 |
|---|---|---|---|
| P1 | `lightrag_repair.py:_check_truth_sources_intact` L509-528 把 partial 状态（GraphML 有 + full_docs/cache 缺）误判为 `intact=False` → 用户点"尝试修复"直接 unrecoverable | v1 只改检测不改修复，Task 4 注释写"lightrag_repair.py 不改" | Task 4 新增：修 `_check_truth_sources_intact` partial 误判 |
| P2 | `_check_vdb_missing`（L258-324）虽实现正确但被标 `pyright: ignore[reportUnusedFunction]`，`check_all` 不调用 → vdb 缺向量场景检测不到 | v1 声称"保留 vdb 一致性检测"但实际未启用 | Task 3 新增：`check_all` 内 `all_errors.extend(_check_vdb_missing(storage_dir))` |
| P3 | v1 方案文档第 1.4 节、Task 3 docstring 写"LightRAG check_and_migrate_data 自动重建派生缓存"——但用户明确反对重建，且代码核查 `check_and_migrate_data`（`lightrag.py:845-913`）只重建 `full_entities`/`full_relations` 且依赖 `doc_status.get_docs_by_status(PROCESSED)`，脑区路径根本不进入迁移逻辑 | v1 误导性说法 | Task 5 新增：删除所有"自动重建"说法，改为"派生缺失时 LightRAG 内部按需 lazy 加载为空 dict，本方案不主动调用任何重建" |

---

## 2. File Structure

需要修改的文件：

| 文件 | 责任 | 改动类型 |
|---|---|---|
| `niu_api/internal/lightrag_integrity.py` | 检测逻辑主体 | 重写 `_check_derived_missing` + `check_all` 启用 vdb 检查 + 调整 `_DERIVED_FILES` 清单 |
| `niu_api/internal/lightrag_repair.py` | 修复前真相源完好性检查 | 修 `_check_truth_sources_intact` partial 误判 |
| `tests/test_lightrag_repair_unit.py` | 现有 repair 单元测试 | 更新 2 个断言（派生缺失不再 major；partial 不再 unrecoverable） |
| `tests/test_lightrag_integrity_check.py` | 新增检测逻辑单元测试 | **新建**，覆盖 8 类场景 |

**不动的文件**：
- `niu_api/internal/lightrag_manager.py` — `run_resilience_phase1` / `get_lightrag` 接口不变
- `niu_api/__main__.py` — lifespan 流程不变
- `launcher/src/main.rs` — Rust 弹窗逻辑不变
- `niu_api/llm_proxy.py` / `niu_api/kg_api.py` — 调用方接口不变

---

## 3. 测试场景矩阵（先写失败测试，再改代码）

### 场景清单

| # | 场景 | GraphML | full_docs | cache | 6 派生 kv_store | 3 vdb | 期望 ok | 期望 major | 期望 critical |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 全新用户（啥都没有） | absent | absent | absent | absent | absent | True | 0 | 0 |
| 2 | **脑区+Skills 已注入（用户当前现场）** | has nodes | absent | absent | 6 missing | 3 exists & 一致 | True | 0 | 0 |
| 3 | 文档入库后正常状态 | has nodes | has content | has content | 6 exists | 3 exists & 一致 | True | 0 | 0 |
| 4 | vdb_entities 缺向量（真损坏） | has nodes | has content | has content | 6 exists | vdb_e 缺 X | False | ≥1 | 0 |
| 5 | vdb_relationships 缺向量（真损坏） | has edges | has content | has content | 6 exists | vdb_r 缺 (A,B) | False | ≥1 | 0 |
| 6 | GraphML corrupt（critical） | corrupt XML | absent | absent | absent | absent | False | 0 | ≥1 |
| 7 | full_docs corrupt（critical） | has nodes | corrupt JSON | absent | 6 missing | 3 exists | False | 0 | ≥1 |
| 8 | 派生文件部分缺失（混合） | has nodes | has content | has content | 3 missing | 3 exists & 一致 | True | 0 | 0 |
| 9 | **partial 真相源 + 修复入口不再 unrecoverable** | has nodes | absent | absent | 6 missing | 3 exists | True (check_all) | 0 | 0 | + repair_all 不返回 _unrecoverable |

**场景 2 是核心**：用户当前现场，必须从"误判 5 major"改成"ok=True 正常启动"。
**场景 9 是修复失败根因**：`_check_truth_sources_intact` partial 误判必须修复。

---

## 4. Task 分解

### Task 1: 备份 + 影响分析

**Files:**
- 无代码改动，仅 git 操作

- [ ] **Step 1: 确认工作区干净**

```bash
cd /Users/lilei/tools/ai-bot && git status
```

期望：要么干净，要么只有上一轮 probe 修复遗留的 `niu_api/compat.py` + `ui/main/main.js`。

- [ ] **Step 2: 临时提交当前状态作为基线**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/compat.py ui/main/main.js
git commit -m "backup: lightrag-integrity-fix-v2 基线（probe 修复遗留）

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

如果工作区已干净，跳过此步（遵守"工作区干净时禁止 git commit --allow-empty 备份"铁律）。

- [ ] **Step 3: 确认 gitnexus 影响范围**

已用 `mcp__gitnexus__impact` 分析 `_check_derived_missing`：
- 风险等级：LOW
- 直接调用方：`check_all`（同文件）
- 间接调用方：`run_resilience_phase1` / `run_repair_on_user_request` / `get_lightrag_status`
- 上层：`lifespan` / `lightrag_status` / `graph_stats`
- 返回结构不变（仍是 `list[dict]`），调用方接口不破坏

`_check_truth_sources_intact` 影响范围：
- 直接调用方：`repair_all`（`lightrag_repair.py`）
- 间接调用方：`run_repair_on_user_request`
- 返回结构不变（仍是 `dict[str, Any]`）

---

### Task 2: 写失败测试（场景 2/9 是核心）

**Files:**
- Create: `tests/test_lightrag_integrity_check.py`

- [ ] **Step 1: 写测试文件**

```python
"""LightRAG 检测逻辑单元测试 v2——验证"派生文件缺失不是损坏"。

修复 bug：脑区+Skills 已注入但未入库文档时，5 个派生 kv_store 文件缺失
被误判为 major 损坏，触发修复弹窗。正确行为：派生缺失让 LightRAG 内部
按需 lazy 加载为空 dict，不阻断启动，不主动重建。

场景矩阵见 docs/superpowers/plans/2026-07-28-lightrag-integrity-check-fix.md 第 3 节。
"""
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from niu_api.internal.lightrag_integrity import check_all


def _write_graphml(path: Path, nodes: list[tuple[str, str]] | None = None,
                   edges: list[tuple[str, str]] | None = None) -> None:
    """写一个最小合法 GraphML 文件。"""
    nsmap = {"g": "http://graphml.graphdrawing.org/xmlns"}
    ET.register_namespace("", nsmap["g"])
    root = ET.Element("{%s}graphml" % nsmap["g"])
    key = ET.SubElement(root, "{%s}key" % nsmap["g"])
    key.set("id", "d1")
    key.set("for", "node")
    key.set("attr.name", "entity_type")
    key.set("attr.type", "string")
    graph = ET.SubElement(root, "{%s}graph" % nsmap["g"])
    graph.set("id", "G")
    graph.set("edgedefault", "undirected")
    for node_id, entity_type in (nodes or []):
        node = ET.SubElement(graph, "{%s}node" % nsmap["g"])
        node.set("id", node_id)
        data = ET.SubElement(node, "{%s}data" % nsmap["g"])
        data.set("key", "d1")
        data.text = entity_type
    for src, tgt in (edges or []):
        edge = ET.SubElement(graph, "{%s}edge" % nsmap["g"])
        edge.set("source", src)
        edge.set("target", tgt)
    path.write_text(ET.tostring(root, encoding="unicode"))


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False))


def _write_vdb(path: Path, entries: list[dict]) -> None:
    _write_json(path, {"data": entries, "__type__": "NanoVectorDB"})


def _make_vdb_entity(name: str) -> dict:
    return {"id": hash(name) & 0xFFFFFF, "entity_name": name, "vector": [0.1] * 768}


def _make_vdb_relation(src: str, tgt: str) -> dict:
    s, t = sorted([src, tgt])
    return {"id": hash(s + t) & 0xFFFFFF, "src_id": s, "tgt_id": t, "vector": [0.1] * 768}


@pytest.fixture
def storage_dir(tmp_path, monkeypatch):
    """重定向 _STORAGE_DIR 到 tmp_path。"""
    from niu_api.internal import lightrag_integrity
    monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", tmp_path)
    monkeypatch.setattr(lightrag_integrity, "_resolve_storage_dir", lambda: tmp_path)
    return tmp_path


# ============================================================================
# 场景 1: 全新用户（啥都没有）→ ok=True
# ============================================================================

def test_scenario_1_empty_user(storage_dir):
    """3 真相源全 absent + 9 派生全 absent → ok=True, major=0。"""
    result = check_all()
    assert result["ok"] is True
    assert result["major_errors"] == 0
    assert result["critical_errors"] == 0


# ============================================================================
# 场景 2: 脑区+Skills 已注入（用户当前现场）→ ok=True
# ============================================================================

def test_scenario_2_brainregion_skills_injected_no_full_docs(storage_dir):
    """GraphML 有 node + 3 vdb 存在且一致 + 6 派生 kv_store 缺失 + full_docs/cache 缺失。

    这是脑区/Skills 注入后的合法中间状态，不应判为损坏。
    """
    nodes = [("脑区_记忆", "brain_region"), ("skill_code_review", "skill")]
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml",
                   nodes=nodes, edges=[])
    _write_vdb(storage_dir / "vdb_chunks.json", [_make_vdb_entity("chunk_1")])
    _write_vdb(storage_dir / "vdb_entities.json",
               [_make_vdb_entity("脑区_记忆"), _make_vdb_entity("skill_code_review")])
    _write_vdb(storage_dir / "vdb_relationships.json", [])
    _write_json(storage_dir / "kv_store_text_chunks.json",
                {"chunk_1": {"content": "xxx", "full_doc_id": "brain_脑区_记忆"}})
    # full_docs / cache / doc_status / entity_chunks / relation_chunks /
    # full_entities / full_relations 都不写（脑区路径不触发）

    result = check_all()
    assert result["ok"] is True, f"脑区+Skills 注入后不应判损坏，errors={result['errors']}"
    assert result["major_errors"] == 0, f"不应有 major，errors={result['errors']}"
    assert result["critical_errors"] == 0


# ============================================================================
# 场景 3: 文档入库后正常状态 → ok=True
# ============================================================================

def test_scenario_3_full_document_ingested(storage_dir):
    """3 真相源全有内容 + 6 派生 kv_store + 3 vdb 全存在且一致 → ok=True。"""
    nodes = [("entity_1", "object"), ("entity_2", "object")]
    edges = [("entity_1", "entity_2")]
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml",
                   nodes=nodes, edges=edges)
    _write_json(storage_dir / "kv_store_full_docs.json",
                {"doc_1": {"content": "doc text"}})
    _write_json(storage_dir / "kv_store_llm_response_cache.json",
                {"resp_1": {"return": "cache text"}})
    _write_json(storage_dir / "kv_store_text_chunks.json",
                {"chunk_1": {"content": "chunk text", "full_doc_id": "doc_1"}})
    _write_json(storage_dir / "kv_store_doc_status.json",
                {"doc_1": {"content": "doc text", "chunks_list": ["chunk_1"]}})
    _write_json(storage_dir / "kv_store_entity_chunks.json",
                {"entity_1": ["chunk_1"]})
    _write_json(storage_dir / "kv_store_relation_chunks.json",
                {f"entity_1##entity_2": ["chunk_1"]})
    _write_json(storage_dir / "kv_store_full_entities.json",
                {"entity_1": {"entity_name": "entity_1"}})
    _write_json(storage_dir / "kv_store_full_relations.json",
                {f"entity_1##entity_2": {"src_id": "entity_1", "tgt_id": "entity_2"}})
    _write_vdb(storage_dir / "vdb_chunks.json", [_make_vdb_entity("chunk_1")])
    _write_vdb(storage_dir / "vdb_entities.json",
               [_make_vdb_entity("entity_1"), _make_vdb_entity("entity_2")])
    _write_vdb(storage_dir / "vdb_relationships.json",
               [_make_vdb_relation("entity_1", "entity_2")])

    result = check_all()
    assert result["ok"] is True, f"errors={result['errors']}"
    assert result["major_errors"] == 0
    assert result["critical_errors"] == 0


# ============================================================================
# 场景 4: vdb_entities 缺向量（真损坏）→ major≥1
# ============================================================================

def test_scenario_4_vdb_entities_missing_is_real_corruption(storage_dir):
    """GraphML 有 node 但 vdb_entities 缺对应向量 → major≥1（真损坏，数据不一致）。"""
    nodes = [("entity_1", "object"), ("entity_2", "object")]
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml",
                   nodes=nodes, edges=[])
    # vdb_entities 只有 entity_1，缺 entity_2
    _write_vdb(storage_dir / "vdb_entities.json", [_make_vdb_entity("entity_1")])
    _write_vdb(storage_dir / "vdb_chunks.json", [])
    _write_vdb(storage_dir / "vdb_relationships.json", [])
    # 派生 kv_store 全存在（避免被派生缺失干扰）
    _write_json(storage_dir / "kv_store_text_chunks.json", {})
    _write_json(storage_dir / "kv_store_doc_status.json", {})
    _write_json(storage_dir / "kv_store_entity_chunks.json", {})
    _write_json(storage_dir / "kv_store_relation_chunks.json", {})
    _write_json(storage_dir / "kv_store_full_entities.json", {})
    _write_json(storage_dir / "kv_store_full_relations.json", {})
    _write_json(storage_dir / "kv_store_full_docs.json", {"doc_1": {"content": "x"}})
    _write_json(storage_dir / "kv_store_llm_response_cache.json", {})

    result = check_all()
    assert result["ok"] is False, "vdb_entities 缺向量应判损坏"
    assert result["major_errors"] >= 1
    vdb_errors = [e for e in result["errors"] if e.get("check") == "vdb_entities_missing"]
    assert len(vdb_errors) >= 1


# ============================================================================
# 场景 5: vdb_relationships 缺向量（真损坏）→ major≥1
# ============================================================================

def test_scenario_5_vdb_relationships_missing_is_real_corruption(storage_dir):
    """GraphML 有 edge 但 vdb_relationships 缺对应向量 → major≥1（真损坏）。"""
    nodes = [("entity_1", "object"), ("entity_2", "object")]
    edges = [("entity_1", "entity_2")]
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml",
                   nodes=nodes, edges=edges)
    _write_vdb(storage_dir / "vdb_entities.json",
               [_make_vdb_entity("entity_1"), _make_vdb_entity("entity_2")])
    _write_vdb(storage_dir / "vdb_relationships.json", [])
    _write_vdb(storage_dir / "vdb_chunks.json", [])
    _write_json(storage_dir / "kv_store_text_chunks.json", {})
    _write_json(storage_dir / "kv_store_doc_status.json", {})
    _write_json(storage_dir / "kv_store_entity_chunks.json", {})
    _write_json(storage_dir / "kv_store_relation_chunks.json", {})
    _write_json(storage_dir / "kv_store_full_entities.json", {})
    _write_json(storage_dir / "kv_store_full_relations.json", {})
    _write_json(storage_dir / "kv_store_full_docs.json", {"doc_1": {"content": "x"}})
    _write_json(storage_dir / "kv_store_llm_response_cache.json", {})

    result = check_all()
    assert result["ok"] is False, "vdb_relationships 缺向量应判损坏"
    assert result["major_errors"] >= 1
    vdb_errors = [e for e in result["errors"] if e.get("check") == "vdb_relationships_missing"]
    assert len(vdb_errors) >= 1


# ============================================================================
# 场景 6: GraphML corrupt（critical）
# ============================================================================

def test_scenario_6_graphml_corrupt_is_critical(storage_dir):
    """GraphML XML 解析失败 → critical（真损坏，不可恢复）。"""
    (storage_dir / "graph_chunk_entity_relation.graphml").write_text(
        "<?xml version='1.0'?><not-valid-graphml><broken"
    )
    result = check_all()
    assert result["ok"] is False
    assert result["critical_errors"] >= 1


# ============================================================================
# 场景 7: full_docs corrupt（critical）
# ============================================================================

def test_scenario_7_full_docs_corrupt_is_critical(storage_dir):
    """kv_store_full_docs.json JSON 解析失败 → critical。"""
    nodes = [("entity_1", "object")]
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml",
                   nodes=nodes, edges=[])
    _write_vdb(storage_dir / "vdb_entities.json", [_make_vdb_entity("entity_1")])
    _write_vdb(storage_dir / "vdb_chunks.json", [])
    _write_vdb(storage_dir / "vdb_relationships.json", [])
    (storage_dir / "kv_store_full_docs.json").write_text("{not valid json")
    result = check_all()
    assert result["ok"] is False
    assert result["critical_errors"] >= 1


# ============================================================================
# 场景 8: 派生文件部分缺失（混合）→ ok=True（派生缺失不报）
# ============================================================================

def test_scenario_8_partial_derived_missing_is_ok(storage_dir):
    """3 真相源有内容 + vdb 一致 + 6 派生 kv_store 中 3 个缺失 → ok=True。

    派生 kv_store 缺失不是损坏，LightRAG 内部按需 lazy 加载为空 dict。
    本方案不主动重建、不写空文件。
    """
    nodes = [("entity_1", "object")]
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml",
                   nodes=nodes, edges=[])
    _write_vdb(storage_dir / "vdb_entities.json", [_make_vdb_entity("entity_1")])
    _write_vdb(storage_dir / "vdb_chunks.json", [])
    _write_vdb(storage_dir / "vdb_relationships.json", [])
    _write_json(storage_dir / "kv_store_full_docs.json", {"doc_1": {"content": "x"}})
    _write_json(storage_dir / "kv_store_llm_response_cache.json", {})
    # 只写 text_chunks + doc_status，缺 entity_chunks / relation_chunks / full_entities / full_relations
    _write_json(storage_dir / "kv_store_text_chunks.json", {})
    _write_json(storage_dir / "kv_store_doc_status.json", {})

    result = check_all()
    assert result["ok"] is True, f"派生缺失不应判损坏，errors={result['errors']}"
    assert result["major_errors"] == 0
    assert result["critical_errors"] == 0


# ============================================================================
# 场景 9: partial 真相源 + 修复入口不再 unrecoverable
# ============================================================================

def test_scenario_9_partial_truth_sources_not_unrecoverable(storage_dir, monkeypatch):
    """GraphML 有 node + full_docs/cache absent → _check_truth_sources_intact 应返回 intact=True。

    这是用户上次"尝试修复"失败的根因：partial 状态被误判为 unrecoverable。
    修复后脑区/Skills 合法状态应允许 repair_all 进入重建分支。
    """
    nodes = [("脑区_记忆", "brain_region")]
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml",
                   nodes=nodes, edges=[])
    _write_vdb(storage_dir / "vdb_entities.json", [_make_vdb_entity("脑区_记忆")])
    _write_vdb(storage_dir / "vdb_chunks.json", [])
    _write_vdb(storage_dir / "vdb_relationships.json", [])
    _write_json(storage_dir / "kv_store_text_chunks.json", {})
    # full_docs / cache 不写（脑区路径合法状态）

    # 重定向 lightrag_repair 的 storage_dir
    from niu_api.internal import lightrag_repair
    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", storage_dir)
    if hasattr(lightrag_repair, "_storage_dir"):
        monkeypatch.setattr(lightrag_repair, "_storage_dir", lambda: storage_dir)

    from niu_api.internal.lightrag_repair import _check_truth_sources_intact
    result = _check_truth_sources_intact()
    assert result["intact"] is True, \
        f"partial 真相源（GraphML 有 + full_docs/cache 缺）应判 intact=True，reason: {result}"
```

- [ ] **Step 2: 跑测试确认场景 2/8/9 失败（场景 1/3/4/5/6/7 视现有逻辑而定）**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python3 -m pytest tests/test_lightrag_integrity_check.py -v
```

期望：场景 2/8 FAIL（误判 major），场景 9 FAIL（partial 误判 intact=False）。

- [ ] **Step 3: 提交失败测试**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_lightrag_integrity_check.py
git commit -m "test: 加 LightRAG 检测逻辑误判测试 v2（场景 2/9 是用户现场）

9 场景覆盖：
- 场景 1 全新用户 ok
- 场景 2 脑区+Skills 注入后 6 派生缺失（用户当前现场，应 ok）
- 场景 3 文档入库后正常 ok
- 场景 4/5 vdb 缺向量真损坏（数据不一致）
- 场景 6/7 GraphML/full_docs corrupt
- 场景 8 派生部分缺失 ok
- 场景 9 partial 真相源不再 unrecoverable（修复失败根因）

当前代码：场景 2/8/9 误判，TDD 红灯。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: 重写 `lightrag_integrity.py` 检测逻辑

**Files:**
- Modify: `niu_api/internal/lightrag_integrity.py`

**核心改动**：
1. `_DERIVED_FILES` 清单移除 3 个 vdb 文件（vdb 由 `_check_vdb_missing` 数据一致性检查负责）
2. `_check_derived_missing` 改为返回空列表（派生 kv_store 缺失不报 major，不修复、不重建、不写空文件）
3. `check_all` 启用 `_check_vdb_missing` 调用（数据一致性检查）

- [ ] **Step 1: 用 Edit 工具调整 `_DERIVED_FILES` 清单（移除 3 个 vdb 文件）**

`old_string`（L28-39）：

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
```

`new_string`：

```python
# 6 派生 kv_store 文件（仅用于文档入库 pipeline，脑区/Skills 路径不写）
# 注意：lightrag_repair._DERIVED_FILES 仍含 9 个文件（含 3 vdb）用于 repair_all 删除派生，
# 本清单只列 kv_store 派生用于检测（vdb 由 _check_vdb_missing 数据一致性检查负责）。
# 派生 kv_store 缺失不是损坏——LightRAG JsonKVStorage.initialize 把缺失文件当空 dict。
_DERIVED_FILES_KVSTORE = [
    "kv_store_text_chunks.json",
    "kv_store_doc_status.json",
    "kv_store_entity_chunks.json",
    "kv_store_relation_chunks.json",
    "kv_store_full_entities.json",
    "kv_store_full_relations.json",
]
```

- [ ] **Step 2: 用 Edit 工具重写 `_check_derived_missing` 函数体**

`old_string`（L327-361，原函数完整体）：

```python
def _check_derived_missing(storage_dir: Path) -> list[dict[str, Any]]:
    """检测 9 派生文件 missing。

    全新用户场景（3 真相源都不存在）时，派生文件 missing 不报错
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

`new_string`：

```python
def _check_derived_missing(storage_dir: Path) -> list[dict[str, Any]]:
    """检测派生 kv_store 文件缺失。

    v2 修复（2026-07-28）：派生 kv_store 文件缺失不是损坏。
    LightRAG fork 版的脑区/Skills 注入路径只写 GraphML + 3 vdb + 可选 text_chunks，
    不写 doc_status / entity_chunks / relation_chunks / full_entities / full_relations。
    LightRAG `JsonKVStorage.initialize`（json_kv_impl.py:62）`load_json() or {}` 把
    缺失文件当空 dict，运行时按需 upsert——**本方案不主动调用任何重建，不写空文件**。

    真损坏由 `_check_vdb_missing`（vdb 与 GraphML 数据一致性）和
    `_check_truth_source`（3 真相源 corrupt）负责。

    Returns:
        空列表（保留函数签名兼容 `check_all` 调用方）。
    """
    return []
```

- [ ] **Step 3: 用 Edit 工具在 `check_all` 内启用 `_check_vdb_missing`**

`old_string`（L430-432）：

```python
    # 2. 检测 9 派生文件 missing
    derived_errors = _check_derived_missing(storage_dir)
    all_errors.extend(derived_errors)
```

`new_string`：

```python
    # 2. 检测派生 kv_store 文件缺失（v2：不再报 major，派生缺失不是损坏）
    derived_errors = _check_derived_missing(storage_dir)
    all_errors.extend(derived_errors)

    # 3. 检测 vdb 与 GraphML 数据一致性（真损坏：node/edge 缺对应向量）
    #    v2 启用：原为死代码（标 pyright: ignore），现 check_all 主动调用
    vdb_errors = _check_vdb_missing(storage_dir)
    all_errors.extend(vdb_errors)
```

并在 `checks` dict 里新增 vdb_missing 项（L444-447）：

`old_string`：

```python
        "checks": {
            "truth_source": {"name": "truth_source", "errors": truth_errors},
            "derived_missing": {"name": "derived_missing", "errors": derived_errors},
        },
```

`new_string`：

```python
        "checks": {
            "truth_source": {"name": "truth_source", "errors": truth_errors},
            "derived_missing": {"name": "derived_missing", "errors": derived_errors},
            "vdb_missing": {"name": "vdb_missing", "errors": vdb_errors},
        },
```

- [ ] **Step 4: 清 pycache + 跑场景测试**

```bash
cd /Users/lilei/tools/ai-bot
find niu_api -name "__pycache__" -exec rm -rf {} + 2>/dev/null
python/bin/python3 -m pytest tests/test_lightrag_integrity_check.py -v
```

期望：9 场景中场景 1-8 全 PASS，场景 9 仍 FAIL（场景 9 需 Task 4 修 `lightrag_repair.py`）。

- [ ] **Step 5: 跑现有 lightrag 测试套件防回归**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python3 -m pytest tests/test_lightrag_repair_unit.py tests/test_lightrag_repair.py tests/test_lightrag_repair_v9_7_scenarios.py tests/test_lightrag_rebuild_from_truth.py -v 2>&1 | tail -50
```

期望：大部分 PASS，但 `test_check_all_missing_derived_file_is_major`（L650-674）会 FAIL——因为它期望"派生缺失报 major"，新逻辑派生缺失不报。这个测试在 Task 5 修复。

- [ ] **Step 6: 提交修复**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/internal/lightrag_integrity.py
git commit -m "fix(lightrag): 派生 kv_store 缺失不再判损坏 + 启用 vdb 数据一致性检查

v2 修复 10 天未解 bug：
1. _DERIVED_FILES 清单移除 3 个 vdb 文件（vdb 由 _check_vdb_missing 负责）
2. _check_derived_missing 返回空列表（派生缺失不是损坏，不修复不重建不写空文件）
3. check_all 启用 _check_vdb_missing 调用（原为死代码，现激活数据一致性检查）

真损坏判定：
- _check_truth_source（3 真相源 corrupt → critical）
- _check_vdb_missing（vdb 与 GraphML 不一致 → major，数据不一致才是真损坏）

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: 修复 `lightrag_repair.py:_check_truth_sources_intact` partial 误判

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py:419-536`

**核心改动**：partial 状态（GraphML has_content + full_docs/cache absent）是脑区/Skills 合法状态，不判为 `intact=False`。只有 corrupt 才判 `intact=False`。

- [ ] **Step 1: 用 Edit 工具重写 `_check_truth_sources_intact` 的 partial 判定**

**第四轮审查发现**：原方案 `old_string` 只覆盖 L509-528（partial 分支），会留下 L530-536（全部有内容分支）成为不可达死代码。**必须扩展 `old_string` 覆盖 L509-536 整块**（partial + 全部有内容两个分支），用单个无条件 return 替换。

`old_string`（L509-536，partial 分支 + 全部有内容分支整块）：

```python
    # partial 状态：部分 has_content + 部分 absent/empty → 损坏
    graphml_has = graphml_state == "has_content"
    full_docs_has = full_docs_state == "has_content"
    cache_has = cache_state == "has_content"
    if graphml_has != full_docs_has or graphml_has != cache_has:
        return {
            "intact": False,
            "graphml": {
                "intact": graphml_has,
                "reason": "partial 状态损坏" if not graphml_has else "有内容",
            },
            "full_docs": {
                "intact": full_docs_has,
                "reason": "partial 状态损坏" if not full_docs_has else "有内容",
            },
            "cache": {
                "intact": cache_has,
                "reason": "partial 状态损坏" if not cache_has else "有内容",
            },
        }

    # 3 文件都有内容且无 corrupt → intact=True
    return {
        "intact": True,
        "graphml": {"intact": True, "reason": "有 node"},
        "full_docs": {"intact": True, "reason": "有 entries"},
        "cache": {"intact": True, "reason": "有 entries"},
    }
```

`new_string`（单个无条件 return，覆盖所有非 corrupt、非全新用户场景）：

```python
    # v2 修复（2026-07-28）：partial 状态不再判为 intact=False
    # 脑区/Skills 注入路径（ainsert_custom_kg）只写 GraphML + 3 vdb + 可选 text_chunks，
    # 不写 full_docs / llm_response_cache。GraphML 有内容但 full_docs/cache absent
    # 是合法中间状态（用户未入库文档），不应阻断修复流程。
    # 真损坏判定已由 _check_truth_source（corrupt）和 _check_vdb_missing（数据不一致）负责。
    # 此分支覆盖所有非 corrupt、非全新用户场景（partial + 全部 has_content），统一返回 intact=True。
    return {
        "intact": True,
        "graphml": {
            "intact": True,
            "reason": "有 node" if graphml_state == "has_content" else "GraphML 不存在或为空（合法）",
        },
        "full_docs": {
            "intact": True,
            "reason": "有 entries" if full_docs_state == "has_content" else "full_docs 不存在或为空（脑区/Skills 路径合法）",
        },
        "cache": {
            "intact": True,
            "reason": "有 entries" if cache_state == "has_content" else "cache 不存在或为空（脑区/Skills 路径合法）",
        },
    }
```

- [ ] **Step 2: 清 pycache + 跑场景 9 测试**

```bash
cd /Users/lilei/tools/ai-bot
find niu_api -name "__pycache__" -exec rm -rf {} + 2>/dev/null
python/bin/python3 -m pytest tests/test_lightrag_integrity_check.py::test_scenario_9_partial_truth_sources_not_unrecoverable -v
```

期望：PASS。

- [ ] **Step 2.5: 第二轮审查 C4 补充——先跑 v9 7 场景测试确认现状**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python3 -m pytest tests/test_lightrag_repair_v9_7_scenarios.py -v 2>&1 | tail -30
```

**关键**：检查 `test_scenario_2_delete_all_9_derived_repair` 现状：
- 如果 PASS：说明现有真实数据 3 真相源都有内容，Task 4 改动不影响（原本就 intact=True）
- 如果 FAIL：说明现有真实数据是脑区路径（full_docs/cache absent），Task 4 改前 partial 误判 unrecoverable。Task 4 改完后应该转 PASS

无论 PASS/FAIL，记录现状，Task 4 改完后重跑确认行为变化。

- [ ] **Step 3: 跑现有 repair 测试套件防回归**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python3 -m pytest tests/test_lightrag_repair.py tests/test_lightrag_repair_v9_7_scenarios.py tests/test_lightrag_rebuild_from_truth.py tests/test_lightrag_repair_unit.py -v 2>&1 | tail -50
```

期望：大部分 PASS。`test_lightrag_repair_v9_7_scenarios.py::test_scenario_4_full_docs_corrupt_unrecoverable` 和 `test_scenario_5_cache_corrupt_unrecoverable` 仍应 PASS（corrupt 仍判 intact=False）。如果 `test_scenario_2_delete_all_9_derived_repair` 等期望 partial 状态 unrecoverable 的测试 FAIL，需 Task 5 修测试断言。

- [ ] **Step 4: 提交修复**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/internal/lightrag_repair.py
git commit -m "fix(lightrag-repair): partial 真相源状态不再判为 unrecoverable

修复用户上次\"尝试修复\"失败的根因：
_check_truth_sources_intact L509-528 把 GraphML has_content + full_docs/cache absent
判为 intact=False（partial 损坏），导致 repair_all 直接返回 _unrecoverable=True。

但脑区/Skills 注入路径下 full_docs/cache 本来就不该存在（只有文档入库才写）。
GraphML 有内容 + full_docs/cache absent 是合法中间状态，不应阻断修复。

v2 改为：只有 corrupt（JSON/XML 解析失败）才判 intact=False，
partial 状态直接判 intact=True。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: 更新现有测试断言

**Files:**
- Modify: `tests/test_lightrag_repair_unit.py`

- [ ] **Step 1: 更新 `test_check_all_missing_derived_file_is_major`（L650-674）**

原断言：9 派生任一 missing → major>=1。
新断言：派生缺失不再报 major；vdb 缺失走 `_check_vdb_missing` 报 major（数据一致性）。

**第二轮审查 D3 关键问题**：新测试名 `test_check_all_vdb_missing_but_graphml_intact_returns_major` 跟 L71 已有同名测试冲突，pytest 收集会覆盖。**必须用不同名字** `test_check_all_kvstore_derived_missing_is_not_major`。

`old_string`：

```python
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
```

`new_string`：

```python
def test_check_all_kvstore_derived_missing_is_not_major(tmp_path, monkeypatch):
    """v2 修复：派生 kv_store 缺失不再报 major（不是损坏）。

    原 test_check_all_missing_derived_file_is_major 期望"9 派生任一 missing → major>=1"，
    v2 改为：派生 kv_store 缺失 → major=0；vdb 缺向量才报 major（数据一致性真损坏）。
    vdb 缺向量的检查由 test_check_all_vdb_missing_but_graphml_intact_returns_major（L71）覆盖。

    此测试 vdb_entities.json 不写（缺 GraphML 对应向量）→ _check_vdb_missing 报 major；
    但派生 kv_store 缺失不应报 major（关键 v2 断言）。
    """
    _write_intact_truth_sources(tmp_path)
    # GraphML 有 node "test-entity"
    # vdb_entities.json 不写（缺该 node 的向量）→ _check_vdb_missing 会报 major
    # 其他派生 kv_store + vdb 写空
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

    # 真相源完好 → critical=0
    assert result["critical_errors"] == 0
    # vdb 缺向量 → major>=1（数据一致性真损坏）
    assert result["major_errors"] >= 1
    assert result["ok"] is False
    # vdb 缺失应记到 checks.vdb_missing（不再记到 derived_missing）
    vdb_errors = result["checks"].get("vdb_missing", {}).get("errors", [])
    assert any(e.get("check") == "vdb_entities_missing" for e in vdb_errors), \
        "vdb 缺向量应记到 vdb_missing check"
    # 关键 v2 断言：派生 kv_store 缺失不应报 major
    derived_errors = result["checks"].get("derived_missing", {}).get("errors", [])
    assert len(derived_errors) == 0, \
        f"派生 kv_store 缺失不应报 major，但 derived_missing 有 errors: {derived_errors}"
```

- [ ] **Step 2: 跑全量 lightrag 测试套件**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python3 -m pytest tests/test_lightrag_repair_unit.py tests/test_lightrag_repair.py tests/test_lightrag_repair_v9_7_scenarios.py tests/test_lightrag_rebuild_from_truth.py tests/test_lightrag_integrity_check.py -v 2>&1 | tail -50
```

期望：全部 PASS。如果仍有 FAIL，逐个看失败用例：
- 如果断言"派生缺失报 major"→ 改断言为 `major_errors == 0`
- 如果断言"partial 真相源 unrecoverable"→ 改断言为 `_unrecoverable=False`

- [ ] **Step 3: 提交测试更新**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_lightrag_repair_unit.py
git commit -m "test: 更新断言适配 v2 检测逻辑（派生缺失不报 major）

test_check_all_missing_derived_file_is_major 改名为
test_check_all_kvstore_derived_missing_is_not_major：
- 原：9 派生任一 missing → major>=1
- 新：派生 kv_store 缺失不应报 major（关键 v2 断言）；
  vdb 缺向量场景由 L71 已有测试覆盖（test_check_all_vdb_missing_but_graphml_intact_returns_major）

第二轮审查 D3 修复：避免与 L71 test_check_all_vdb_missing_but_graphml_intact_returns_major 重名。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 6: 更新模块 docstring（删除"自动重建"错误说法）

**Files:**
- Modify: `niu_api/internal/lightrag_integrity.py:1-8`

- [ ] **Step 1: 更新顶部 docstring**

`old_string`（L1-8）：

```python
"""LightRAG 数据一致性检查（v4：3 真相源不可动 + 9 派生文件 missing）。

检查项：
1. 3 真相源完整可用（GraphML + full_docs + cache）→ critical = unrecoverable
2. 9 派生文件 missing 检测 → major（需 repair 重建）

全新用户合法：3 真相源都不存在时，9 派生文件 missing 也不报错。
"""
```

`new_string`：

```python
"""LightRAG 数据一致性检查 v2（3 真相源 corrupt + vdb 数据一致性）。

检查项：
1. 3 真相源 corrupt 检测（GraphML XML 解析失败 / full_docs/cache JSON 解析失败）→ critical
2. vdb 与 GraphML 数据一致性检测（node/edge 在 vdb 有对应向量）→ major = 真损坏

派生 kv_store 文件缺失不是损坏：脑区/Skills 注入路径只写 GraphML + 3 vdb +
可选 text_chunks，其他派生 kv_store 由 LightRAG 内部 JsonKVStorage.initialize
按需 lazy 加载为空 dict（load_json() or {}），运行时按需 upsert。
本方案不主动调用任何重建，不写空文件。
"""
```

- [ ] **Step 2: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/internal/lightrag_integrity.py
git commit -m "docs(lightrag): 同步 integrity 模块 docstring v2

删除\"自动重建派生\"错误说法（用户明确反对重建）。
改为：派生缺失时 LightRAG 内部按需 lazy 加载为空 dict，本方案不主动调用任何重建。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 7: 真实 E2E 验证（用户当前现场）

**Files:**
- 无代码改动，仅运行验证

- [ ] **Step 1: 确认用户现场未动 + 跑 check_all 验证 vdb 一致性**

```bash
ls -la /Users/lilei/.niu/lightrag_storage/
```

期望：仍是 5 个文件（GraphML + text_chunks + 3 vdb），full_docs/cache/5 派生 kv_store 缺失。

**第二轮审查 B2 关键补充**：用 Python 实际跑 `check_all()` 确认用户现场 vdb 与 GraphML 一致：

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python3 -c "
from niu_api.internal.lightrag_integrity import check_all
result = check_all()
print(f'ok={result[\"ok\"]}, critical={result[\"critical_errors\"]}, major={result[\"major_errors\"]}, minor={result[\"minor_errors\"]}')
if result['errors']:
    for e in result['errors']:
        print(f'  - {e.get(\"check\")}: {e.get(\"msg\", \"\")}')
"
```

期望输出：`ok=True, critical=0, major=0, minor=0`，无 errors。

**如果 major>0**：用户现场 vdb 与 GraphML 不一致（D4 风险），v2 启用 `_check_vdb_missing` 后会触发真损坏弹窗。这是设计使然——vdb 缺向量就是真损坏，需要修复。此时不要继续 Task 7 Step 3，改为分析 vdb 缺失的具体 node/edge，决定是否需要 repair。

- [ ] **Step 2: 杀干净所有 niu 进程**

```bash
ps aux | grep -E "niu|python.*niu_api" | grep -v grep
# 用 kill -TERM 优雅退出（禁止 pkill -f niu，会损坏 vdb 文件）
# 找到 PID 后逐个 kill -TERM
```

- [ ] **Step 3: 启动程序验证不再弹修复窗**

```bash
cd /Users/lilei/tools/ai-bot
./niu
```

期望：
- 启动日志不再出现 `LightRAG Phase 1 检测: {'check_ok': False, 'need_repair': True, ...}`
- 不再出现 `[LightRAG] 检测到损坏，等待用户在 rfd 弹窗选择`
- 出现 `LightRAG Phase 1 检测: {'check_ok': True, 'need_repair': False}`
- 主窗口正常进入助手界面

- [ ] **Step 4: 验证 ChatQueue 不再被 pause**

启动日志不应出现 `[LightRAG] ChatQueue paused due to LightRAG corruption`。

- [ ] **Step 5: 验证脑区数据可正常使用**

在助手界面问"我的脑区有哪些"，期望能正常返回脑区列表（GraphML 数据可读）。

- [ ] **Step 6: 退出程序，杀干净进程**

```bash
ps aux | grep -E "niu|python.*niu_api" | grep -v grep
# 如有残留，kill -TERM
```

---

### Task 8: 故障注入验证（vdb 缺向量仍能阻断）

**Files:**
- 临时改动 `/Users/lilei/.niu/lightrag_storage/`，验证后恢复

- [ ] **Step 1: 备份用户现场**

```bash
cp -r /Users/lilei/.niu/lightrag_storage /tmp/lightrag_storage_backup_$(date +%s)
```

- [ ] **Step 2: 删 vdb_entities.json 模拟真损坏**

```bash
rm /Users/lilei/.niu/lightrag_storage/vdb_entities.json
```

- [ ] **Step 3: 启动程序，验证弹修复窗**

```bash
cd /Users/lilei/tools/ai-bot
./niu
```

期望：
- 启动日志出现 `major_errors: 1`（vdb_entities_missing）
- 弹出修复弹窗
- 用户选"退出"程序退出

- [ ] **Step 4: 恢复用户现场**

```bash
rm -rf /Users/lilei/.niu/lightrag_storage
cp -r /tmp/lightrag_storage_backup_* /Users/lilei/.niu/lightrag_storage
ls -la /Users/lilei/.niu/lightrag_storage/
```

- [ ] **Step 5: 提交最终验收报告（无代码改动，跳过 commit）**

---

## 5. Self-Review

### Spec 覆盖
- 用户原话"派生缺失不是损坏" → Task 3 `_check_derived_missing` 返回空列表 ✓
- 用户原话"不重建空文件" → Task 6 docstring 明确"不主动调用任何重建，不写空文件" ✓
- 用户原话"如何检查判断它是真损坏，而不是只靠文件缺失判断" → Task 3 启用 `_check_vdb_missing` 数据一致性检查 ✓
- 第一轮审查 P1（partial 误判） → Task 4 修 `_check_truth_sources_intact` ✓
- 第一轮审查 P2（vdb 死代码） → Task 3 Step 3 `check_all` 启用 `_check_vdb_missing` ✓
- 第一轮审查 P3（"自动重建"错误说法） → Task 6 删除 ✓
- 交付条件"连续两轮审查无 bug" → Task 9（下一轮审查）✓

### Placeholder 扫描
- 无 "TBD"/"TODO"/"implement later" ✓
- 每个步骤都有完整代码块或具体命令 ✓
- 测试代码完整可执行 ✓

### 类型一致性
- `check_all` 返回结构变化：`checks` dict 新增 `vdb_missing` 项（不影响调用方，调用方只读顶层 `ok`/`critical_errors`/`major_errors`）✓
- `_check_derived_missing` 签名不变（`list[dict[str, Any]]`）✓
- `_check_truth_sources_intact` 返回结构不变（`dict[str, Any]`）✓
- 调用方 `run_resilience_phase1` / `get_lightrag` / `run_repair_on_user_request` 不需要改动 ✓

### 风险点
- **风险 1**：`test_lightrag_repair_v9_7_scenarios.py::test_scenario_2_delete_all_9_derived_repair` 可能因 `_check_truth_sources_intact` 不再判 partial 而改变行为。Task 4 Step 3 会跑这个测试，如果 FAIL 需要分析具体行为。
- **风险 2**：`_check_vdb_missing` 启用后，用户现场的 vdb 是否与 GraphML 一致需 Task 7 验证。若 vdb 缺向量，会触发真损坏弹窗（这是正确行为）。
- **风险 3**：Task 8 故障注入后必须恢复用户现场，遵守"派 subagent 禁止跑探针脚本操作真实 ~/.niu 数据"——本计划是主对话亲自操作，但仍要备份+恢复。

---

## 6. 执行选择

**Plan complete and saved to `docs/superpowers/plans/2026-07-28-lightrag-integrity-check-fix.md` (v2). Two execution options:**

**1. Subagent-Driven (recommended)** - 我派 fresh subagent 跑 Task 2/3/4/5/6，每个 Task 完成后我审查，迭代快

**2. Inline Execution** - 在当前会话直接执行，分批 checkpoint 审查

**Which approach?**
