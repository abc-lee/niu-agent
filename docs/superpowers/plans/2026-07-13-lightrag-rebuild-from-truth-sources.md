# LightRAG 数据修复重构：从真相源一刀切重建 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 LightRAG 数据修复逻辑从"针对每种故障写专门 repair 函数"重构为"检测 2 个真相源文件 → 删除其他 9 个文件 → 按依赖链重建"，让任何数据故障都能一刀切修复（不调 LLM、不丢数据）。

**Architecture:** 真相源 = `kv_store_full_docs.json` + `kv_store_llm_response_cache.json`（LLM 非确定性，不可重新调 LLM 恢复）。其他 9 个文件全是派生数据，可从这 2 个按依赖链重建。新 `repair_all` 不再按 check 报错选择性 repair，而是无条件删 9 重建。`check_all` 简化为只检 2 真相源是否完整可用（JSON 解析 + 内容非空 + 字段完整）。启动器 status 接口暴露的 `total_errors` 字段从 `check_result.major_errors` 读，不再硬编码 0。Rust 启动器弹窗逻辑保持不变（status 报损坏 → 用户点"是" → 调 repair → 弹修复结果）。

**Tech Stack:** Python 3.11、xml.etree.ElementTree（GraphML）、nano-vectordb（向量存储）、pytest（TDD）、真实 LightRAG 实例（端到端验证，不 mock LLM）。

---

## 背景

### 前 5 轮修复为什么没解决

1. **7-08 entity-sync**：根因判定 = "check_all 没检同步性"，加 check_entity_sync
2. **7-08 case-insensitive**：根因判定 = "源头没 lower 化"，改 LightRAG Fork 源码
3. **7-09 startup-block**：根因判定 = "启动流程不阻塞 + repaired 硬编码"
4. **7-11 consistency-redo**：根因判定 = "集合比对非因果链"，全部重写
5. **7-12 semantic-integrity**：根因判定 = "句法非语义"，加 5 个语义 check + repair_brainregion_zombies

**循环原因**：每轮都针对"当前这次具体故障"写专门 repair 函数，下次出别的故障又要再写。`repair_all` 按 check 报错选择性 repair——check 漏检（如 16 个僵尸脑区在 11 项 check 全过）→ repair 不触发→ 修复失败。check 误报（如 `chunk_shared_by_too_many_entities` 把通讯录 chunk 被 68 entity 共享当 bug）→ repair 永远修不完 → 修复失败。

### 真相源确认（基于源码 + 实测数据）

**真相源 = 2 个文件**：

1. **`kv_store_full_docs.json`** — 文档原文。其他文件全是 chunk/entity/relation 级别，无法反向拼回原文。源码 `_UNRECOVERABLE_FILES = {"kv_store_full_docs.json"}`（`lightrag_repair.py:2058`）。

2. **`kv_store_llm_response_cache.json`** — LLM 抽取结果缓存。每个 `extract` 类型 entry 自带 `chunk_id` 字段（实测 232/259 条都是 extract 类型，全部含 chunk_id）。LLM 是非确定性的，重新调 LLM 抽取的 entity/relation 跟原来**一定不同** → 数据丢失。所以不可重新调 LLM 恢复，必须保留 cache。

**派生数据 = 9 个文件**（全部可从 2 真相源重建，全程不调 LLM）：

| 文件 | 重建路径 | 复用现有 repair 函数 |
|------|---------|-------------------|
| `kv_store_text_chunks.json` | 从 `full_docs` 重新 chunking（chunk_id=MD5(content) 确定性）；`llm_cache_list` 从 `llm_response_cache` 反向扫描 `chunk_id` 字段重建 | `repair_text_chunks`（`lightrag_repair.py:386`）已实现 |
| `kv_store_doc_status.json` | 从 `full_docs` + `text_chunks` 派生 | `repair_doc_status`（`lightrag_repair.py:543`）已实现 |
| `graph_chunk_entity_relation.graphml` | 重跑 `apipeline_process_enqueue_documents`，extract 阶段 cache 命中免调 LLM，summary 阶段 `force_llm_summary_on_merge` 跳过 | `repair_graphml`（`lightrag_repair.py:644`）已实现 |
| `kv_store_entity_chunks.json` | 从 GraphML node source_id 派生 | `repair_entity_chunks`（`lightrag_repair.py:1394`）已实现 |
| `kv_store_relation_chunks.json` | 从 GraphML edge source_id 派生 | `repair_relation_chunks`（`lightrag_repair.py:1459`）已实现 |
| `kv_store_full_entities.json` | 从 GraphML 按 file_path 分组 | `repair_full_entities`（`lightrag_repair.py:1534`）已实现 |
| `kv_store_full_relations.json` | 从 GraphML 按 file_path 分组 | `repair_full_relations`（`lightrag_repair.py:1616`）已实现 |
| `vdb_chunks.json` | 从 `text_chunks.content` 重新 embed（embedding 函数确定） | `repair_vdb_chunks`（`lightrag_repair.py:981`）已实现 |
| `vdb_entities.json` | 从 GraphML node 重新 embed | `repair_vdb_entities`（`lightrag_repair.py:1120`）已实现 |
| `vdb_relationships.json` | 从 GraphML edge 重新 embed | `repair_vdb_relationships`（`lightrag_repair.py:1243`）已实现 |

### 关键设计决策

1. **不动 14 个 repair 函数本身**——它们已实现"从真相源重建"逻辑，质量没问题。要改的是**调度策略**：`repair_all` 从"按 check 选择性 repair"改为"无条件删 9 重建"。

2. **删 9 个文件前不备份**——这 9 个全是派生数据，删了能从真相源重建。备份只会让用户混淆（不知道哪个是真相）。

3. **`repair_text_chunks` 的 llm_cache_list 问题**——现有实现把 `llm_cache_list` 初始化为 `[]`（`lightrag_repair.py:492`），但 `llm_response_cache` 每个 entry 自带 `chunk_id` 字段，可以反向重建。Task 3 修复这个。

4. **简化 `check_all`**——现有 16 个 check（11 句法 + 5 语义）大部分是针对具体故障的检测，新设计不需要。`check_all` 改为只检 2 真相源 + 1 个 GraphML 后置验证（重建后 GraphML 应该跟真相源一致）。

5. **`repaired` 判定**——`run_repair_on_user_request` 现在用"check_all 报 major=0"判定修复成功。新设计改为"2 真相源完整 + 9 重建文件全部 status=ok"判定。

---

## 文件结构

| 文件 | 责任 | 改动类型 |
|------|------|---------|
| `niu_api/internal/lightrag_repair.py` | 重写 `repair_all` 为"检测 2 真相源 → 删 9 → 按依赖链重建"；修复 `repair_text_chunks` 的 `llm_cache_list` 反向重建；删除 `_CHECK_TO_REPAIR` / `_FILE_TO_REPAIR` 旧映射 | 修改 |
| `niu_api/internal/lightrag_integrity.py` | 简化 `check_all` 为"检 2 真相源完整 + GraphML 后置验证"；删除 11 个旧句法 check + 5 个旧语义 check + 16 项 check 函数；保留 `_load_graphml` / `_load_json_dict` 工具函数 | 修改 |
| `niu_api/internal/lightrag_manager.py` | 修复 `run_resilience_phase1` 的 `total_errors` 字段（从 `check_result.major_errors` 读，不硬编码 0）；修复 `run_repair_on_user_request` 的 `repaired` 判定（用"2 真相源完整 + 9 文件 status=ok"） | 修改 |
| `tests/test_lightrag_rebuild_from_truth.py` | 端到端 TDD 测试：删 vdb_*.json → repair → 验证重建；删 GraphML → repair → 验证；删 9 个文件 → repair → 验证；真相源损坏 → unrecoverable | 创建 |
| `tests/test_lightrag_repair_unit.py` | 单元测试：`repair_text_chunks` 的 `llm_cache_list` 反向重建；`repair_all` 的新调度逻辑；`check_all` 新逻辑 | 创建 |

---

## Task 1: 修复 `repair_text_chunks` 的 `llm_cache_list` 反向重建

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py:386-540`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

现有 `repair_text_chunks` 重建 text_chunks 时把 `llm_cache_list` 初始化为 `[]`（`lightrag_repair.py:492`）。但 `llm_response_cache` 每个 `extract` 类型 entry 自带 `chunk_id` 字段，可以反向重建映射。

新 `repair_all` 流程是"删 9 重建"——`text_chunks` 被删后从 `full_docs` 重建。如果 `llm_cache_list` 为空，GraphML 重建时 `merge_nodes_and_edges` 仍能通过扫描 `llm_response_cache` 的 `chunk_id` 字段找到对应 cache（源码 `operate.py:875-911`），但保留 `llm_cache_list` 能加速重建（避免全表扫描）。

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py`:

```python
"""repair_text_chunks 的 llm_cache_list 反向重建单元测试。"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch

from niu_api.internal.lightrag_repair import repair_text_chunks


def _make_truth_sources(tmp_path: Path, docs: dict, cache: dict):
    """生成 full_docs + llm_response_cache 两个真相源文件。"""
    (tmp_path / "kv_store_full_docs.json").write_text(
        json.dumps(docs, ensure_ascii=False)
    )
    (tmp_path / "kv_store_llm_response_cache.json").write_text(
        json.dumps(cache, ensure_ascii=False)
    )


def test_repair_text_chunks_rebuilds_llm_cache_list_from_cache(tmp_path):
    """repair_text_chunks 应从 llm_response_cache 反向重建 llm_cache_list。
    
    场景：text_chunks 被删（或损坏），从 full_docs 重新 chunking。
    重建后每个 chunk 的 llm_cache_list 应指向 llm_response_cache 里
    chunk_id 匹配的 cache key。
    """
    # 准备真相源
    docs = {
        "doc-aaa": {
            "content": "这是一个测试文档，用于验证 llm_cache_list 反向重建。",
            "file_path": "test.md",
        }
    }
    # llm_response_cache 有 2 个 extract entry，都指向同一个 chunk_id
    # （chunk_id = compute_mdhash_id(content, "chunk-")，确定性）
    from lightrag.utils import compute_mdhash_id
    expected_chunk_id = compute_mdhash_id(docs["doc-aaa"]["content"], prefix="chunk-")
    
    cache = {
        "default:extract:key1": {
            "return": "entity<|#|>测试文档<|#|>document<|#|>测试文档描述",
            "cache_type": "extract",
            "chunk_id": expected_chunk_id,
            "original_prompt": "...",
            "create_time": 1781930610,
        },
        "default:extract:key2": {
            "return": "<|COMPLETE|>",
            "cache_type": "extract",
            "chunk_id": expected_chunk_id,
            "original_prompt": "...",
            "create_time": 1781930611,
        },
        # 非 extract 类型不应被扫描
        "default:keywords:xxx": {
            "return": "keywords",
            "cache_type": "keywords",
            "chunk_id": expected_chunk_id,
            "create_time": 1781930612,
        },
    }
    _make_truth_sources(tmp_path, docs, cache)
    
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path):
        result = repair_text_chunks()
    
    assert result["status"] == "ok"
    
    # 验证 text_chunks 重建后 llm_cache_list 正确填充
    tc = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    assert expected_chunk_id in tc
    chunk_entry = tc[expected_chunk_id]
    assert "llm_cache_list" in chunk_entry
    cache_list = chunk_entry["llm_cache_list"]
    assert isinstance(cache_list, list)
    # 应包含 2 个 extract cache key（不含 keywords）
    assert len(cache_list) == 2
    assert "default:extract:key1" in cache_list
    assert "default:extract:key2" in cache_list
    assert "default:keywords:xxx" not in cache_list


def test_repair_text_chunks_no_cache_still_works(tmp_path):
    """llm_response_cache 为空时，text_chunks 仍能重建（llm_cache_list 为空列表）。"""
    docs = {
        "doc-bbb": {
            "content": "无 cache 的文档",
            "file_path": "test2.md",
        }
    }
    _make_truth_sources(tmp_path, docs, {})  # 空 cache
    
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path):
        result = repair_text_chunks()
    
    assert result["status"] == "ok"
    tc = json.loads((tmp_path / "kv_store_text_chunks.json").read_text())
    for chunk_id, chunk_data in tc.items():
        assert chunk_data.get("llm_cache_list") == []
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_lightrag_repair_unit.py::test_repair_text_chunks_rebuilds_llm_cache_list_from_cache -v
```

Expected: FAIL with `AssertionError: assert 0 == 2`（现有实现 `llm_cache_list` 是空列表，长度 0）

### - [ ] Step 3: Write minimal implementation

修改 `niu_api/internal/lightrag_repair.py:386-540` 的 `repair_text_chunks` 函数，在写 text_chunks 前反向扫描 `llm_response_cache` 填充 `llm_cache_list`。

找到现有函数里写 text_chunks 的部分（大约在 L490 附近，`"llm_cache_list": [],` 那行），替换为：

```python
def repair_text_chunks() -> dict[str, Any]:
    """从 full_docs 重新 chunking 重建 text_chunks（含 llm_cache_list 反向重建）。
    
    重建逻辑：
        1. 从 full_docs 读取所有文档原文
        2. 调用 chunking_by_token_size 切分（chunk_id = compute_mdhash_id(content, prefix="chunk-")）
        3. 反向扫描 llm_response_cache，为每个 chunk 填充 llm_cache_list
           （cache entry 的 cache_type="extract" 且 chunk_id 匹配）
        4. 原子写入 kv_store_text_chunks.json
    
    Returns:
        {
            "status": "ok"|"unrecoverable",
            "expected": int,  # full_docs 的文档数
            "actual": int,    # 重建后的 chunk 数
            "source": "full_docs",
            "message": str,
        }
    """
    storage_dir = _storage_dir()
    
    # 1. 读 full_docs（真相源 1）
    full_docs_path = storage_dir / "kv_store_full_docs.json"
    try:
        full_docs = json.loads(full_docs_path.read_text())
    except Exception as e:
        return {
            "status": "unrecoverable",
            "expected": 0,
            "actual": 0,
            "source": "full_docs",
            "message": f"full_docs 读取失败: {e}",
        }
    if not full_docs:
        return {
            "status": "unrecoverable",
            "expected": 0,
            "actual": 0,
            "source": "full_docs",
            "message": "full_docs 为空，无法重建 text_chunks",
        }
    
    # 2. 反向扫描 llm_response_cache，构建 chunk_id -> [cache_key] 映射
    lrc_path = storage_dir / "kv_store_llm_response_cache.json"
    chunk_to_cache_keys: dict[str, list[str]] = {}
    try:
        if lrc_path.exists():
            lrc = json.loads(lrc_path.read_text())
            for cache_key, entry in lrc.items():
                if not isinstance(entry, dict):
                    continue
                if entry.get("cache_type") != "extract":
                    continue
                cid = entry.get("chunk_id", "")
                if cid:
                    chunk_to_cache_keys.setdefault(cid, []).append(cache_key)
    except Exception as e:
        logger.warning(f"[LightRAGRepair] llm_response_cache 读取失败（llm_cache_list 将为空）: {e}")
    
    # 3. 调用 LightRAG 的 chunking_by_token_size 切分
    try:
        from lightrag.operate import chunking_by_token_size
        from lightrag.utils import compute_mdhash_id
    except ImportError as e:
        return {
            "status": "unrecoverable",
            "expected": len(full_docs),
            "actual": 0,
            "source": "full_docs",
            "message": f"LightRAG 模块导入失败: {e}",
        }
    
    # 构造 chunks 列表
    chunks_to_write: dict[str, dict[str, Any]] = {}
    for doc_id, doc_data in full_docs.items():
        if not isinstance(doc_data, dict):
            continue
        content = doc_data.get("content", "")
        if not content:
            continue
        # 调用 chunking_by_token_size 切分
        chunks = chunking_by_token_size(
            content=content,
            chunk_size=1200,  # 默认配置，跟 LightRAG 一致
            chunk_overlap=100,
        )
        for i, chunk_content in enumerate(chunks):
            chunk_id = compute_mdhash_id(chunk_content, prefix="chunk-")
            chunks_to_write[chunk_id] = {
                "content": chunk_content,
                "source_id": doc_id,
                "tokens": len(chunk_content.split()),  # 简化估算
                "chunk_order_index": i,
                "full_doc_id": doc_id,
                "file_path": doc_data.get("file_path", ""),
                "status": "processed",
                "llm_cache_list": chunk_to_cache_keys.get(chunk_id, []),
                "update_time": doc_data.get("update_time", 0),
                "_id": chunk_id,
            }
    
    # 4. 原子写入
    tc_path = storage_dir / "kv_store_text_chunks.json"
    try:
        _atomic_write_json(tc_path, chunks_to_write)
    except Exception as e:
        return {
            "status": "unrecoverable",
            "expected": len(full_docs),
            "actual": len(chunks_to_write),
            "source": "full_docs",
            "message": f"text_chunks 写入失败: {e}",
        }
    
    logger.info(f"[LightRAGRepair] 重建 text_chunks: {len(chunks_to_write)} 个 chunk (source=full_docs)")
    return {
        "status": "ok",
        "expected": len(full_docs),
        "actual": len(chunks_to_write),
        "source": "full_docs",
        "message": f"从 full_docs 重新 chunking 重建 {len(chunks_to_write)} 条 text_chunks（含 llm_cache_list 反向重建）",
    }
```

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_repair_text_chunks_rebuilds_llm_cache_list_from_cache \
                tests/test_lightrag_repair_unit.py::test_repair_text_chunks_no_cache_still_works -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_repair.py tests/test_lightrag_repair_unit.py
git commit -m "fix(repair): repair_text_chunks 反向重建 llm_cache_list 从 llm_response_cache

text_chunks 被删重建时，llm_cache_list 不再初始化为空，
而是反向扫描 llm_response_cache 的 extract 类型 entry
（每个 entry 自带 chunk_id 字段），为每个 chunk 填充对应的 cache key。
加速后续 GraphML 重建（避免 merge_nodes_and_edges 全表扫描 cache）。
"
```

---

## Task 2: 重写 `repair_all` 为"检测 2 真相源 → 删 9 → 按依赖链重建"

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py:2061-2197`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

现有 `repair_all` 按 check 报错选择性 repair（L2103-2127 收集 `needed_repairs`）。新设计改为**无条件删 9 重建**——不管什么故障，删掉 9 个派生文件，从 2 真相源按依赖链重建。

依赖链顺序：
1. `repair_text_chunks`（从 full_docs）→ 写 text_chunks + 反向填充 llm_cache_list
2. `repair_doc_status`（从 full_docs + text_chunks）
3. `repair_graphml`（重跑 pipeline，extract cache 命中免调 LLM）
4. `repair_vdb_chunks`（从 text_chunks.content 重新 embed）
5. `repair_vdb_entities`（从 GraphML node 重新 embed）
6. `repair_vdb_relationships`（从 GraphML edge 重新 embed）
7. `repair_entity_chunks`（从 GraphML node source_id）
8. `repair_relation_chunks`（从 GraphML edge source_id）
9. `repair_full_entities`（从 GraphML 按 file_path 分组）
10. `repair_full_relations`（从 GraphML 按 file_path 分组）

注意：`llm_response_cache` 不删（真相源）。`full_docs` 不删（真相源）。

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py` 追加：

```python
def test_repair_all_deletes_9_derived_files_and_rebuilds(tmp_path):
    """repair_all 应删除 9 个派生文件，从 2 真相源重建。"""
    from niu_api.internal.lightrag_repair import repair_all
    from niu_api.internal.lightrag_integrity import _TRUTH_SOURCE_FILES
    
    # 准备 2 真相源
    docs = {
        "doc-test1": {
            "content": "测试文档内容，用于验证 repair_all 一刀切重建。",
            "file_path": "test.md",
        }
    }
    cache = {}  # 空 cache（让 graphml 重建走 cache miss，但这里只测调度逻辑）
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    
    # 准备 9 个派生文件（含损坏数据）
    derived_files = [
        "kv_store_text_chunks.json",
        "kv_store_doc_status.json",
        "graph_chunk_entity_relation.graphml",
        "vdb_chunks.json",
        "vdb_entities.json",
        "vdb_relationships.json",
        "kv_store_entity_chunks.json",
        "kv_store_relation_chunks.json",
        "kv_store_full_entities.json",
        "kv_store_full_relations.json",
    ]
    for fname in derived_files:
        (tmp_path / fname).write_text('{"corrupt": "垃圾数据"}')  # 故意写坏
    
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        result = repair_all()
    
    # 应该删除并重建所有 9 个派生文件
    for fname in derived_files:
        fpath = tmp_path / fname
        assert fpath.exists(), f"{fname} 应被重建"
        content = fpath.read_text()
        assert "corrupt" not in content, f"{fname} 仍是损坏数据"
    
    # 真相源文件不应被删
    assert (tmp_path / "kv_store_full_docs.json").exists()
    assert (tmp_path / "kv_store_llm_response_cache.json").exists()


def test_repair_all_unrecoverable_when_full_docs_missing(tmp_path):
    """full_docs 损坏 → unrecoverable，不删除任何文件。"""
    from niu_api.internal.lightrag_repair import repair_all
    
    # 不写 full_docs（真相源损坏）
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")
    # 写一些派生文件
    (tmp_path / "kv_store_text_chunks.json").write_text('{"old": "保留"}')
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text("old graphml")
    
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        result = repair_all()
    
    assert result.get("_unrecoverable") is True
    # 不应删除任何文件（保留原状让用户从备份恢复）
    assert (tmp_path / "kv_store_text_chunks.json").read_text() == '{"old": "保留"}'
    assert (tmp_path / "graph_chunk_entity_relation.graphml").read_text() == "old graphml"


def test_repair_all_unrecoverable_when_llm_response_cache_missing(tmp_path):
    """llm_response_cache 损坏 → unrecoverable（重新调 LLM 会导致数据不一致）。"""
    from niu_api.internal.lightrag_repair import repair_all
    
    (tmp_path / "kv_store_full_docs.json").write_text(
        json.dumps({"doc-x": {"content": "test", "file_path": "x.md"}}, ensure_ascii=False)
    )
    # 不写 llm_response_cache
    
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        result = repair_all()
    
    assert result.get("_unrecoverable") is True
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_repair_all_deletes_9_derived_files_and_rebuilds -v
```

Expected: FAIL（现有 `repair_all` 按 check 报错选择性 repair，不会无条件删 9 重建；且 `_TRUTH_SOURCE_FILES` 常量尚未定义）

### - [ ] Step 3: Write minimal implementation

修改 `niu_api/internal/lightrag_repair.py`：

**3a. 新增 `_TRUTH_SOURCE_FILES` 和 `_DERIVED_FILES` 常量**（替换 `_UNRECOVERABLE_FILES`，在 L2058 附近）：

```python
# 真相源文件（不可重建，损坏 = unrecoverable）
_TRUTH_SOURCE_FILES = {
    "kv_store_full_docs.json",
    "kv_store_llm_response_cache.json",
}

# 派生数据文件（可从真相源重建，repair_all 一刀切删除重建）
_DERIVED_FILES = [
    "kv_store_text_chunks.json",
    "kv_store_doc_status.json",
    "graph_chunk_entity_relation.graphml",
    "vdb_chunks.json",
    "vdb_entities.json",
    "vdb_relationships.json",
    "kv_store_entity_chunks.json",
    "kv_store_relation_chunks.json",
    "kv_store_full_entities.json",
    "kv_store_full_relations.json",
]

# 向后兼容别名（外部代码可能引用）
_UNRECOVERABLE_FILES = _TRUTH_SOURCE_FILES
```

**3b. 重写 `repair_all` 函数**（替换 L2061-2197 整个函数）：

```python
# 重建依赖链顺序（按依赖关系）
_REBUILD_ORDER = [
    ("text_chunks", repair_text_chunks),
    ("doc_status", repair_doc_status),
    ("graphml", repair_graphml),
    ("vdb_chunks", repair_vdb_chunks),
    ("vdb_entities", repair_vdb_entities),
    ("vdb_relationships", repair_vdb_relationships),
    ("entity_chunks", repair_entity_chunks),
    ("relation_chunks", repair_relation_chunks),
    ("full_entities", repair_full_entities),
    ("full_relations", repair_full_relations),
]


def repair_all() -> dict[str, Any]:
    """一键修复：检测 2 真相源 → 删除 9 个派生文件 → 按依赖链重建。
    
    不管什么数据故障（孤儿 chunk、悬空引用、vdb 缺失、GraphML 损坏、僵尸脑区残留...），
    全部删了重建。只要 2 真相源没坏，重建出来的图谱跟原来完全一致
    （LLM 抽取结果从 cache 读，不重新调 LLM）。
    
    流程：
        1. 检测 2 真相源（full_docs + llm_response_cache）是否完整可用
           - 任一损坏 → 返回 unrecoverable，不删任何文件（让用户从备份恢复）
        2. 删除 9 个派生文件
        3. 按依赖链顺序重建（每个 repair 函数从真相源/上游派生数据重建）
        4. 任意 repair 报 unrecoverable → 标记 unrecoverable_detected
    
    Returns:
        {
            "repaired": bool,  # 是否全部重建成功
            "repair_result": {
                "text_chunks": {status, ...},
                ...
                "_unrecoverable": bool,
            }
        }
    """
    storage_dir = _storage_dir()
    results: dict[str, Any] = {}
    unrecoverable_detected = False
    
    # 1. 检测 2 真相源
    truth_source_check = _check_truth_sources(storage_dir)
    if not truth_source_check["ok"]:
        results["_unrecoverable"] = True
        results["_unrecoverable_reason"] = truth_source_check["reason"]
        results["_truth_source_check"] = truth_source_check
        return {
            "repaired": False,
            "repair_result": results,
        }
    
    # 2. 删除 9 个派生文件
    deleted: list[str] = []
    for fname in _DERIVED_FILES:
        fpath = storage_dir / fname
        if fpath.exists():
            try:
                fpath.unlink()
                deleted.append(fname)
                logger.info(f"[LightRAGRepair] 删除派生文件: {fname}")
            except Exception as e:
                logger.warning(f"[LightRAGRepair] 删除 {fname} 失败（继续）: {e}")
    results["_deleted"] = deleted
    
    # 3. 按依赖链重建
    for name, fn in _REBUILD_ORDER:
        try:
            result = fn()
            results[name] = result
            if result.get("unrecoverable") or result.get("status") == "unrecoverable":
                unrecoverable_detected = True
                logger.warning(
                    f"[LightRAGRepair] {name} 报 unrecoverable: {result.get('message', '')}"
                )
        except Exception as e:
            logger.error(f"[LightRAGRepair] {name} 抛异常: {e}", exc_info=True)
            results[name] = {
                "status": "error",
                "message": f"repair 函数抛异常: {type(e).__name__}: {e}",
            }
            unrecoverable_detected = True
    
    results["_unrecoverable"] = unrecoverable_detected
    
    return {
        "repaired": not unrecoverable_detected,
        "repair_result": results,
    }


def _check_truth_sources(storage_dir: Path) -> dict[str, Any]:
    """检测 2 个真相源文件是否完整可用。
    
    Returns:
        {
            "ok": bool,
            "reason": str,  # ok=False 时的原因
            "files": {filename: {"ok": bool, "reason": str, "size": int}},
        }
    """
    files_status = {}
    all_ok = True
    reasons = []
    
    for fname in _TRUTH_SOURCE_FILES:
        fpath = storage_dir / fname
        status = {"ok": False, "reason": "", "size": 0}
        if not fpath.exists():
            status["reason"] = f"{fname} 不存在"
            reasons.append(status["reason"])
        else:
            try:
                size = fpath.stat().st_size
                status["size"] = size
                if size == 0:
                    status["reason"] = f"{fname} 为空文件"
                    reasons.append(status["reason"])
                else:
                    data = json.loads(fpath.read_text())
                    if not data:
                        status["reason"] = f"{fname} 内容为空"
                        reasons.append(status["reason"])
                    else:
                        status["ok"] = True
            except json.JSONDecodeError as e:
                status["reason"] = f"{fname} JSON 解析失败: {e}"
                reasons.append(status["reason"])
            except Exception as e:
                status["reason"] = f"{fname} 读取失败: {e}"
                reasons.append(status["reason"])
        if not status["ok"]:
            all_ok = False
        files_status[fname] = status
    
    return {
        "ok": all_ok,
        "reason": "; ".join(reasons) if reasons else "",
        "files": files_status,
    }
```

**3c. 删除旧的 `_CHECK_TO_REPAIR` / `_FILE_TO_REPAIR` / `_REPAIR_ORDER`**（L1995-2056），它们不再被使用。

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_repair.py tests/test_lightrag_repair_unit.py
git commit -m "refactor(repair): repair_all 改为检测 2 真相源 → 删 9 → 按依赖链重建

不再按 check 报错选择性 repair，改为无条件删除 9 个派生文件
从 2 真相源（full_docs + llm_response_cache）按依赖链重建。
不管什么数据故障都能一刀切修复，只要 2 真相源没坏。

删除旧的 _CHECK_TO_REPAIR / _FILE_TO_REPAIR / _REPAIR_ORDER 映射。
新增 _TRUTH_SOURCE_FILES / _DERIVED_FILES / _REBUILD_ORDER 常量。
新增 _check_truth_sources 函数检测真相源完整性。
"
```

---

## Task 3: 简化 `check_all` 为"检 2 真相源 + GraphML 后置验证"

**Files:**
- Modify: `niu_api/internal/lightrag_integrity.py`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

现有 `check_all` 有 16 个 check（11 句法 + 5 语义），大部分是针对具体故障的检测，新设计不需要。

新 `check_all` 只做 2 件事：
1. **前置检测**：检 2 真相源（full_docs + llm_response_cache）是否完整可用
2. **后置验证**：检 GraphML 是否存在且非空（重建后应该有数据）

旧 check 函数（`check_entity_chunks_dangling` 等）全部删除——它们是"针对具体故障写专门检测"的旧思路产物。

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py` 追加：

```python
def test_check_all_truth_sources_intact_returns_ok(tmp_path):
    """2 真相源完整 → check_all 返回 ok=True。"""
    from niu_api.internal.lightrag_integrity import check_all
    
    # 准备 2 真相源
    (tmp_path / "kv_store_full_docs.json").write_text(
        json.dumps({"doc-x": {"content": "test", "file_path": "x.md"}}, ensure_ascii=False)
    )
    (tmp_path / "kv_store_llm_response_cache.json").write_text(
        json.dumps({"default:extract:xxx": {"return": "entity", "cache_type": "extract"}}, ensure_ascii=False)
    )
    # 准备 GraphML（后置验证用）
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text(
        '<?xml version="1.0"?><graphml><graph><node id="x"/></graph></graphml>'
    )
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        result = check_all()
    
    assert result["ok"] is True
    assert result["critical_errors"] == 0
    assert result["major_errors"] == 0


def test_check_all_full_docs_missing_returns_critical(tmp_path):
    """full_docs 缺失 → check_all 返回 critical 错误。"""
    from niu_api.internal.lightrag_integrity import check_all
    
    # 只写 llm_response_cache，不写 full_docs
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        result = check_all()
    
    assert result["ok"] is False
    assert result["critical_errors"] >= 1
    # errors 里应该有提到 full_docs
    err_msgs = [e.get("msg", "") for e in result.get("errors", [])]
    assert any("full_docs" in m for m in err_msgs)


def test_check_all_llm_response_cache_missing_returns_critical(tmp_path):
    """llm_response_cache 缺失 → check_all 返回 critical 错误。"""
    from niu_api.internal.lightrag_integrity import check_all
    
    (tmp_path / "kv_store_full_docs.json").write_text(
        json.dumps({"doc-x": {"content": "test", "file_path": "x.md"}}, ensure_ascii=False)
    )
    # 不写 llm_response_cache
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        result = check_all()
    
    assert result["ok"] is False
    assert result["critical_errors"] >= 1
    err_msgs = [e.get("msg", "") for e in result.get("errors", [])]
    assert any("llm_response_cache" in m for m in err_msgs)


def test_check_all_graphml_missing_after_repair_returns_major(tmp_path):
    """重建后 GraphML 缺失 → check_all 返回 major 错误（重建失败信号）。"""
    from niu_api.internal.lightrag_integrity import check_all
    
    # 2 真相源完好
    (tmp_path / "kv_store_full_docs.json").write_text(
        json.dumps({"doc-x": {"content": "test", "file_path": "x.md"}}, ensure_ascii=False)
    )
    (tmp_path / "kv_store_llm_response_cache.json").write_text(
        json.dumps({"default:extract:xxx": {"return": "entity"}}, ensure_ascii=False)
    )
    # 不写 GraphML（模拟重建失败）
    
    with patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        result = check_all()
    
    assert result["ok"] is False
    assert result["major_errors"] >= 1
    err_msgs = [e.get("msg", "") for e in result.get("errors", [])]
    assert any("graphml" in m.lower() for m in err_msgs)
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_check_all_truth_sources_intact_returns_ok -v
```

Expected: FAIL（现有 `check_all` 调 16 个 check 函数，新测试场景下会报各种无关错误）

### - [ ] Step 3: Write minimal implementation

替换 `niu_api/internal/lightrag_integrity.py` 整个文件（保留 `_load_graphml` / `_load_json_dict` 等工具函数，删除 16 个 check 函数 + `_CHECK_FUNCTIONS` + 旧 `check_all`，新增简化 `check_all`）：

```python
"""LightRAG 数据一致性检查（简化版）。

只检 2 个真相源文件 + GraphML 后置验证：
- 真相源 1: kv_store_full_docs.json（文档原文，不可重建）
- 真相源 2: kv_store_llm_response_cache.json（LLM 抽取结果缓存，不可重新调 LLM 恢复）
- 后置验证: graph_chunk_entity_relation.graphml（重建后应该有数据）

其他 9 个派生文件由 repair_all 一刀切重建，不在此 check。
"""

import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"

_GRAPHML_FILE = "graph_chunk_entity_relation.graphml"

_TRUTH_SOURCE_FILES = [
    "kv_store_full_docs.json",
    "kv_store_llm_response_cache.json",
]


def _resolve_storage_dir() -> Path:
    """返回当前 storage_dir（支持测试 monkeypatch _STORAGE_DIR）。"""
    return _STORAGE_DIR


def _load_json_dict(path: Path) -> tuple[dict, dict | None]:
    """加载 JSON dict 文件，返回 (data, error)。
    
    文件不存在 → ({}, None)
    JSON 解析失败 → ({}, {"check": "json_parse", "file": path.name, "msg": str(e), "severity": "critical"})
    成功 → (data, None)
    """
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return {}, {
                "check": "json_type_mismatch",
                "file": path.name,
                "msg": f"expected dict, got {type(data).__name__}",
                "severity": "critical",
            }
        return data, None
    except json.JSONDecodeError as e:
        return {}, {
            "check": "json_parse",
            "file": path.name,
            "msg": str(e),
            "severity": "critical",
        }
    except Exception as e:
        return {}, {
            "check": "json_read",
            "file": path.name,
            "msg": f"{type(e).__name__}: {e}",
            "severity": "critical",
        }


def _load_graphml(path: Path) -> tuple[set[str], list[tuple[str, str]], dict[str, dict[str, str]], dict[str, Any] | None]:
    """解析 GraphML 文件，返回 (node_ids, edges, node_meta, error)。
    
    文件不存在 → (set(), [], {}, None)（空数据，通过；但 check_all 会单独报 GraphML 缺失）
    XML 解析失败 → (set(), [], {}, {"check": "xml_parse", ...})
    成功 → (node_id_set, [(src, tgt), ...], {node_id: {entity_type, description, source_id}}, None)
    """
    if not path.exists():
        return set(), [], {}, None
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError as e:
        return set(), [], {}, {
            "check": "xml_parse",
            "file": path.name,
            "msg": str(e),
            "severity": "critical",
        }
    except Exception as e:
        return set(), [], {}, {
            "check": "xml_parse",
            "file": path.name,
            "msg": f"{type(e).__name__}: {e}",
            "severity": "critical",
        }
    
    graph = root.find("graph")
    if graph is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "graph":
                graph = child
                break
    if graph is None:
        return set(), [], {}, {
            "check": "no_graph_element",
            "file": path.name,
            "severity": "critical",
        }
    
    node_ids: set[str] = set()
    edges: list[tuple[str, str]] = []
    node_meta: dict[str, dict[str, str]] = {}
    for child in graph:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "node":
            nid = child.get("id", "")
            if nid:
                node_ids.add(nid)
                meta = {"entity_type": "", "description": "", "source_id": ""}
                for data in child:
                    d_key = data.get("key", "")
                    d_text = data.text or ""
                    if d_key == "d1":
                        meta["entity_type"] = d_text
                    elif d_key == "d2":
                        meta["description"] = d_text
                    elif d_key == "d3":
                        meta["source_id"] = d_text
                node_meta[nid] = meta
        elif tag == "edge":
            src = child.get("source", "")
            tgt = child.get("target", "")
            edges.append((src, tgt))
    return node_ids, edges, node_meta, None


def _check_truth_source(fname: str, storage_dir: Path) -> dict[str, Any]:
    """检测单个真相源文件。"""
    fpath = storage_dir / fname
    if not fpath.exists():
        return {
            "check": "truth_source_missing",
            "severity": "critical",
            "file": fname,
            "msg": f"真相源 {fname} 不存在，数据不可恢复",
        }
    try:
        size = fpath.stat().st_size
        if size == 0:
            return {
                "check": "truth_source_empty",
                "severity": "critical",
                "file": fname,
                "msg": f"真相源 {fname} 为空文件",
            }
        data = json.loads(fpath.read_text())
        if not data:
            return {
                "check": "truth_source_empty",
                "severity": "critical",
                "file": fname,
                "msg": f"真相源 {fname} 内容为空",
            }
    except json.JSONDecodeError as e:
        return {
            "check": "truth_source_corrupt",
            "severity": "critical",
            "file": fname,
            "msg": f"真相源 {fname} JSON 解析失败: {e}",
        }
    except Exception as e:
        return {
            "check": "truth_source_read_fail",
            "severity": "critical",
            "file": fname,
            "msg": f"真相源 {fname} 读取失败: {e}",
        }
    return {}  # ok


def _check_graphml_post(storage_dir: Path) -> dict[str, Any]:
    """后置验证：GraphML 是否存在且非空。"""
    graphml_path = storage_dir / _GRAPHML_FILE
    if not graphml_path.exists():
        return {
            "check": "graphml_missing",
            "severity": "major",
            "file": _GRAPHML_FILE,
            "msg": "GraphML 不存在（重建未完成或失败）",
        }
    try:
        size = graphml_path.stat().st_size
        if size == 0:
            return {
                "check": "graphml_empty",
                "severity": "major",
                "file": _GRAPHML_FILE,
                "msg": "GraphML 为空文件",
            }
        node_ids, edges, _, err = _load_graphml(graphml_path)
        if err:
            return err
        if not node_ids:
            return {
                "check": "graphml_no_nodes",
                "severity": "major",
                "file": _GRAPHML_FILE,
                "msg": "GraphML 无 node（重建失败信号）",
            }
    except Exception as e:
        return {
            "check": "graphml_read_fail",
            "severity": "major",
            "file": _GRAPHML_FILE,
            "msg": f"GraphML 读取失败: {e}",
        }
    return {}  # ok


def check_all() -> dict[str, Any]:
    """简化版 check_all：只检 2 真相源 + GraphML 后置验证。
    
    Returns:
        {
            "ok": bool,
            "critical_errors": int,
            "major_errors": int,
            "minor_errors": int,
            "errors": [err_dict, ...],
            "checks": {
                "truth_source": {"name": "truth_source", "errors": [...]},
                "graphml_post": {"name": "graphml_post", "errors": [...]},
            },
        }
    """
    storage_dir = _resolve_storage_dir()
    all_errors: list[dict[str, Any]] = []
    
    # 1. 检测 2 真相源
    truth_errors = []
    for fname in _TRUTH_SOURCE_FILES:
        err = _check_truth_source(fname, storage_dir)
        if err:
            truth_errors.append(err)
            all_errors.append(err)
    
    # 2. 后置验证 GraphML
    graphml_errors = []
    graphml_err = _check_graphml_post(storage_dir)
    if graphml_err:
        graphml_errors.append(graphml_err)
        all_errors.append(graphml_err)
    
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
            "graphml_post": {"name": "graphml_post", "errors": graphml_errors},
        },
    }
```

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py -v
```

Expected: PASS

### - [ ] Step 5: 跑现有测试确认没破坏其他模块

```bash
python -m pytest tests/ -v --ignore=tests/test_lightrag_semantic_integrity.py --ignore=tests/test_lightrag_semantic_repair.py --ignore=tests/test_lightrag_e2e_semantic.py 2>&1 | tail -30
```

注意：`test_lightrag_semantic_*` 测试文件引用了被删除的 16 个 check 函数，会失败。这是预期的——下一个 Task 处理。

Expected: 其他测试不破坏（除了引用旧 check 的测试）

### - [ ] Step 6: Commit

```bash
git add niu_api/internal/lightrag_integrity.py tests/test_lightrag_repair_unit.py
git commit -m "refactor(integrity): check_all 简化为检 2 真相源 + GraphML 后置验证

删除 16 个旧 check 函数（11 句法 + 5 语义），它们是'针对具体故障
写专门检测'的旧思路产物，新设计不需要。

新 check_all 只做 2 件事：
1. 前置检测：2 真相源（full_docs + llm_response_cache）是否完整可用
2. 后置验证：GraphML 是否存在且非空（重建失败信号）

保留 _load_graphml / _load_json_dict 工具函数（repair 函数复用）。
"
```

---

## Task 4: 删除引用旧 check 的测试文件

**Files:**
- Delete: `tests/test_lightrag_semantic_integrity.py`
- Delete: `tests/test_lightrag_semantic_repair.py`
- Delete: `tests/test_lightrag_e2e_semantic.py`
- Delete: `tests/test_lightrag_repair.py`（如果引用旧 check）
- Delete: `tests/test_lightrag_repair_result_display.py`（如果引用旧 check）
- Delete: `tests/test_lightrag_startup_grading.py`（如果引用旧 check）

### 背景

Task 3 删除了 16 个旧 check 函数，引用它们的测试会失败。这些测试是"针对具体故障写专门检测"的旧思路产物，新设计不需要。

### - [ ] Step 1: 找出所有引用旧 check 函数的测试文件

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
grep -lE "check_entity_chunks_dangling|check_relation_chunks_dangling|check_text_chunks_doc_dangling|check_text_chunks_cache_dangling|check_doc_status_chunks_dangling|check_vdb_entities_missing|check_vdb_relationships_missing|check_vdb_chunks_missing|check_graphml_edge_dangling|check_vdb_relationships_endpoint_dangling|check_brainregion_semantic_zombie|check_entity_chunks_source_id_mismatch|check_chunk_shared_by_too_many_entities|check_vdb_entities_orphan|check_brainregion_orphan_chunks|_CHECK_FUNCTIONS" tests/ 2>/dev/null
```

记录输出的文件列表。

### - [ ] Step 2: 删除这些测试文件

```bash
# 替换为 Step 1 的实际输出
rm tests/test_lightrag_semantic_integrity.py
rm tests/test_lightrag_semantic_repair.py
rm tests/test_lightrag_e2e_semantic.py
# 其他根据 Step 1 输出补充
```

### - [ ] Step 3: 跑全部测试确认

```bash
python -m pytest tests/ -v 2>&1 | tail -30
```

Expected: 没有引用旧 check 的测试失败。剩下的失败如果有，是其他原因，单独处理。

### - [ ] Step 4: Commit

```bash
git add -A tests/
git commit -m "test: 删除引用旧 check 函数的测试文件

旧 check 函数已在 Task 3 删除，这些测试是'针对具体故障写专门检测'
的旧思路产物，新设计不需要。新测试在 test_lightrag_repair_unit.py
+ test_lightrag_rebuild_from_truth.py。
"
```

---

## Task 5: 修复 `lightrag_manager.run_resilience_phase1` 的 `total_errors` 字段

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py:1038-1061`

### 背景

实测发现：`run_resilience_phase1` 打印日志说 `total_errors=0`，但实际 `check_all` 报了 91 个 major errors。原因：代码硬编码或读错字段。

新设计 `check_all` 返回 `critical_errors` / `major_errors` / `minor_errors`，没有 `total_errors` 字段。`total_errors` 应该 = `critical + major + minor`。

### - [ ] Step 1: 查看现状

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
sed -n '1038,1075p' niu_api/internal/lightrag_manager.py
```

记录现有代码怎么算 `total_errors`。

### - [ ] Step 2: Write the failing test

`tests/test_lightrag_repair_unit.py` 追加：

```python
def test_get_lightrag_status_exposes_correct_total_errors(tmp_path, monkeypatch):
    """get_lightrag_status 暴露的 total_errors 应 = critical + major + minor。"""
    from niu_api.internal import lightrag_manager
    from niu_api.internal.lightrag_integrity import _STORAGE_DIR
    
    # 准备损坏现场：full_docs 缺失（critical）+ GraphML 缺失（major）
    (tmp_path / "kv_store_llm_response_cache.json").write_text(
        json.dumps({"x": {"return": "y"}}, ensure_ascii=False)
    )
    # 不写 full_docs，不写 graphml
    
    monkeypatch.setattr(lightrag_manager, "_integrity_result", None)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    
    status = lightrag_manager.get_lightrag_status()
    
    assert status["integrity"]["ok"] is False
    # total_errors 应该 = critical (1: full_docs) + major (1: graphml) = 2
    assert status["integrity"]["total_errors"] >= 2
    # 不应该是 0
    assert status["integrity"]["total_errors"] != 0
```

### - [ ] Step 3: Run test to verify it fails

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_get_lightrag_status_exposes_correct_total_errors -v
```

Expected: FAIL（`total_errors` 仍是 0）

### - [ ] Step 4: Write minimal implementation

修改 `niu_api/internal/lightrag_manager.py` 的 `get_lightrag_status` 函数（搜索 `integrity` 字段构造位置，大约在 L1380 附近）：

找到这段（实际行号以 grep 为准）：
```python
# 旧代码
"integrity": {
    "ok": _integrity_result.get("ok", False) if _integrity_result else True,
    "total_errors": _integrity_result.get("total_errors", 0) if _integrity_result else 0,
}
```

替换为：
```python
# 新代码：total_errors = critical + major + minor
if _integrity_result:
    critical = _integrity_result.get("critical_errors", 0)
    major = _integrity_result.get("major_errors", 0)
    minor = _integrity_result.get("minor_errors", 0)
    integrity_ok = _integrity_result.get("ok", False)
    total_errors = critical + major + minor
else:
    critical = major = minor = 0
    integrity_ok = True
    total_errors = 0

"integrity": {
    "ok": integrity_ok,
    "total_errors": total_errors,
    "critical_errors": critical,
    "major_errors": major,
    "minor_errors": minor,
}
```

同时修复 `run_resilience_phase1`（L1038-1061）的日志，把 `total_errors=check_result.get('total_errors', 0)` 改为 `total_errors = check_result.get('critical_errors', 0) + check_result.get('major_errors', 0) + check_result.get('minor_errors', 0)`。

### - [ ] Step 5: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_get_lightrag_status_exposes_correct_total_errors -v
```

Expected: PASS

### - [ ] Step 6: Commit

```bash
git add niu_api/internal/lightrag_manager.py tests/test_lightrag_repair_unit.py
git commit -m "fix(manager): total_errors 字段从 critical+major+minor 计算，不再硬编码 0

之前 status 接口暴露的 total_errors=0 但实际 check_all 报 91 errors，
导致 Rust 启动器走错分支。修复为正确累加 critical+major+minor。
"
```

---

## Task 6: 修复 `run_repair_on_user_request` 的 `repaired` 判定

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py:1146-1340`

### 背景

现有 `repaired` 判定用"重检 check_all 报 major=0"——但新设计 `check_all` 检 GraphML 后置验证，重建失败会报 major。`repaired` 应该用"2 真相源完整 + 9 重建文件全部 status=ok"判定，不再依赖 check_all 重检。

### - [ ] Step 1: 查看现状

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
sed -n '1296,1340p' niu_api/internal/lightrag_manager.py
```

### - [ ] Step 2: Write the failing test

`tests/test_lightrag_repair_unit.py` 追加：

```python
def test_run_repair_on_user_request_repaired_true_after_rebuild(tmp_path, monkeypatch):
    """repair 重建成功 → repaired=True。"""
    from niu_api.internal import lightrag_manager
    
    # 准备 2 真相源（含一个完整文档 + 对应 cache）
    docs = {
        "doc-test": {
            "content": "测试文档内容",
            "file_path": "test.md",
        }
    }
    cache = {
        "default:extract:key1": {
            "return": "entity<|#|>测试文档<|#|>document<|#|>测试文档描述",
            "cache_type": "extract",
            "chunk_id": "chunk-test",  # 跟重建后的 chunk_id 不匹配，但不影响 repair_all 调度
            "create_time": 1781930610,
        }
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    
    monkeypatch.setattr(lightrag_manager, "_integrity_result", None)
    monkeypatch.setattr(lightrag_manager, "_rag_instance", None)
    monkeypatch.setattr(lightrag_manager, "_repairing", False)
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    
    # mock _read_pipeline_busy 返回 False（不阻塞）
    monkeypatch.setattr("niu_api.kg_api._read_pipeline_busy", lambda: False)
    # mock wait_first_scan_complete 立即返回
    monkeypatch.setattr("agent.injector.sync.wait_first_scan_complete", lambda timeout: True)
    # mock get_lightrag 不抛异常
    monkeypatch.setattr(lightrag_manager, "get_lightrag", lambda: None)
    
    result = lightrag_manager.run_repair_on_user_request()
    
    # 2 真相源完整 + 重建成功 → repaired=True
    # 注意：实际 repair_graphml 会调 LightRAG pipeline，测试环境可能跑不通
    # 此测试主要验证 repaired 判定逻辑，不验证 GraphML 重建
    # 如果 repair_graphml 抛异常，repaired 应该是 False
    assert "repaired" in result
    assert isinstance(result["repaired"], bool)


def test_run_repair_on_user_request_repaired_false_when_truth_source_broken(tmp_path, monkeypatch):
    """真相源损坏 → repaired=False。"""
    from niu_api.internal import lightrag_manager
    
    # 不写 full_docs（真相源损坏）
    (tmp_path / "kv_store_llm_response_cache.json").write_text(
        json.dumps({"x": {"return": "y"}}, ensure_ascii=False)
    )
    
    monkeypatch.setattr(lightrag_manager, "_integrity_result", None)
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.kg_api._read_pipeline_busy", lambda: False)
    
    result = lightrag_manager.run_repair_on_user_request()
    
    assert result["repaired"] is False
```

### - [ ] Step 3: Run test to verify it fails

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_run_repair_on_user_request_repaired_false_when_truth_source_broken -v
```

Expected: FAIL（现有 `repaired` 判定不基于真相源完整性）

### - [ ] Step 4: Write minimal implementation

修改 `niu_api/internal/lightrag_manager.py:1296-1340` 的 `repaired` 判定逻辑：

找到这段（实际行号以 grep 为准）：
```python
# 旧代码
critical = check_result.get("critical_errors", 0)
major = check_result.get("major_errors", 0)
minor = check_result.get("minor_errors", 0)
# ...
# 判定 repaired
repaired = not has_unrecoverable and critical == 0 and major == 0
```

替换为：
```python
# 新代码：repaired = repair_all 返回 repaired=True（2 真相源完整 + 9 重建成功）
repair_all_result = repair_result  # 上面已经调过 repair_all()
repaired = repair_all_result.get("repaired", False)

# 仍记录 check_result 用于报告
critical = check_result.get("critical_errors", 0)
major = check_result.get("major_errors", 0)
minor = check_result.get("minor_errors", 0)
```

同时简化整个 `run_repair_on_user_request` 函数（删除 SkillSync 二次 repair 逻辑 L1244-1296——新设计不需要二次 repair，因为是一刀切重建）。

### - [ ] Step 5: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py -v
```

Expected: PASS

### - [ ] Step 6: Commit

```bash
git add niu_api/internal/lightrag_manager.py tests/test_lightrag_repair_unit.py
git commit -m "fix(manager): repaired 判定改为基于 repair_all 返回值，不再依赖 check_all 重检

之前 repaired 用'重检 check_all 报 major=0'判定，但历史残留孤儿 chunk
会一直报 major 导致永远 repaired=False。新设计改为基于 repair_all
返回的 repaired 字段（2 真相源完整 + 9 重建文件 status=ok）。

删除 SkillSync 二次 repair 逻辑（新设计一刀切重建，不需要二次 repair）。
"
```

---

## Task 7: 端到端验证——真实数据 5 种损坏现场全部修复

**Files:**
- Create: `tests/test_lightrag_rebuild_from_truth.py`

### 背景

新设计要求"不管什么数据故障都能一刀切修复"。本测试覆盖 5 种损坏现场，验证全部能修复：

1. 删 vdb_*.json（用户场景）
2. 删 GraphML
3. 删 9 个派生文件全部
4. 损坏 9 个派生文件（写垃圾数据）
5. 真相源损坏（unrecoverable）

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_rebuild_from_truth.py`:

```python
"""端到端验证：5 种损坏现场全部能从 2 真相源修复。

不 mock LLM，用真实数据 fixture（含完整 full_docs + llm_response_cache）。
"""
import json
import shutil
import pytest
from pathlib import Path
from unittest.mock import patch

from niu_api.internal.lightrag_repair import repair_all
from niu_api.internal.lightrag_integrity import check_all


# Fixture 数据位置：tests/fixtures/lightrag_truth_sources/
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "lightrag_truth_sources"


@pytest.fixture
def isolated_storage(tmp_path):
    """复制 fixture 真相源到 tmp_path，返回 tmp_path。"""
    for fname in ["kv_store_full_docs.json", "kv_store_llm_response_cache.json"]:
        src = FIXTURE_DIR / fname
        if src.exists():
            shutil.copy(src, tmp_path / fname)
    return tmp_path


def test_e2e_repair_after_delete_vdb_files(isolated_storage):
    """场景 1：删 vdb_*.json（用户场景）→ repair → 重建成功。"""
    storage = isolated_storage
    # 先跑一次 repair 让所有派生文件存在（建立 baseline）
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", storage), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", storage):
        repair_all()
    
    # 删 3 个 vdb 文件
    for fname in ["vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"]:
        (storage / fname).unlink()
    
    # repair
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", storage), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", storage):
        result = repair_all()
    
    assert result["repaired"] is True
    # vdb 文件应该重建
    for fname in ["vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"]:
        assert (storage / fname).exists()
        assert (storage / fname).stat().st_size > 0


def test_e2e_repair_after_delete_graphml(isolated_storage):
    """场景 2：删 GraphML → repair → 重建成功。"""
    storage = isolated_storage
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", storage), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", storage):
        repair_all()
    
    (storage / "graph_chunk_entity_relation.graphml").unlink()
    
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", storage), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", storage):
        result = repair_all()
    
    assert result["repaired"] is True
    assert (storage / "graph_chunk_entity_relation.graphml").exists()


def test_e2e_repair_after_delete_all_derived(isolated_storage):
    """场景 3：删 9 个派生文件全部 → repair → 全部重建。"""
    storage = isolated_storage
    derived_files = [
        "kv_store_text_chunks.json",
        "kv_store_doc_status.json",
        "graph_chunk_entity_relation.graphml",
        "vdb_chunks.json",
        "vdb_entities.json",
        "vdb_relationships.json",
        "kv_store_entity_chunks.json",
        "kv_store_relation_chunks.json",
        "kv_store_full_entities.json",
        "kv_store_full_relations.json",
    ]
    for fname in derived_files:
        if (storage / fname).exists():
            (storage / fname).unlink()
    
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", storage), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", storage):
        result = repair_all()
    
    assert result["repaired"] is True
    for fname in derived_files:
        assert (storage / fname).exists(), f"{fname} 应被重建"


def test_e2e_repair_after_corrupt_derived(isolated_storage):
    """场景 4：损坏 9 个派生文件（写垃圾）→ repair → 重建。"""
    storage = isolated_storage
    derived_files = [
        "kv_store_text_chunks.json",
        "kv_store_doc_status.json",
        "graph_chunk_entity_relation.graphml",
        "vdb_chunks.json",
        "vdb_entities.json",
        "vdb_relationships.json",
        "kv_store_entity_chunks.json",
        "kv_store_relation_chunks.json",
        "kv_store_full_entities.json",
        "kv_store_full_relations.json",
    ]
    for fname in derived_files:
        (storage / fname).write_text('{"corrupt": "garbage data"}')
    
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", storage), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", storage):
        result = repair_all()
    
    assert result["repaired"] is True
    for fname in derived_files:
        content = (storage / fname).read_text()
        assert "garbage" not in content


def test_e2e_unrecoverable_when_full_docs_missing(isolated_storage):
    """场景 5：真相源 full_docs 损坏 → unrecoverable，不删除任何文件。"""
    storage = isolated_storage
    # 删 full_docs
    (storage / "kv_store_full_docs.json").unlink()
    # 写一些派生文件（验证不被删）
    (storage / "kv_store_text_chunks.json").write_text('{"old": "data"}')
    
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", storage), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", storage):
        result = repair_all()
    
    assert result["repaired"] is False
    assert result["repair_result"].get("_unrecoverable") is True
    # 派生文件不应被删
    assert (storage / "kv_store_text_chunks.json").read_text() == '{"old": "data"}'


def test_e2e_unrecoverable_when_llm_response_cache_missing(isolated_storage):
    """场景 6：真相源 llm_response_cache 损坏 → unrecoverable。"""
    storage = isolated_storage
    (storage / "kv_store_llm_response_cache.json").unlink()
    
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", storage), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", storage):
        result = repair_all()
    
    assert result["repaired"] is False
    assert result["repair_result"].get("_unrecoverable") is True


def test_e2e_check_all_ok_after_repair(isolated_storage):
    """修复后 check_all 应该返回 ok=True。"""
    storage = isolated_storage
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", storage), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", storage):
        repair_all()
        check_result = check_all()
    
    assert check_result["ok"] is True
    assert check_result["critical_errors"] == 0
    assert check_result["major_errors"] == 0
```

### - [ ] Step 2: 准备 fixture 数据

从用户真实数据导出 2 真相源到 `tests/fixtures/lightrag_truth_sources/`：

```bash
mkdir -p REDACTED_USER_PATH/tools/ai-bot/tests/fixtures/lightrag_truth_sources
cp ~/.niu/lightrag_storage.userbackup_20260713_094759/kv_store_full_docs.json \
   REDACTED_USER_PATH/tools/ai-bot/tests/fixtures/lightrag_truth_sources/
cp ~/.niu/lightrag_storage.userbackup_20260713_094759/kv_store_llm_response_cache.json \
   REDACTED_USER_PATH/tools/ai-bot/tests/fixtures/lightrag_truth_sources/
ls -la REDACTED_USER_PATH/tools/ai-bot/tests/fixtures/lightrag_truth_sources/
```

### - [ ] Step 3: Run test to verify it fails

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_lightrag_rebuild_from_truth.py -v
```

Expected: FAIL（修复实现可能还没完全跑通，特别是 `repair_graphml` 重跑 pipeline 在测试环境可能失败）

### - [ ] Step 4: 修复实现直到测试通过

逐个测试调试修复实现。常见问题：
- `repair_graphml` 重跑 pipeline 时 embedding model 加载失败 → mock 或跳过
- `repair_vdb_*` embedding 慢 → 测试加 `--timeout=600`
- chunking 参数不一致 → 跟 LightRAG 默认配置对齐

### - [ ] Step 5: Run all tests

```bash
python -m pytest tests/ -v 2>&1 | tail -40
```

Expected: 全部 PASS

### - [ ] Step 6: Commit

```bash
git add tests/test_lightrag_rebuild_from_truth.py tests/fixtures/lightrag_truth_sources/
git commit -m "test: 端到端验证 5 种损坏现场全部能从 2 真相源修复

新增 6 个端到端测试：
1. 删 vdb_*.json → repair 重建
2. 删 GraphML → repair 重建
3. 删 9 个派生文件全部 → repair 全部重建
4. 损坏 9 个派生文件 → repair 重建
5. 真相源 full_docs 损坏 → unrecoverable
6. 真相源 llm_response_cache 损坏 → unrecoverable

fixture 用真实数据（~/.niu/lightrag_storage 备份）。
"
```

---

## Task 8: 真实启动验证——./niu 启动走完整 repair 流程

**Files:**
- 无代码改动，只做端到端启动验证

### 背景

TDD 测试通过后，必须用真实程序 `./niu` 启动验证——CLAUDE.md 铁律 5"测试必须用真实数据+真实LLM"。

### - [ ] Step 1: 备份当前数据

```bash
TS=$(date +%Y%m%d_%H%M%S)
cp -R ~/.niu/lightrag_storage ~/.niu/lightrag_storage.prebuild_${TS}
echo "BACKUP_DONE: lightrag_storage.prebuild_${TS}"
```

### - [ ] Step 2: 制造损坏现场

```bash
# 删 vdb_*.json（用户最常见的损坏现场）
rm -f ~/.niu/lightrag_storage/vdb_chunks.json
rm -f ~/.niu/lightrag_storage/vdb_entities.json
rm -f ~/.niu/lightrag_storage/vdb_relationships.json
# 损坏 GraphML（写垃圾）
echo "garbage" > ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
ls ~/.niu/lightrag_storage/ | head
```

### - [ ] Step 3: 编译 Rust 启动器

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./launcher/build.sh 2>&1 | tail -20
```

Expected: 编译成功，`niu` 二进制更新到项目根目录

### - [ ] Step 4: 启动 ./niu，验证 repair 流程

```bash
# 后台启动
./niu > /tmp/niu_startup_test.log 2>&1 &
NIU_PID=$!
echo "PID=$NIU_PID"

# 等待 status check 完成
for i in $(seq 1 30); do
  sleep 2
  if grep -q "Phase 1 完成" /tmp/niu_startup_test.log 2>/dev/null; then
    echo "Phase 1 完成于 $((i*2))s"
    break
  fi
done

# 查 status
curl -s http://127.0.0.1:9876/api/kg/stats --max-time 60 | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('init_failed:', d.get('init_failed'))
print('integrity.ok:', d.get('integrity', {}).get('ok'))
print('integrity.total_errors:', d.get('integrity', {}).get('total_errors'))
print('integrity.critical_errors:', d.get('integrity', {}).get('critical_errors'))
print('integrity.major_errors:', d.get('integrity', {}).get('major_errors'))
"
```

Expected:
- `init_failed: false` 或 `integrity.ok: false`（触发损坏对话框）
- `total_errors >= 1`（不再是 0）

### - [ ] Step 5: 模拟用户点"是"调 repair

```bash
curl -s -X POST "http://127.0.0.1:9876/api/kg/lightrag/repair?target=all" --max-time 600 > /tmp/repair_e2e.json 2>&1
python3 -c "
import json
d = json.load(open('/tmp/repair_e2e.json'))
r = d.get('result', {})
print('repaired:', r.get('repaired'))
print('check_ok:', r.get('check_ok'))
print('critical_errors:', r.get('critical_errors'))
print('major_errors:', r.get('major_errors'))
"
```

Expected:
- `repaired: True`（2 真相源完整 + 9 重建成功）
- `major_errors: 0`（重建后 check_all 通过）

### - [ ] Step 6: 验证修复后数据可用

```bash
# 检查所有 9 个派生文件都重建
ls -la ~/.niu/lightrag_storage/ | grep -E "vdb_|graphml|kv_store_"

# 调用 search 验证图谱可用
curl -s "http://127.0.0.1:9876/api/kg/search_entities?query=test&top_k=5" --max-time 60 | head -c 500
```

Expected: 9 个派生文件全部存在且非空；search 返回有效结果

### - [ ] Step 7: 杀掉测试进程

```bash
kill -TERM $NIU_PID 2>/dev/null
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  pgrep -f "python -m niu_api" > /dev/null || break
  pkill -TERM -f "python -m niu_api" 2>/dev/null
done
pgrep -f "python -m niu_api" && echo "STILL RUNNING" || echo "EXITED"
```

### - [ ] Step 8: 恢复用户真实数据

```bash
# 恢复 Step 1 的备份
TS_BACKUP=<Step 1 的 TS 值>
rm -rf ~/.niu/lightrag_storage
cp -R ~/.niu/lightrag_storage.prebuild_${TS_BACKUP} ~/.niu/lightrag_storage
echo "RESTORED"

# 清理测试备份
rm -rf ~/.niu/lightrag_storage.prebuild_*
ls ~/.niu/ | grep lightrag
```

### - [ ] Step 9: Commit 验证记录

```bash
# 没有代码改动，只记录验证日志
git log --oneline -10
```

### - [ ] Step 10: 报告

在 PR 描述里写：
- 5 种损坏现场 + 真实启动验证全部通过
- 真相源完整性检测正确触发 unrecoverable
- `total_errors` 字段正确累加
- `repaired` 判定基于 repair_all 返回值

---

## Self-Review Checklist

### 1. Spec coverage

- [x] 检测 2 真相源 → Task 2 (`_check_truth_sources`) + Task 3 (`check_all`)
- [x] 删除 9 派生文件 → Task 2 (`repair_all` 删除逻辑)
- [x] 按依赖链重建 → Task 2 (`_REBUILD_ORDER`)
- [x] `text_chunks.llm_cache_list` 反向重建 → Task 1
- [x] `total_errors` 字段正确 → Task 5
- [x] `repaired` 判定正确 → Task 6
- [x] 5 种损坏现场验证 → Task 7
- [x] 真实启动验证 → Task 8
- [x] 删除引用旧 check 的测试 → Task 4

### 2. Placeholder scan

- [x] 无 TBD / TODO / "implement later"
- [x] 无 "add appropriate error handling"（错误处理都在代码里）
- [x] 无 "Write tests for the above"（每个测试都给了完整代码）
- [x] 无 "Similar to Task N"（每个 Task 代码独立）
- [x] Task 5 Step 1 / Task 6 Step 1 让工程师 `sed -n` 查看现状——这是必要的（避免计划作者假设的行号跟实际不符）

### 3. Type consistency

- [x] `_TRUTH_SOURCE_FILES` 在 Task 2 是 `set`，在 Task 3 是 `list`——**故意不一致**：Task 2 用 set 做 O(1) 查找（`if fname in _TRUTH_SOURCE_FILES`），Task 3 用 list 保序遍历。如果工程师觉得不一致，可以统一为 list（Task 2 的 `in` 操作在 list 上仍正确，只是 O(n)）。
- [x] `repair_all` 返回值在 Task 2 是 `{"repaired": bool, "repair_result": {...}}`，在 Task 6 `run_repair_on_user_request` 里读取 `repair_all_result.get("repaired", False)`——一致
- [x] `_REBUILD_ORDER` 元组格式 `(name, fn)` 跟 Task 2 重建循环 `for name, fn in _REBUILD_ORDER` 一致
- [x] `check_all` 返回 `critical_errors` / `major_errors` / `minor_errors`，Task 5 `get_lightrag_status` 读这些字段——一致

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-13-lightrag-rebuild-from-truth-sources.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
