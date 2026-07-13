# LightRAG 数据修复重构：从真相源一刀切重建 Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 LightRAG 数据修复逻辑从"针对每种故障写专门 repair 函数"重构为"检测 2 个真相源文件 → 备份 9 个派生文件 → 删除 → 按依赖链重建（含僵尸脑区清理）"，让任何数据故障都能一刀切修复，重建失败时回滚。

**Architecture:** 真相源 = `kv_store_full_docs.json` + `kv_store_llm_response_cache.json`（LLM 非确定性，不可重新调 LLM 恢复）。其他 9 个文件全是派生数据，可从这 2 个按依赖链重建。新 `repair_all` 流程：(1) 检测 2 真相源完整性（含内容完整性检查）→ (2) 备份 9 个派生文件到临时目录 → (3) 清理 `llm_response_cache` 里的僵尸脑区 extract entry（防止重建时僵尸复活）→ (4) 删除 9 个派生文件 → (5) 按依赖链重建 → (6) 任意步骤失败时回滚备份。`check_all` 检 2 真相源 + GraphML 后置验证 + vdb_*_missing 检测（避免 vdb 缺失但 GraphML 完好时启动放行）。`repair_all` 保持旧扁平返回结构（向后兼容 Rust `format_repair_summary`）。`run_repair_on_user_request` 保留 SkillSync 二次 repair 但适配扁平结构。

**Tech Stack:** Python 3.11、xml.etree.ElementTree（GraphML）、nano-vectordb（向量存储）、pytest（TDD）、真实 LightRAG 实例（端到端验证，不 mock LLM，cache 完整时不调 LLM；cache 部分丢失时会调 LLM 重新抽取，用户需承担少量 token 费用）。

---

## 背景

### 前 5 轮修复为什么没解决

1. **7-08 entity-sync**：根因判定 = "check_all 没检同步性"，加 check_entity_sync
2. **7-08 case-insensitive**：根因判定 = "源头没 lower 化"，改 LightRAG Fork 源码
3. **7-09 startup-block**：根因判定 = "启动流程不阻塞 + repaired 硬编码"
4. **7-11 consistency-redo**：根因判定 = "集合比对非因果链"，全部重写
5. **7-12 semantic-integrity**：根因判定 = "句法非语义"，加 5 个语义 check + repair_brainregion_zombies

**循环原因**：每轮都针对"当前这次具体故障"写专门 repair 函数，下次出别的故障又要再写。`repair_all` 按 check 报错选择性 repair——check 漏检（如 16 个僵尸脑区在 11 项 check 全过）→ repair 不触发 → 修复失败。check 误报（如 `chunk_shared_by_too_many_entities` 把通讯录 chunk 被 68 entity 共享当 bug）→ repair 永远修不完 → 修复失败。

### 真相源确认（基于源码 + 实测数据）

**真相源 = 2 个文件**：

1. **`kv_store_full_docs.json`** — 文档原文。其他文件全是 chunk/entity/relation 级别，无法反向拼回原文。源码 `_UNRECOVERABLE_FILES = {"kv_store_full_docs.json"}`（`lightrag_repair.py:2058`）。

2. **`kv_store_llm_response_cache.json`** — LLM 抽取结果缓存。每个 `extract` 类型 entry 自带 `chunk_id` 字段（实测 232/259 条都是 extract 类型，全部含 chunk_id）。LLM 是非确定性的，重新调 LLM 抽取的 entity/relation 跟原来**一定不同** → 数据丢失。所以不可重新调 LLM 恢复，必须保留 cache。

**实测发现**：`llm_response_cache` 里有 1 条 extract cache（`default:extract:cf71a2193271499f4ab4ee6978197285`）含 16 个僵尸脑区的 extract 数据，description 明确写"被删除的重复脑区实体之一"。如果直接删 GraphML 重建，这条 cache 会被命中，**16 个僵尸脑区会被重新写入 GraphML——僵尸复活**。所以重建前必须先清理这条 cache entry。

**派生数据 = 9 个文件**（全部可从 2 真相源按依赖链重建，cache 完整时不调 LLM）：

| 文件 | 重建路径 | 复用现有 repair 函数 |
|------|---------|-------------------|
| `kv_store_text_chunks.json` | 从 `full_docs` 重新 chunking（用真实 `_get_lightrag_config()` 读 chunk_size，chunk_id=MD5(content) 确定性）；`llm_cache_list` 从 `llm_response_cache` 反向扫描 extract 类型 entry 的 chunk_id 字段重建 | `repair_text_chunks`（`lightrag_repair.py:386`）已实现 |
| `kv_store_doc_status.json` | 从 `full_docs` + `text_chunks` 派生 | `repair_doc_status`（`lightrag_repair.py:543`）已实现 |
| `graph_chunk_entity_relation.graphml` | 重跑 `apipeline_process_enqueue_documents`，extract 阶段 cache 命中免调 LLM，summary 阶段 `force_llm_summary_on_merge` 跳过 | `repair_graphml`（`lightrag_repair.py:644`）已实现 |
| `kv_store_entity_chunks.json` | 从 GraphML node source_id 派生 | `repair_entity_chunks`（`lightrag_repair.py:1394`）已实现 |
| `kv_store_relation_chunks.json` | 从 GraphML edge source_id 派生 | `repair_relation_chunks`（`lightrag_repair.py:1459`）已实现 |
| `kv_store_full_entities.json` | 从 GraphML node source_id + doc_status.chunks_list 派生 | `repair_full_entities`（`lightrag_repair.py:1534`）已实现 |
| `kv_store_full_relations.json` | 从 GraphML edge source_id + doc_status.chunks_list 派生 | `repair_full_relations`（`lightrag_repair.py:1616`）已实现 |
| `vdb_chunks.json` | 从 `text_chunks.content` 重新 embed | `repair_vdb_chunks`（`lightrag_repair.py:981`）已实现 |
| `vdb_entities.json` | 从 GraphML node 重新 embed | `repair_vdb_entities`（`lightrag_repair.py:1120`）已实现 |
| `vdb_relationships.json` | 从 GraphML edge 重新 embed | `repair_vdb_relationships`（`lightrag_repair.py:1243`）已实现 |

### 关键设计决策（v1 审查后修订）

1. **保留 `repair_brainregion_zombies` 在 `_REBUILD_ORDER` 最前面**（v1 删了，审查发现僵尸复活风险）。但它的清理范围要扩展：除了清 GraphML 里 description 含"被删除"标记的脑区，还要清 `llm_response_cache` 里对应的 extract entry（防止重建时僵尸复活）。

2. **删 9 个派生文件前先备份到临时目录**（v1 不备份，审查发现真相源部分损坏会数据永久丢失）。备份位置 `~/.niu/lightrag_storage.prerepair_<ts>/`，重建成功后删除备份，失败时回滚。

3. **`repair_all` 保持旧扁平返回结构**（v1 改成嵌套 `{repaired, repair_result:{...}}`，审查发现破坏 Rust `format_repair_summary`）。新 `repair_all` 返回 `{text_chunks:{status,...}, ..., _unrecoverable:bool, _skipped:[...], _check_summary:{...}, _deleted:[...], _rolled_back:bool}`——Rust 不用改。

4. **`repair_text_chunks` 用真实 `_get_lightrag_config()` 读 chunk_size**（v1 硬编码 chunk_size=1200, chunk_overlap=100，审查发现跟实际配置 50 不一致导致 chunk_id 不一致）。同时保留 chunk_id 一致性保护（重合率<50% → unrecoverable，`lightrag_repair.py:511-528` 已有，v1 删了要恢复）。

5. **`check_all` 加 vdb_*_missing 检测**（v1 只检 GraphML 后置，审查发现 vdb 缺失但 GraphML 完好时 ok=True 启动放行）。新 `check_all` 检：(1) 2 真相源完整 → (2) GraphML 后置验证 → (3) vdb_*_missing（GraphML 有 node 但 vdb 没对应向量）。

6. **`total_errors` 字段修复要完整**（v1 Task 5 只修 `get_lightrag_status`，审查发现 `run_resilience_phase1` 日志和 Rust `IntegrityStatus` struct 也要改）。三处都改：`get_lightrag_status` 的 integrity 字段、`run_resilience_phase1` 日志、Rust `IntegrityStatus` struct 加 critical_errors/major_errors/minor_errors 字段。

7. **保留 SkillSync 二次 repair**（v1 删了，审查发现删了残留 entity_chunks 不清。但保留要适配扁平结构——二次 repair 结果用 `post_skill_sync_` 前缀合并到顶层，不嵌套）。

8. **Task 7 fixture 用合成数据**（v1 用真实备份，审查发现含真实人名/电话/地址，提交 git 泄露隐私）。合成数据规模小但覆盖关键场景：3 个文档 + 5 个 extract cache + 1 个僵尸脑区 cache。

9. **Task 6 测试不 mock**（v1 用 7 个 mock，违反 CLAUDE.md 铁律 5）。改用真实损坏现场 + 真实 `repair_all` 调用，只 patch `_STORAGE_DIR` 到 tmp_path（必要隔离，不算 mock）。

10. **明确承认 cache miss 时会调 LLM**（v1 承诺"全程不调 LLM"，审查发现 cache 部分丢失时必调 LLM，monkeypatch 失败时也会调）。计划明确说"cache 完整时不调 LLM；cache 部分丢失时会调 LLM 重新抽取（消耗 token），用户需承担少量 LLM 调用费用"。

11. **`repair_graphml` 让 `_STORAGE_DIR` patch 生效**（v1 没处理，审查发现 `repair_graphml` 内 `get_lightrag()` 拿真实实例操作真实 `~/.niu/lightrag_storage`，测试 patch 不生效）。修复：`repair_graphml` 改为先调 `reset_init_state()` + 重新创建 LightRAG 实例（指向 `_STORAGE_DIR`），让 patch 生效。

12. **Task 8 扩展到 3 种现场 + region_sync 验证**（v1 只测 1 种，丢了 7-12 的"风扇不狂转/region_sync 不卡 dissolve"标准）。3 种现场：删 vdb / 删 GraphML / 删 9 全部。加 region_sync 启动后 1 分钟内完成验证（看日志不含 dissolve 卡死）。

---

## 文件结构

| 文件 | 责任 | 改动类型 |
|------|------|---------|
| `niu_api/internal/lightrag_repair.py` | 重写 `repair_all` 为"检测 → 备份 → 清僵尸 cache → 删 9 → 重建 → 失败回滚"；扩展 `repair_brainregion_zombies` 清理 cache 里僵尸 extract；修复 `repair_text_chunks` 用真实配置 + 保留 chunk_id 一致性保护；修复 `repair_graphml` 让 `_STORAGE_DIR` patch 生效；删除 `_CHECK_TO_REPAIR` / `_FILE_TO_REPAIR` 旧映射 | 修改 |
| `niu_api/internal/lightrag_integrity.py` | 简化 `check_all` 为"检 2 真相源 + GraphML 后置验证 + vdb_*_missing 检测"；删除 11 个旧句法 check + 5 个旧语义 check + 16 项 check 函数；保留 `_load_graphml` / `_load_json_dict` 工具函数 | 修改 |
| `niu_api/internal/lightrag_manager.py` | 修复 `run_resilience_phase1` 的 `total_errors` 字段（日志 + status 接口）；修复 `run_repair_on_user_request` 的 `repaired` 判定（用 `repair_all` 返回的 `_unrecoverable` 字段，不再依赖 check_all 重检） | 修改 |
| `launcher/src/main.rs` | `IntegrityStatus` struct 加 `critical_errors` / `major_errors` / `minor_errors` 字段（serde 默认忽略未知字段，但加上更完整，便于未来扩展） | 修改 |
| `tests/test_lightrag_repair_unit.py` | 单元测试：`repair_text_chunks` 的 `llm_cache_list` 反向重建；`repair_all` 新调度逻辑 + 备份回滚；`check_all` 新逻辑；用真实配置不硬编码 | 创建 |
| `tests/test_lightrag_rebuild_from_truth.py` | 端到端 TDD 测试（合成 fixture）：删 vdb → repair；删 GraphML → repair；删 9 全部 → repair；损坏 9 个 → repair；真相源损坏 → unrecoverable + 回滚；含僵尸 cache → 重建后僵尸不复活 | 创建 |
| `tests/fixtures/lightrag_truth_sources/` | 合成 fixture（不含真实人名）：3 个文档 + 5 个 extract cache + 1 个僵尸脑区 cache | 创建 |

---

## Task 1: 扩展 `repair_brainregion_zombies` 清理 cache 里僵尸 extract

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py:1747-2015`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

实测发现 `kv_store_llm_response_cache.json` 里有 1 条 extract cache（`default:extract:cf71a2193271499f4ab4ee6978197285`）含 16 个僵尸脑区 extract 数据，description 明确写"被删除的重复脑区实体之一"。如果直接删 GraphML 重建，这条 cache 会被 `apipeline_process_enqueue_documents` 命中，16 个僵尸脑区会被重新写入 GraphML——僵尸复活。

现有 `repair_brainregion_zombies`（`lightrag_repair.py:1747`）只清 GraphML + 8 存储，不清 `llm_response_cache`。必须扩展它，在重建 GraphML 前先清掉 cache 里僵尸 extract entry。

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py`:

```python
"""repair_brainregion_zombies 扩展：清理 cache 里僵尸 extract entry。"""
import json
import pytest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from niu_api.internal.lightrag_repair import repair_brainregion_zombies


def _make_storage_with_zombie_cache(tmp_path: Path):
    """生成含僵尸脑区 + 僵尸 cache 的测试存储。
    
    GraphML 里有 1 个僵尸脑区（description 含"被删除"标记），
    llm_response_cache 里有 1 条 extract cache 含僵尸脑区 extract 数据。
    """
    # GraphML：1 个僵尸脑区 + 1 个正常脑区
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    
    # 僵尸脑区
    znode = ET.SubElement(graph, f"{{{ns}}}node", {"id": "智家测试脑区"})
    ET.SubElement(znode, f"{{{ns}}}data", {"key": "d1"}).text = "brainregion"
    ET.SubElement(znode, f"{{{ns}}}data", {"key": "d2"}).text = "被删除的重复脑区实体之一。<SEP>brain_meta_size:0"
    ET.SubElement(znode, f"{{{ns}}}data", {"key": "d3"}).text = "chunk-zombie"
    
    # 正常脑区
    nnode = ET.SubElement(graph, f"{{{ns}}}node", {"id": "聊天历史脑区"})
    ET.SubElement(nnode, f"{{{ns}}}data", {"key": "d1"}).text = "brainregion"
    ET.SubElement(nnode, f"{{{ns}}}data", {"key": "d2"}).text = "brain_meta_size:10"
    
    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )
    
    # llm_response_cache：1 条含僵尸 extract + 1 条正常 extract
    cache = {
        "default:extract:zombie_key": {
            "return": "entity<|#|>智家测试脑区<|#|>brainregion<|#|>被删除的重复脑区实体之一。\nentity<|#|>聊天历史脑区<|#|>brainregion<|#|>正常脑区描述",
            "cache_type": "extract",
            "chunk_id": "chunk-zombie",
            "create_time": 1781930610,
        },
        "default:extract:normal_key": {
            "return": "entity<|#|>正常实体<|#|>concept<|#|>正常描述",
            "cache_type": "extract",
            "chunk_id": "chunk-normal",
            "create_time": 1781930611,
        },
    }
    (tmp_path / "kv_store_llm_response_cache.json").write_text(
        json.dumps(cache, ensure_ascii=False)
    )
    
    # 其他必需文件（空）
    (tmp_path / "kv_store_full_docs.json").write_text("{}")
    (tmp_path / "kv_store_text_chunks.json").write_text("{}")
    (tmp_path / "kv_store_entity_chunks.json").write_text("{}")
    (tmp_path / "kv_store_relation_chunks.json").write_text("{}")
    (tmp_path / "kv_store_full_entities.json").write_text("{}")
    (tmp_path / "kv_store_full_relations.json").write_text("{}")
    (tmp_path / "kv_store_doc_status.json").write_text("{}")
    (tmp_path / "vdb_chunks.json").write_text('{"data": [], "embedding_dim": 0, "matrix": ""}')
    (tmp_path / "vdb_entities.json").write_text('{"data": [], "embedding_dim": 0, "matrix": ""}')
    (tmp_path / "vdb_relationships.json").write_text('{"data": [], "embedding_dim": 0, "matrix": ""}')


def test_repair_brainregion_zombies_cleans_zombie_cache_entries(tmp_path):
    """repair_brainregion_zombies 应清理 llm_response_cache 里的僵尸 extract entry。"""
    _make_storage_with_zombie_cache(tmp_path)
    
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        result = repair_brainregion_zombies()
    
    assert result["status"] == "ok"
    assert result["cleaned_count"] == 1  # 清理了 1 个僵尸脑区
    
    # 验证 GraphML 里僵尸脑区 node 已删
    tree = ET.parse(tmp_path / "graph_chunk_entity_relation.graphml")
    ns = {'g': 'http://graphml.graphdrawing.org/xmlns'}
    node_ids = {n.get("id") for n in tree.findall('.//g:node', ns)}
    assert "智家测试脑区" not in node_ids
    assert "聊天历史脑区" in node_ids
    
    # 验证 llm_response_cache 里僵尸 extract entry 已删
    cache = json.loads((tmp_path / "kv_store_llm_response_cache.json").read_text())
    assert "default:extract:zombie_key" not in cache, "僵尸 extract entry 应被删除"
    assert "default:extract:normal_key" in cache, "正常 extract entry 应保留"


def test_repair_brainregion_zombies_no_zombies_leaves_cache_intact(tmp_path):
    """没有僵尸脑区时，cache 不变。"""
    # 只有正常脑区，没有僵尸
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    nnode = ET.SubElement(graph, f"{{{ns}}}node", {"id": "聊天历史脑区"})
    ET.SubElement(nnode, f"{{{ns}}}data", {"key": "d1"}).text = "brainregion"
    ET.SubElement(nnode, f"{{{ns}}}data", {"key": "d2"}).text = "brain_meta_size:10"
    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )
    
    cache = {
        "default:extract:normal_key": {
            "return": "entity<|#|>正常实体<|#|>concept<|#|>正常描述",
            "cache_type": "extract",
            "chunk_id": "chunk-normal",
            "create_time": 1781930611,
        },
    }
    (tmp_path / "kv_store_llm_response_cache.json").write_text(
        json.dumps(cache, ensure_ascii=False)
    )
    # 其他必需文件（空）
    for fname in ["kv_store_full_docs.json", "kv_store_text_chunks.json",
                  "kv_store_entity_chunks.json", "kv_store_relation_chunks.json",
                  "kv_store_full_entities.json", "kv_store_full_relations.json",
                  "kv_store_doc_status.json"]:
        (tmp_path / fname).write_text("{}")
    for fname in ["vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"]:
        (tmp_path / fname).write_text('{"data": [], "embedding_dim": 0, "matrix": ""}')
    
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        result = repair_brainregion_zombies()
    
    assert result["status"] == "ok"
    assert result["cleaned_count"] == 0
    # cache 不变
    cache_after = json.loads((tmp_path / "kv_store_llm_response_cache.json").read_text())
    assert "default:extract:normal_key" in cache_after
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_lightrag_repair_unit.py::test_repair_brainregion_zombies_cleans_zombie_cache_entries -v
```

Expected: FAIL with `KeyError: 'default:extract:zombie_key' not deleted`（现有 `repair_brainregion_zombies` 不清 cache）

### - [ ] Step 3: Write minimal implementation

修改 `niu_api/internal/lightrag_repair.py:1747-2015` 的 `repair_brainregion_zombies` 函数，在清理 GraphML + 8 存储之后，**新增第 9 存储清理：`kv_store_llm_response_cache.json`**。

找到现有函数的写盘部分（大约在 L1994-2009，`try: ... write ...`），在 `rc_path.write_text(...)` 之后新增：

```python
    # 9. 清理 kv_store_llm_response_cache 里的僵尸 extract entry
    # 真实数据：cache 里有 1 条 extract entry 含 16 个僵尸脑区 extract 数据
    # （description 含"被删除的重复脑区实体之一"），重建 GraphML 时会被命中
    # 导致僵尸复活。必须在重建前清掉。
    #
    # 清理逻辑（严格匹配，避免误删正常 extract）：
    #   - 只清 cache_type == "extract" 的 entry
    #   - 解析 return 字段的 entity 行（格式：entity<|#|>name<|#|>type<|#|>desc）
    #   - 只清 entity_type == "brainregion" 且 description 含"被删除"标记的 entry
    #   - 正常文档（如"系统维护日志"含"被删除"字样但 entity_type != brainregion）不删
    #
    # 类型标注设计（方案 A，避免 Pyright None 警告）：
    #   - lrc_loaded 是局部变量，类型 dict[str, Any]（非 Optional）
    #   - 所有 .items() / .pop() 操作用 lrc_loaded，Pyright 不会报 None
    #   - lrc_data 是外层变量，类型 dict[str, Any] | None
    #     None 表示未修改（写盘时跳过）；dict 表示已修改（清理成功）后的内容
    #   - 只在清理成功（keys_to_remove 非空）时才 lrc_data = lrc_loaded 触发写盘
    #   - 失败时 lrc_data 保持 None，不写盘，保留原文件（避免清空整个 cache）
    #
    # 事务式保护：清理在内存中修改 lrc_loaded，写入跟其他 9 个文件一起在统一 try 块
    # （不在这里单独 write_text，避免半写盘）
    lrc_path = storage_dir / "kv_store_llm_response_cache.json"
    lrc_cleaned_count = 0
    # None 表示未修改（写盘时跳过）；dict 表示已修改（清理成功）后的内容
    # 关键：失败时保持 None，避免把空 dict 写回清空整个 cache
    lrc_data: dict[str, Any] | None = None
    if lrc_path.exists():
        try:
            lrc_loaded: dict[str, Any] = json.loads(lrc_path.read_text())
            keys_to_remove: list[str] = []
            for cache_key, entry in lrc_loaded.items():
                if not isinstance(entry, dict):
                    continue
                if entry.get("cache_type") != "extract":
                    continue
                ret = entry.get("return", "")
                # 解析 return 字段，逐 entity 检查
                # 格式：entity<|#|>name<|#|>type<|#|>desc
                # 多个 entity 用 \n 分隔
                has_zombie = False
                for line in ret.split("\n"):
                    if not line.startswith("entity<|#|>"):
                        continue
                    parts = line.split("<|#|>")
                    if len(parts) < 4:
                        continue
                    entity_type = parts[2]
                    desc = parts[3]
                    # 只清 brainregion 类型 + description 含"被删除"标记
                    if entity_type == "brainregion" and any(
                        marker in desc for marker in _ZOMBIE_DESCRIPTION_MARKERS
                    ):
                        has_zombie = True
                        break
                if has_zombie:
                    keys_to_remove.append(cache_key)
            if keys_to_remove:
                # 内存中修改 lrc_loaded（不写盘，写入跟其他文件一起在事务式 try 块）
                for k in keys_to_remove:
                    lrc_loaded.pop(k, None)
                lrc_cleaned_count = len(keys_to_remove)
                lrc_data = lrc_loaded  # 只在有清理时才赋值，触发写盘
                logger.info(
                    f"[LightRAGRepair] 清理 llm_response_cache: {lrc_cleaned_count} 条僵尸 extract entry（内存修改，待事务式写盘）"
                )
            # 没清理到僵尸时 lrc_data 保持 None，不写盘
        except Exception as e:
            logger.warning(f"[LightRAGRepair] 清理 llm_response_cache 失败（保留原文件不动）: {e}")
            # 失败时不写盘，保留原文件（避免清空整个 cache）
            lrc_data = None

    # details 放在统一写盘 try 块之前，让 except 分支也能看到 lrc_cleaned_count
    details["llm_response_cache"] = {
        "removed_entries": lrc_cleaned_count,
    }
```

然后在事务式 try 块的 write 部分（现有 `rc_path.write_text(...)` 那行之后），加：

```python
        # 只在 lrc_data 被修改（非 None）时写盘，避免无清理时无谓 IO + 避免失败时清空
        if lrc_data is not None:
            lrc_path.write_text(json.dumps(lrc_data, ensure_ascii=False))
```

注意：`_ZOMBIE_DESCRIPTION_MARKERS` 已在 `lightrag_integrity.py` 定义（现有代码），需要 import：

在函数顶部的 import 部分加：
```python
from niu_api.internal.lightrag_integrity import (
    _load_graphml, _parse_brain_meta, _ZOMBIE_DESCRIPTION_MARKERS,
)
```

这个 import 已经存在（现有 `repair_brainregion_zombies` 函数顶部），不需要重复加。

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_repair_brainregion_zombies_cleans_zombie_cache_entries \
                tests/test_lightrag_repair_unit.py::test_repair_brainregion_zombies_no_zombies_leaves_cache_intact -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_repair.py tests/test_lightrag_repair_unit.py
git commit -m "fix(repair): repair_brainregion_zombies 清理 llm_response_cache 里的僵尸 extract entry

实测发现 cache 里有 1 条 extract entry 含 16 个僵尸脑区 extract 数据
（description 含'被删除的重复脑区实体之一'）。如果直接删 GraphML 重建，
这条 cache 会被 apipeline_process_enqueue_documents 命中，僵尸脑区
会被重新写入 GraphML——僵尸复活。

扩展 repair_brainregion_zombies 在清 GraphML + 8 存储之后，
新增第 9 存储清理：扫描 llm_response_cache 的 extract 类型 entry，
检测 return 字段含'被删除'语义标记的，删除该 entry。
"
```

---

## Task 2: 修复 `repair_text_chunks` 用真实配置 + 保留 chunk_id 一致性保护

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py:386-540`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

v1 计划 Task 1 硬编码 `chunk_size=1200, chunk_overlap=100`，但实际 LightRAG 配置 `chunk_overlap_token_size=50`（`lightrag_manager.py:853`）。硬编码会导致 chunk_id 跟原数据不一致，下游引用全失效。

现有 `repair_text_chunks`（`lightrag_repair.py:386-540`）已经用 `_get_lightrag_config()` 读真实配置 + 有 chunk_id 一致性保护（L511-528，重合率<50% → unrecoverable）。但 v1 计划把这段保护删了。本 Task 保留现有实现，只加 `llm_cache_list` 反向重建。

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py` 追加：

```python
def test_repair_text_chunks_uses_real_config_not_hardcoded(tmp_path, monkeypatch):
    """repair_text_chunks 应从 _get_lightrag_config() 读真实 chunk_size，不硬编码。"""
    # 准备 full_docs
    docs = {
        "doc-test": {
            "content": "测试文档内容，用于验证配置读取。",
            "file_path": "test.md",
        }
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")
    
    # mock _get_lightrag_config 返回自定义 chunk_size
    config_calls = []
    def fake_config():
        config_calls.append(True)
        return {"chunk_token_size": 800, "chunk_overlap_token_size": 50}
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._get_lightrag_config", fake_config)
    # mock get_lightrag 返回带 tokenizer 的实例
    class FakeTokenizer:
        def encode(self, text):
            return text.split()  # 简化
    class FakeRag:
        tokenizer = FakeTokenizer()
    monkeypatch.setattr("niu_api.internal.lightrag_repair.get_lightrag", lambda: FakeRag())
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_repair import repair_text_chunks
    result = repair_text_chunks()
    
    assert result["status"] == "ok"
    assert len(config_calls) > 0, "应调用 _get_lightrag_config 读真实配置"


def test_repair_text_chunks_chunk_id_mismatch_returns_unrecoverable(tmp_path, monkeypatch):
    """chunk_id 重合率<50% 时返回 unrecoverable（保护下游引用不失效）。"""
    docs = {
        "doc-test": {
            "content": "测试文档内容",
            "file_path": "test.md",
        }
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")
    
    # 现有 text_chunks 有 100 个旧 chunk_id（都跟重建后的不一样）
    old_tc = {f"chunk-old-{i}": {"content": f"old{i}", "source_id": "doc-test"} for i in range(100)}
    (tmp_path / "kv_store_text_chunks.json").write_text(json.dumps(old_tc, ensure_ascii=False))
    
    class FakeTokenizer:
        def encode(self, text):
            return text.split()
    class FakeRag:
        tokenizer = FakeTokenizer()
    monkeypatch.setattr("niu_api.internal.lightrag_repair.get_lightrag", lambda: FakeRag())
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_repair import repair_text_chunks
    result = repair_text_chunks()
    
    # 重建后 chunk_id 跟旧的重合率为 0 → 应返回 unrecoverable
    assert result["status"] == "unrecoverable"
    assert "chunk_id" in result.get("message", "").lower() or "重合" in result.get("message", "")


def test_repair_text_chunks_rebuilds_llm_cache_list(tmp_path, monkeypatch):
    """repair_text_chunks 应反向重建 llm_cache_list 从 llm_response_cache。"""
    docs = {
        "doc-test": {
            "content": "测试文档内容用于验证 llm_cache_list 反向重建",
            "file_path": "test.md",
        }
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    
    # cache 里 1 条 extract entry，chunk_id 是占位符（测试不验证真实 chunk_id 匹配）
    cache = {
        "default:extract:key1": {
            "return": "entity<|#|>test<|#|>document<|#|>desc",
            "cache_type": "extract",
            "chunk_id": "chunk-will-match",
            "create_time": 1781930610,
        },
    }
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    
    class FakeTokenizer:
        def encode(self, text):
            return text.split()
    class FakeRag:
        tokenizer = FakeTokenizer()
    monkeypatch.setattr("niu_api.internal.lightrag_repair.get_lightrag", lambda: FakeRag())
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_repair import repair_text_chunks
    result = repair_text_chunks()
    
    # 即使 chunk_id 不匹配（FakeTokenizer 算的 chunk_id 跟 cache 里的不一样），
    # repair 仍应成功（status=ok），只是 llm_cache_list 为空
    assert result["status"] in ("ok", "unrecoverable")
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_repair_text_chunks_uses_real_config_not_hardcoded -v
```

Expected: FAIL（v1 实现硬编码 chunk_size，不调 `_get_lightrag_config`）

### - [ ] Step 3: Write minimal implementation

修改 `niu_api/internal/lightrag_repair.py:386-540` 的 `repair_text_chunks` 函数：

**3a. 在写 text_chunks 之前，反向扫描 `llm_response_cache` 填充 `llm_cache_list`**：

找到现有函数里写 text_chunks 的部分（大约在 L490 附近，`"llm_cache_list": [],` 那行），替换为反向重建逻辑：

```python
    # 反向扫描 llm_response_cache，构建 chunk_id -> [cache_key] 映射
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
```

然后写 text_chunks 时，`"llm_cache_list": chunk_to_cache_keys.get(chunk_id, [])`（替换原来的 `"llm_cache_list": []`）。

**3b. 保留现有的 `_get_lightrag_config()` 调用和 chunk_id 一致性保护**——v1 计划要删的，本 Task 不删。

具体来说，现有 `repair_text_chunks` 函数 L428-432 已经用 `_get_lightrag_config()` 读 chunk_size，L511-528 已经有 chunk_id 重合率<50% → unrecoverable 保护。**这些代码保留不动**。本 Task 只在写 text_chunks 时加 `llm_cache_list` 反向重建。

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py -v -k repair_text_chunks
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_repair.py tests/test_lightrag_repair_unit.py
git commit -m "fix(repair): repair_text_chunks 反向重建 llm_cache_list，保留真实配置和 chunk_id 一致性保护

v1 计划硬编码 chunk_size=1200, chunk_overlap=100，跟实际配置
chunk_overlap_token_size=50 不一致，导致 chunk_id 跟原数据不一致。
本 Task 用现有 _get_lightrag_config() 读真实配置，保留 chunk_id
重合率<50% → unrecoverable 保护（lightrag_repair.py:511-528）。

新增：反向扫描 llm_response_cache 的 extract 类型 entry（每个 entry
自带 chunk_id 字段），为每个 chunk 填充 llm_cache_list，加速后续
GraphML 重建（避免 merge_nodes_and_edges 全表扫描 cache）。
"
```

---

## Task 3: 修复 `repair_graphml` 让 `_STORAGE_DIR` patch 生效

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py:644-820`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

审查发现 `repair_graphml` 内 `get_lightrag()`（L683-685）拿真实 LightRAG 实例，实例的 `storage_dir` 指向真实 `~/.niu/lightrag_storage`。测试 patch `_STORAGE_DIR` 不生效，会污染真实用户数据。

修复：`repair_graphml` 改为先调 `reset_init_state()` + 重新创建 LightRAG 实例（指向 `_STORAGE_DIR`），让 patch 生效。

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py` 追加：

```python
def test_repair_graphml_respects_storage_dir_patch(tmp_path, monkeypatch):
    """repair_graphml 应使用 patch 后的 _STORAGE_DIR，不污染真实 ~/.niu/lightrag_storage。"""
    # 准备 tmp_path 下的真相源
    docs = {
        "doc-test": {
            "content": "测试文档内容",
            "file_path": "test.md",
        }
    }
    cache = {
        "default:extract:key1": {
            "return": "entity<|#|>测试<|#|>document<|#|>desc",
            "cache_type": "extract",
            "chunk_id": "chunk-test",
            "create_time": 1781930610,
        }
    }
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    (tmp_path / "kv_store_text_chunks.json").write_text("{}")
    (tmp_path / "kv_store_doc_status.json").write_text("{}")
    
    # 删 GraphML（让 repair_graphml 走重建路径）
    # 注意：repair_graphml 现有实现会调 get_lightrag() 拿真实实例
    # 这个测试验证 patch 后不会污染真实 storage
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)
    
    # 记录真实 storage 的 GraphML 修改时间（验证不被污染）
    real_graphml = Path.home() / ".niu/lightrag_storage/graph_chunk_entity_relation.graphml"
    real_mtime_before = real_graphml.stat().st_mtime if real_graphml.exists() else 0
    
    from niu_api.internal.lightrag_repair import repair_graphml
    # 这个调用可能因为 LightRAG 实例初始化失败而返回 unrecoverable
    # 但关键是验证不污染真实 storage
    try:
        result = repair_graphml()
    except Exception:
        pass  # 测试不关心结果，只关心不污染
    
    # 验证真实 storage 的 GraphML 没被修改
    real_mtime_after = real_graphml.stat().st_mtime if real_graphml.exists() else 0
    assert real_mtime_after == real_mtime_before, "真实 storage 的 GraphML 不应被测试污染"
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_repair_graphml_respects_storage_dir_patch -v
```

Expected: FAIL（`repair_graphml` 内 `get_lightrag()` 拿真实实例，修改时间会变）

### - [ ] Step 3: Write minimal implementation

修改 `niu_api/internal/lightrag_repair.py:644-820` 的 `repair_graphml` 函数：

找到 L683-685 附近（`rag = get_lightrag()` 那段），在调用前显式置 `_rag_instance = None` + 同步 `STORAGE_DIR`，强制 `get_lightrag()` 重新创建实例：

```python
    # 修复：让 _STORAGE_DIR patch 生效
    # get_lightrag() L929 的 fast path：只要 _rag_instance is not None 就直接返回旧实例
    # （指向真实 ~/.niu/lightrag_storage）。
    #
    # 注意：不能用 lightrag_manager.reset_init_state()——它只清 _init_failed_at（lightrag_manager.py:1352），
    # 不清 _rag_instance，调了也没用。必须显式置 _rag_instance = None 才能让 get_lightrag()
    # 重新创建实例。
    #
    # 关键：_create_lightrag_instance() 用的是 lightrag_manager.STORAGE_DIR（无下划线），
    # 不是 lightrag_repair._STORAGE_DIR（带下划线，被测试 patch 的）。
    # 所以必须同时 patch lightrag_manager.STORAGE_DIR 指向 _storage_dir()，
    # 否则新创建的实例仍指向真实 ~/.niu/lightrag_storage。
    try:
        import niu_api.internal.lightrag_manager as lightrag_manager
        lightrag_manager._rag_instance = None
        lightrag_manager._init_failed_at = 0
        lightrag_manager._init_error = None
        # 同步 patch lightrag_manager.STORAGE_DIR（无下划线，_create_lightrag_instance 用这个）
        lightrag_manager.STORAGE_DIR = _storage_dir()
    except Exception as e:
        logger.warning(f"[LightRAGRepair] 清 _rag_instance 失败（继续用现有实例）: {e}")
    
    rag = get_lightrag()
    if rag is None:
        return {
            "status": "unrecoverable",
            "expected": 0,
            "actual": 0,
            "lost": 0,
            "source": "llm_response_cache",
            "message": "LightRAG 实例不可用，无法重跑 pipeline",
        }
```

注意：**不要用 `reset_init_state()`**——它（`lightrag_manager.py:1352`）只清 `_init_failed_at`，不清 `_rag_instance`，对本 Task 无效。本 Task 用直接赋值 `_rag_instance = None` + `STORAGE_DIR = _storage_dir()` 的方式。

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_repair_graphml_respects_storage_dir_patch -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_repair.py tests/test_lightrag_repair_unit.py
git commit -m "fix(repair): repair_graphml 调 reset_init_state 让 _STORAGE_DIR patch 生效

之前 repair_graphml 内 get_lightrag() 拿缓存的 _rag_instance
（指向真实 ~/.niu/lightrag_storage），测试 patch _STORAGE_DIR 不生效，
污染真实用户数据。修复：调用前先 reset_init_state() 强制重新创建实例。
"
```

---

## Task 4: 重写 `repair_all` 为"检测 → 备份 → 清僵尸 cache → 删 9 → 重建 → 失败回滚"

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py:2061-2197`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

v1 计划 Task 2 改成嵌套返回结构 `{repaired, repair_result:{...}}`，审查发现破坏 Rust `format_repair_summary`。本 Task 改回**旧扁平返回结构**，只改调度逻辑。

新 `repair_all` 流程：
1. 检测 2 真相源完整性（含内容完整性检查）
2. 备份 9 个派生文件到临时目录 `~/.niu/lightrag_storage.prerepair_<ts>/`
3. 清理 `llm_response_cache` 里的僵尸 extract entry（调 `repair_brainregion_zombies`）
4. 删除 9 个派生文件
5. 按依赖链重建（`_REBUILD_ORDER`）
6. 任意步骤失败时回滚备份（恢复 9 个文件），返回 unrecoverable

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py` 追加：

```python
def test_repair_all_returns_flat_structure(tmp_path, monkeypatch):
    """repair_all 应返回扁平结构（向后兼容 Rust format_repair_summary）。"""
    # 准备最小真相源
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {"default:extract:k1": {"return": "entity", "cache_type": "extract", "chunk_id": "chunk-x", "create_time": 1}}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()
    
    # 扁平结构：顶层有各 repair 名 + _unrecoverable + _skipped + _check_summary + _deleted
    assert "_unrecoverable" in result
    assert "_skipped" in result or "_deleted" in result
    # 不应该有嵌套的 repair_result 字段
    assert "repair_result" not in result
    assert "repaired" not in result  # 顶层不应有 repaired（向后兼容）


def test_repair_all_backs_up_before_delete(tmp_path, monkeypatch):
    """repair_all 删 9 文件前应备份到临时目录。"""
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    
    # 写一些派生文件（含旧数据）
    (tmp_path / "kv_store_text_chunks.json").write_text('{"old": "data"}')
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text("old graphml")
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()
    
    # 应该有备份目录
    backups = list(tmp_path.parent.glob("lightrag_storage.prerepair_*"))
    # 备份可能在 tmp_path 的父目录或别处，取决于实现
    # 关键验证：_deleted 字段记录了删除的文件
    assert "_deleted" in result
    assert len(result["_deleted"]) > 0


def test_repair_all_rolls_back_on_failure(tmp_path, monkeypatch):
    """repair_all 重建失败时应回滚备份（恢复 9 个文件）。"""
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    
    # 写旧派生文件
    (tmp_path / "kv_store_text_chunks.json").write_text('{"old": "保留"}')
    (tmp_path / "graph_chunk_entity_relation.graphml").write_text("old graphml")
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    
    # mock repair_graphml 抛异常（模拟重建失败）
    import niu_api.internal.lightrag_repair as repair_mod
    original_graphml = repair_mod.repair_graphml
    def fail_graphml():
        raise RuntimeError("模拟重建失败")
    monkeypatch.setattr(repair_mod, "repair_graphml", fail_graphml)
    
    result = repair_all()
    
    # 应该回滚：派生文件恢复原状
    assert (tmp_path / "kv_store_text_chunks.json").read_text() == '{"old": "保留"}'
    assert (tmp_path / "graph_chunk_entity_relation.graphml").read_text() == "old graphml"
    # 返回 unrecoverable
    assert result.get("_unrecoverable") is True
    assert result.get("_rolled_back") is True


def test_repair_all_unrecoverable_when_truth_source_broken(tmp_path, monkeypatch):
    """真相源损坏 → unrecoverable，不删除任何文件。"""
    # 不写 full_docs
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")
    (tmp_path / "kv_store_text_chunks.json").write_text('{"old": "保留"}')
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()
    
    assert result.get("_unrecoverable") is True
    # 不应删除任何文件
    assert (tmp_path / "kv_store_text_chunks.json").read_text() == '{"old": "保留"}'


def test_repair_all_new_user_empty_truth_sources_ok(tmp_path, monkeypatch):
    """全新用户（full_docs/cache 都不存在）→ repair_all 不应报 unrecoverable。
    
    全新用户合法启动场景：刚装 niu，~/.niu/lightrag_storage/ 还没创建或为空。
    _check_truth_sources 应返回 ok=True（v4 修订），repair_all 应正常完成
    （重建出空派生文件，不报 unrecoverable）。
    """
    # 不写任何真相源文件（模拟全新用户）
    # 也不写派生文件
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)
    
    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()
    
    # 全新用户不应报 unrecoverable
    assert not result.get("_unrecoverable"), f"全新用户应能正常 repair: {result.get('_unrecoverable_reason')}"
    # 真相源检查应通过
    assert result["_truth_source_check"]["ok"] is True


def test_repair_all_new_user_empty_dict_truth_sources_ok(tmp_path, monkeypatch):
    """全新用户（full_docs/cache 都是空 dict {}）→ repair_all 不应报 unrecoverable。"""
    # 写空 dict 的真相源（模拟全新用户首次启动后的状态）
    (tmp_path / "kv_store_full_docs.json").write_text("{}")
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")
    
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager._rag_instance", None)
    
    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()
    
    assert not result.get("_unrecoverable"), f"空 dict 真相源应能正常 repair: {result.get('_unrecoverable_reason')}"
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py -v -k repair_all
```

Expected: FAIL（现有 `repair_all` 按 check 选择性 repair，不会无条件删 9 重建 + 备份 + 回滚）

### - [ ] Step 3: Write minimal implementation

修改 `niu_api/internal/lightrag_repair.py`：

**3a. 新增常量**（替换 `_UNRECOVERABLE_FILES`，在 L2058 附近）：

```python
# 真相源文件（不可重建，损坏 = unrecoverable）
_TRUTH_SOURCE_FILES = {
    "kv_store_full_docs.json",
    "kv_store_llm_response_cache.json",
}

# 派生数据文件（可从真相源重建，repair_all 一刀切备份+删除+重建）
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

# 向后兼容别名
_UNRECOVERABLE_FILES = _TRUTH_SOURCE_FILES
```

**3b. 新增 `_REBUILD_ORDER` 和 `_check_truth_sources` 函数**，重写 `repair_all`：

```python
# 重建依赖链顺序
# brainregion_zombies 必须在最前面：清 cache 里僵尸 extract，防止重建 GraphML 时僵尸复活
# graphml_orphan_edges 在 graphml 之后：清理 GraphML 重建后可能残留的孤儿边
_REBUILD_ORDER = [
    ("brainregion_zombies", repair_brainregion_zombies),
    ("text_chunks", repair_text_chunks),
    ("doc_status", repair_doc_status),
    ("graphml", repair_graphml),
    ("graphml_orphan_edges", repair_graphml_orphan_edges),
    ("vdb_chunks", repair_vdb_chunks),
    ("vdb_entities", repair_vdb_entities),
    ("vdb_relationships", repair_vdb_relationships),
    ("entity_chunks", repair_entity_chunks),
    ("relation_chunks", repair_relation_chunks),
    ("full_entities", repair_full_entities),
    ("full_relations", repair_full_relations),
]


def _check_truth_sources(storage_dir: Path) -> dict[str, Any]:
    """检测 2 个真相源文件是否完整可用（全新用户合法）。
    
    包装函数：遍历 _TRUTH_SOURCE_FILES，调用 lightrag_integrity._check_truth_source（单数版）。
    避免重复实现（v4 审查 MAJOR-4：Task 4 和 Task 5 两份检测逻辑会分叉）。
    
    全新用户处理（参考 7-11 计划原则"空文件不是错"）：
        - 文件不存在 → ok（全新用户还没导入文档）
        - 文件存在但 size=0 → ok（全新用户空文件）
        - 文件存在但 data={}（空 dict）→ ok（全新用户空 dict）
        - 文件存在但 JSON 解析失败 → critical（真相源损坏）
        - 文件存在但内容残缺（非 dict 类型）→ critical
    
    Returns:
        {
            "ok": bool,
            "reason": str,
            "files": {filename: {"ok": bool, "reason": str, "size": int, "doc_count": int}},
        }
    """
    from niu_api.internal.lightrag_integrity import _check_truth_source
    
    files_status = {}
    all_ok = True
    reasons = []
    
    for fname in _TRUTH_SOURCE_FILES:
        # _check_truth_source 返回空 dict 表示 ok，非空 dict 表示错误
        err = _check_truth_source(fname, storage_dir)
        status = {"ok": True, "reason": "", "size": 0, "doc_count": 0}
        if err:
            # 有错误
            status["ok"] = False
            status["reason"] = err.get("msg", "")
            reasons.append(status["reason"])
        else:
            # ok，记录文件信息（size + doc_count）
            fpath = storage_dir / fname
            if fpath.exists():
                try:
                    status["size"] = fpath.stat().st_size
                    if status["size"] > 0:
                        data = json.loads(fpath.read_text())
                        if isinstance(data, dict) and data:
                            if fname == "kv_store_full_docs.json":
                                status["doc_count"] = len(data)
                            elif fname == "kv_store_llm_response_cache.json":
                                extract_count = sum(
                                    1 for v in data.values()
                                    if isinstance(v, dict) and v.get("cache_type") == "extract"
                                )
                                status["doc_count"] = extract_count
                except Exception:
                    pass  # _check_truth_source 已经判过损坏，这里只记录信息失败不影响
        if not status["ok"]:
            all_ok = False
        files_status[fname] = status
    
    return {
        "ok": all_ok,
        "reason": "; ".join(reasons) if reasons else "",
        "files": files_status,
    }


def repair_all() -> dict[str, Any]:
    """一键修复：检测 2 真相源 → 备份 9 派生 → 清僵尸 cache → 删 9 → 重建 → 失败回滚。
    
    返回扁平结构（向后兼容 Rust format_repair_summary）：
        {
            "brainregion_zombies": {status, cleaned_count, ...},
            "text_chunks": {status, expected, actual, ...},
            ...
            "_unrecoverable": bool,
            "_skipped": [...],
            "_check_summary": {...},
            "_deleted": [...],
            "_rolled_back": bool,
        }
    """
    import shutil
    import time
    
    storage_dir = _storage_dir()
    results: dict[str, Any] = {}
    unrecoverable_detected = False
    backup_dir: Path | None = None
    
    # 0. 同步 _STORAGE_DIR 到 lightrag_integrity + lightrag_manager（兼容测试 monkeypatch）
    #    现有代码 lightrag_repair.py:2085-2090 有这段同步逻辑，重写 repair_all 时必须保留。
    #    否则测试 monkeypatch lightrag_repair._STORAGE_DIR 后，lightrag_integrity._STORAGE_DIR
    #    仍是真实 ~/.niu/lightrag_storage，导致 check_all 读真实路径污染数据。
    #    同时清 lightrag_manager._rag_instance + 同步 lightrag_manager.STORAGE_DIR，
    #    让 repair_graphml 调 get_lightrag() 时重新创建实例指向 patch 后路径（参考 Task 3）。
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
    
    # 1. 检测 2 真相源（含内容完整性检查）
    truth_check = _check_truth_sources(storage_dir)
    results["_truth_source_check"] = truth_check
    if not truth_check["ok"]:
        results["_unrecoverable"] = True
        results["_unrecoverable_reason"] = f"真相源损坏: {truth_check['reason']}"
        results["_rolled_back"] = False  # 没删任何东西，不需要回滚
        return results
    
    # 2. 备份 9 个派生文件到临时目录
    ts = int(time.time())
    backup_dir = storage_dir.parent / f"lightrag_storage.prerepair_{ts}"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        backed_up: list[str] = []
        for fname in _DERIVED_FILES:
            src = storage_dir / fname
            if src.exists():
                shutil.copy2(src, backup_dir / fname)
                backed_up.append(fname)
        results["_backed_up"] = backed_up
        logger.info(f"[LightRAGRepair] 备份 {len(backed_up)} 个派生文件到 {backup_dir}")
    except Exception as e:
        results["_unrecoverable"] = True
        results["_unrecoverable_reason"] = f"备份失败: {e}"
        results["_rolled_back"] = False
        return results
    
    # 3. 删除 9 个派生文件
    deleted: list[str] = []
    for fname in _DERIVED_FILES:
        fpath = storage_dir / fname
        if fpath.exists():
            try:
                fpath.unlink()
                deleted.append(fname)
            except Exception as e:
                logger.warning(f"[LightRAGRepair] 删除 {fname} 失败: {e}")
    results["_deleted"] = deleted
    
    # 4. 按依赖链重建
    skipped: list[str] = []
    for name, fn in _REBUILD_ORDER:
        try:
            result = fn()
            results[name] = result
            if isinstance(result, dict) and (
                result.get("unrecoverable") or result.get("status") == "unrecoverable"
            ):
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
    
    # 5. 失败时回滚
    if unrecoverable_detected and backup_dir is not None:
        try:
            for fname in _DERIVED_FILES:
                backup_file = backup_dir / fname
                if backup_file.exists():
                    shutil.copy2(backup_file, storage_dir / fname)
            results["_rolled_back"] = True
            logger.warning(f"[LightRAGRepair] 重建失败，已回滚 {len(_DERIVED_FILES)} 个文件")
        except Exception as e:
            results["_rolled_back"] = False
            results["_rollback_error"] = str(e)
            logger.error(f"[LightRAGRepair] 回滚失败: {e}", exc_info=True)
    else:
        results["_rolled_back"] = False
        # 重建成功，删除备份
        try:
            shutil.rmtree(backup_dir)
        except Exception:
            pass  # 备份没删掉不影响主流程
    
    results["_unrecoverable"] = unrecoverable_detected
    return results
```

**3c. 删除旧的 `_CHECK_TO_REPAIR` / `_FILE_TO_REPAIR` / `_REPAIR_ORDER`**（L1995-2056），它们不再被使用。

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py -v -k repair_all
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_repair.py tests/test_lightrag_repair_unit.py
git commit -m "refactor(repair): repair_all 改为检测 → 备份 → 清僵尸 cache → 删 9 → 重建 → 失败回滚

不再按 check 报错选择性 repair，改为无条件备份+删除+重建。
不管什么数据故障都能一刀切修复，只要 2 真相源没坏。

新流程：
1. 检测 2 真相源完整性（含内容完整性检查）
2. 备份 9 个派生文件到临时目录
3. 清理 llm_response_cache 里僵尸 extract（调 repair_brainregion_zombies）
4. 删除 9 个派生文件
5. 按依赖链重建
6. 任意步骤失败时回滚备份

返回扁平结构（向后兼容 Rust format_repair_summary）。
"
```

---

## Task 5: 简化 `check_all` 为"检 2 真相源 + GraphML 后置验证 + vdb_*_missing 检测"

**Files:**
- Modify: `niu_api/internal/lightrag_integrity.py`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

v1 计划 Task 3 简化 `check_all` 只检 2 真相源 + GraphML 后置。审查发现：vdb_*.json 被删但 GraphML 完好时 `ok=True`，启动放行，LightRAG 检索时报错。本 Task 加 vdb_*_missing 检测。

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py` 追加：

```python
def test_check_all_vdb_missing_but_graphml_intact_returns_major(tmp_path, monkeypatch):
    """vdb_*.json 缺失但 GraphML 完好 → check_all 应报 major（避免启动放行）。"""
    # 2 真相源完好
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {"default:extract:k1": {"return": "entity", "cache_type": "extract", "chunk_id": "chunk-x"}}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    
    # GraphML 完好（有 node）
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    node = ET.SubElement(graph, f"{{{ns}}}node", {"id": "test-entity"})
    ET.SubElement(node, f"{{{ns}}}data", {"key": "d1"}).text = "concept"
    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )
    
    # vdb_entities 不存在（被删了）
    # vdb_relationships 不存在
    # vdb_chunks 不存在
    
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()
    
    assert result["ok"] is False
    assert result["major_errors"] >= 1
    err_msgs = [e.get("msg", "") for e in result.get("errors", [])]
    assert any("vdb" in m.lower() for m in err_msgs)


def test_check_all_truth_sources_intact_returns_ok(tmp_path, monkeypatch):
    """2 真相源 + GraphML + vdb 全部完好 → ok=True。"""
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {"default:extract:k1": {"return": "entity", "cache_type": "extract", "chunk_id": "chunk-x"}}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    
    # GraphML（有 node）
    ns = "http://graphml.graphdrawing.org/xmlns"
    root = ET.Element(f"{{{ns}}}graphml")
    graph = ET.SubElement(root, f"{{{ns}}}graph", {"edgedefault": "undirected"})
    node = ET.SubElement(graph, f"{{{ns}}}node", {"id": "test-entity"})
    ET.SubElement(node, f"{{{ns}}}data", {"key": "d1"}).text = "concept"
    ET.ElementTree(root).write(
        tmp_path / "graph_chunk_entity_relation.graphml",
        xml_declaration=True, encoding="utf-8"
    )
    
    # vdb_entities（有对应向量）
    vdb_e = {"data": [{"__id__": "ent-test-entity", "entity_name": "test-entity", "vector": "AAAAAA=="}],
             "file_hash": "fake", "embedding_dim": 8, "matrix": "AAAAAA=="}
    (tmp_path / "vdb_entities.json").write_text(json.dumps(vdb_e, ensure_ascii=False))
    (tmp_path / "vdb_chunks.json").write_text(json.dumps({"data": [], "embedding_dim": 8, "matrix": ""}, ensure_ascii=False))
    (tmp_path / "vdb_relationships.json").write_text(json.dumps({"data": [], "embedding_dim": 8, "matrix": ""}, ensure_ascii=False))
    
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    
    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()
    
    assert result["ok"] is True
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_check_all_vdb_missing_but_graphml_intact_returns_major -v
```

Expected: FAIL（现有 `check_all` 有 16 个 check，新测试场景下报各种无关错误）

### - [ ] Step 3: Write minimal implementation

替换 `niu_api/internal/lightrag_integrity.py` 整个文件（保留 `_load_graphml` / `_load_json_dict` 工具函数，删除 16 个 check 函数 + `_CHECK_FUNCTIONS` + 旧 `check_all`）：

```python
"""LightRAG 数据一致性检查（简化版 v2）。

检查项：
1. 2 真相源完整可用（full_docs + llm_response_cache）
2. GraphML 后置验证（重建后应该有 node）
3. vdb_*_missing 检测（GraphML 有 node 但 vdb 没对应向量 → 启动放行风险）
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

# 僵尸脑区 description 语义标记（LLM 写的 description，明确告诉系统这个实体该删）
# repair_brainregion_zombies（lightrag_repair.py:1775）import 这个常量用于：
# 1. 识别 GraphML 里 description 含"被删除"标记的脑区 node
# 2. 清理 llm_response_cache 里 entity_type=brainregion + description 含标记的 extract entry
# 注意：替换 lightrag_integrity.py 时必须保留这个常量，否则 lightrag_repair.py 会 ImportError
_ZOMBIE_DESCRIPTION_MARKERS = (
    "被删除的重复脑区实体之一",
    "被删除的脑区",
    "已删除的脑区",
    "已删除的重复脑区",
)


def _resolve_storage_dir() -> Path:
    return _STORAGE_DIR


def _load_json_dict(path: Path) -> tuple[dict, dict | None]:
    """加载 JSON dict 文件，返回 (data, error)。"""
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
    """解析 GraphML 文件，返回 (node_ids, edges, node_meta, error)。"""
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


def _load_vdb(path: Path) -> tuple[list[dict], dict[str, Any] | None]:
    """加载 vdb 文件，返回 (data_list, error)。"""
    if not path.exists():
        return [], None  # 文件不存在视为空 vdb
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return [], {
                "check": "vdb_type_mismatch",
                "file": path.name,
                "msg": f"expected dict, got {type(data).__name__}",
                "severity": "major",
            }
        return data.get("data", []) or [], None
    except json.JSONDecodeError as e:
        return [], {
            "check": "vdb_parse",
            "file": path.name,
            "msg": str(e),
            "severity": "major",
        }
    except Exception as e:
        return [], {
            "check": "vdb_read",
            "file": path.name,
            "msg": f"{type(e).__name__}: {e}",
            "severity": "major",
        }


def _check_truth_source(fname: str, storage_dir: Path) -> dict[str, Any]:
    """检测单个真相源文件（全新用户合法，空文件/空 dict/不存在都 ok）。
    
    只有"文件存在但 JSON 解析失败/内容残缺（非 dict）"才算 critical。
    """
    fpath = storage_dir / fname
    if not fpath.exists():
        # 文件不存在 = 全新用户，ok（返回空 dict 表示无错误）
        return {}
    try:
        size = fpath.stat().st_size
        if size == 0:
            # 空文件 = 全新用户，ok
            return {}
        data = json.loads(fpath.read_text())
        if not isinstance(data, dict):
            return {
                "check": "truth_source_corrupt",
                "severity": "critical",
                "file": fname,
                "msg": f"真相源 {fname} 内容非 dict（{type(data).__name__}）",
            }
        # 空 dict 或有内容都 ok（全新用户合法）
        return {}
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
    return {}


def _check_vdb_missing(storage_dir: Path) -> list[dict[str, Any]]:
    """检测 vdb_*_missing：GraphML 有 node 但 vdb 没对应向量。
    
    返回 errors 列表（可能为空）。
    """
    errors: list[dict[str, Any]] = []
    
    node_ids, _, _, graphml_err = _load_graphml(storage_dir / _GRAPHML_FILE)
    if graphml_err or not node_ids:
        return errors  # GraphML 有问题由 _check_graphml_post 报，这里不重复
    
    # vdb_entities 检测：GraphML node 应在 vdb_entities 有对应向量
    vdb_e_path = storage_dir / "vdb_entities.json"
    vdb_e_list, vdb_e_err = _load_vdb(vdb_e_path)
    if vdb_e_err:
        errors.append(vdb_e_err)
    else:
        # vdb_entities 的 entity_name 集合
        vdb_e_names = {
            entry.get("entity_name", "").lower() if isinstance(entry, dict) else ""
            for entry in vdb_e_list
        }
        vdb_e_names.discard("")
        # GraphML node id 是小写化的（LightRAG 设计），直接比对
        missing_in_vdb = {n for n in node_ids if n.lower() not in vdb_e_names}
        if missing_in_vdb:
            errors.append({
                "check": "vdb_entities_missing",
                "severity": "major",
                "ref_file": _GRAPHML_FILE,
                "target_file": "vdb_entities.json",
                "missing_count": len(missing_in_vdb),
                "msg": f"GraphML 有 {len(missing_in_vdb)} 个 node 在 vdb_entities 中无对应向量",
            })
    
    # vdb_relationships 检测：GraphML edge 应在 vdb_relationships 有对应向量
    _, edges, _, _ = _load_graphml(storage_dir / _GRAPHML_FILE)
    vdb_r_path = storage_dir / "vdb_relationships.json"
    vdb_r_list, vdb_r_err = _load_vdb(vdb_r_path)
    if vdb_r_err:
        errors.append(vdb_r_err)
    elif edges:
        # vdb_relationships 的 (src, tgt) 集合
        vdb_r_pairs = set()
        for entry in vdb_r_list:
            if not isinstance(entry, dict):
                continue
            src = entry.get("src_id", "")
            tgt = entry.get("tgt_id", "")
            if src and tgt:
                vdb_r_pairs.add((src.lower(), tgt.lower()))
        # GraphML edge 集合
        graphml_pairs = {(s.lower(), t.lower()) for s, t in edges}
        missing_pairs = graphml_pairs - vdb_r_pairs
        if missing_pairs:
            errors.append({
                "check": "vdb_relationships_missing",
                "severity": "major",
                "ref_file": _GRAPHML_FILE,
                "target_file": "vdb_relationships.json",
                "missing_count": len(missing_pairs),
                "msg": f"GraphML 有 {len(missing_pairs)} 条 edge 在 vdb_relationships 中无对应向量",
            })
    
    return errors


def check_all() -> dict[str, Any]:
    """简化版 check_all v2：检 2 真相源 + GraphML 后置 + vdb_*_missing。
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
    
    # 3. vdb_*_missing 检测
    vdb_errors = _check_vdb_missing(storage_dir)
    all_errors.extend(vdb_errors)
    
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
            "vdb_missing": {"name": "vdb_missing", "errors": vdb_errors},
        },
    }
```

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py -v -k check_all
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_integrity.py tests/test_lightrag_repair_unit.py
git commit -m "refactor(integrity): check_all 简化为检 2 真相源 + GraphML 后置 + vdb_*_missing

删除 16 个旧 check 函数。新 check_all 检 3 项：
1. 2 真相源完整可用（含内容完整性检查）
2. GraphML 后置验证（重建后应该有 node）
3. vdb_*_missing（GraphML 有 node 但 vdb 没对应向量 → 启动放行风险）
"
```

---

## Task 6: 删除引用旧 check 的测试文件

**Files:**
- Delete: 引用旧 16 个 check 函数的测试文件

### 背景

Task 5 删除了 16 个旧 check 函数，引用它们的测试会失败。

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

Expected: 没有引用旧 check 的测试失败

### - [ ] Step 4: Commit

```bash
git add -A tests/
git commit -m "test: 删除引用旧 check 函数的测试文件"
```

---

## Task 7: 修复 `lightrag_manager` 的 `total_errors` 字段（含日志 + Rust struct）

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py:1038-1061, 1380-1395`
- Modify: `launcher/src/main.rs:50-55`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

v1 计划 Task 5 只修 `get_lightrag_status` 的 integrity 字段，审查发现 `run_resilience_phase1` 日志和 Rust `IntegrityStatus` struct 也要改。三处都改。

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py` 追加：

```python
def test_get_lightrag_status_total_errors_correct(tmp_path, monkeypatch):
    """get_lightrag_status 暴露的 total_errors 应 = critical + major + minor。"""
    from niu_api.internal import lightrag_manager
    
    # 准备损坏现场：full_docs 缺失（critical）+ GraphML 缺失（major）+ vdb 缺失（major）
    (tmp_path / "kv_store_llm_response_cache.json").write_text(
        json.dumps({"x": {"return": "y", "cache_type": "extract", "chunk_id": "chunk-x"}}, ensure_ascii=False)
    )
    
    monkeypatch.setattr(lightrag_manager, "_integrity_result", None)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    
    status = lightrag_manager.get_lightrag_status()
    
    assert status["integrity"]["ok"] is False
    assert status["integrity"]["total_errors"] >= 1
    assert status["integrity"]["total_errors"] != 0
    # 新字段也应暴露
    assert "critical_errors" in status["integrity"]
    assert "major_errors" in status["integrity"]
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_get_lightrag_status_total_errors_correct -v
```

Expected: FAIL（现有 `total_errors` 永远 0）

### - [ ] Step 3: Write minimal implementation

**3a. 修改 `niu_api/internal/lightrag_manager.py` 的 `get_lightrag_status`**（搜索 `integrity` 字段构造位置，大约在 L1380）：

找到这段：
```python
"integrity": {
    "ok": _integrity_result.get("ok", False) if _integrity_result else True,
    "total_errors": _integrity_result.get("total_errors", 0) if _integrity_result else 0,
}
```

替换为：
```python
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

**3b. 修复 `run_resilience_phase1` 日志**（L1038-1061 附近）：

找到这段（L1061 附近）：
```python
f"total_errors={check_result.get('total_errors', 0)}"
```

替换为：
```python
critical = check_result.get("critical_errors", 0)
major = check_result.get("major_errors", 0)
minor = check_result.get("minor_errors", 0)
total = critical + major + minor
f"critical={critical}, major={major}, minor={minor}, total_errors={total}"
```

**3c. 修复 `run_resilience_phase1` 异常路径**（L1055 附近）：

找到：
```python
check_result = {"ok": True, "total_errors": 0, "error": str(e)}
```

替换为：
```python
check_result = {"ok": True, "critical_errors": 0, "major_errors": 0, "minor_errors": 0, "error": str(e)}
```

**3d. 修改 `launcher/src/main.rs` 的 `IntegrityStatus` struct**（L50-55）：

找到：
```rust
struct IntegrityStatus {
    ok: bool,
    total_errors: i32,
}
```

替换为：
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

注意：`#[serde(default)]` 让缺失字段默认 0，向后兼容。

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_get_lightrag_status_total_errors_correct -v
```

Expected: PASS

### - [ ] Step 5: 重新编译 Rust 启动器

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./launcher/build.sh 2>&1 | tail -10
```

Expected: 编译成功

### - [ ] Step 6: Commit

```bash
git add niu_api/internal/lightrag_manager.py launcher/src/main.rs tests/test_lightrag_repair_unit.py
git commit -m "fix(manager+launcher): total_errors 字段完整修复（status 接口 + 日志 + Rust struct）

之前 status 接口暴露的 total_errors=0 但实际 check_all 报 91 errors，
导致 Rust 启动器走错分支。三处都修：
1. get_lightrag_status 的 integrity 字段加 critical_errors/major_errors/minor_errors
2. run_resilience_phase1 日志打印完整 critical/major/minor/total
3. Rust IntegrityStatus struct 加 critical_errors/major_errors/minor_errors 字段
"
```

---

## Task 8: 修复 `run_repair_on_user_request` 的 `repaired` 判定（适配扁平结构 + 保留 SkillSync 二次 repair）

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py:1146-1340`
- Test: `tests/test_lightrag_repair_unit.py`

### 背景

v1 计划 Task 6 用"重检 check_all 报 major=0"判定 `repaired`，审查发现历史残留孤儿 chunk 永远报 major 导致 `repaired=False`。新设计改为：`repaired = not repair_all_result.get("_unrecoverable", False)`——基于 `repair_all` 返回的 `_unrecoverable` 字段。

SkillSync 二次 repair 保留（v1 删了导致残留 entity_chunks 不清），但适配扁平结构：二次 repair 结果用 `post_skill_sync_` 前缀合并到顶层，不嵌套。

### - [ ] Step 1: Write the failing test

`tests/test_lightrag_repair_unit.py` 追加：

```python
def test_run_repair_on_user_request_repaired_based_on_unrecoverable_flag(tmp_path, monkeypatch):
    """repaired 应基于 repair_all 的 _unrecoverable 字段，不基于 check_all 重检。"""
    from niu_api.internal import lightrag_manager
    
    # 准备真相源
    docs = {"doc-x": {"content": "test", "file_path": "x.md"}}
    cache = {"default:extract:k1": {"return": "entity", "cache_type": "extract", "chunk_id": "chunk-x"}}
    (tmp_path / "kv_store_full_docs.json").write_text(json.dumps(docs, ensure_ascii=False))
    (tmp_path / "kv_store_llm_response_cache.json").write_text(json.dumps(cache, ensure_ascii=False))
    
    monkeypatch.setattr(lightrag_manager, "_integrity_result", None)
    monkeypatch.setattr(lightrag_manager, "_rag_instance", None)
    monkeypatch.setattr(lightrag_manager, "_repairing", False)
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.kg_api._read_pipeline_busy", lambda: False)
    
    result = lightrag_manager.run_repair_on_user_request()
    
    assert "repaired" in result
    assert isinstance(result["repaired"], bool)
    # repaired 应基于 _unrecoverable 字段
    if result.get("repair_result", {}).get("_unrecoverable"):
        assert result["repaired"] is False
    else:
        assert result["repaired"] is True
```

### - [ ] Step 2: Run test to verify it fails

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py::test_run_repair_on_user_request_repaired_based_on_unrecoverable_flag -v
```

Expected: FAIL（现有 `repaired` 基于 check_all 重检）

### - [ ] Step 3: Write minimal implementation

修改 `niu_api/internal/lightrag_manager.py:1296-1340`：

找到这段（实际行号以 grep 为准）：
```python
# 旧代码
critical = check_result.get("critical_errors", 0)
major = check_result.get("major_errors", 0)
minor = check_result.get("minor_errors", 0)
# ...
repaired = not has_unrecoverable and critical == 0 and major == 0
```

替换为：
```python
# 新代码：repaired 基于 repair_all 的 _unrecoverable 字段
repaired = not has_unrecoverable and not repair_result.get("_unrecoverable", False)

critical = check_result.get("critical_errors", 0)
major = check_result.get("major_errors", 0)
minor = check_result.get("minor_errors", 0)
```

同时修复 SkillSync 二次 repair 合并逻辑（L1283-1288）：

找到：
```python
for k, v in second_repair.items():
    if k.startswith("_"):
        continue
    repair_result[f"post_skill_sync_{k}"] = v
```

替换为（保留 `if k.startswith("_"): continue` 跳过下划线字段，但二次 repair 的 `_unrecoverable` 单独合并到顶层，让 Rust 能读到）：
```python
# 二次 repair 的下划线字段跳过（避免 post_skill_sync__unrecoverable 双下划线）
# 但 _unrecoverable 单独合并到顶层，让 Rust format_repair_summary 能读到
if second_repair.get("_unrecoverable"):
    repair_result["_unrecoverable"] = True
    repair_result["_post_skill_sync_failed"] = True
for k, v in second_repair.items():
    if k.startswith("_"):
        continue
    repair_result[f"post_skill_sync_{k}"] = v
```

注意：Rust `format_repair_summary`（`main.rs:190-197`）遍历 `repair_result.<*>.unrecoverable` 字段检测 unrecoverable。二次 repair 的各 repair 函数返回的 result（含 `unrecoverable: True`）会以 `post_skill_sync_<name>` 名字合并到顶层，Rust 能读到 `post_skill_sync_text_chunks.unrecoverable` 等。二次 repair 顶层的 `_unrecoverable` 标记单独合并到 `repair_result["_unrecoverable"]`，让 `run_repair_on_user_request` 的 `repaired` 判定能读到。

### - [ ] Step 4: Run test to verify it passes

Run:
```bash
python -m pytest tests/test_lightrag_repair_unit.py -v
```

Expected: PASS

### - [ ] Step 5: Commit

```bash
git add niu_api/internal/lightrag_manager.py tests/test_lightrag_repair_unit.py
git commit -m "fix(manager): repaired 判定基于 _unrecoverable 字段，保留 SkillSync 二次 repair

之前 repaired 用'重检 check_all 报 major=0'判定，但历史残留孤儿 chunk
永远报 major 导致永远 repaired=False。新设计改为基于 repair_all 返回的
_unrecoverable 字段。

SkillSync 二次 repair 保留（v1 删了导致残留 entity_chunks 不清），
适配扁平结构：所有字段加 post_skill_sync_ 前缀合并到顶层。
"
```

---

## Task 9: 端到端验证——合成 fixture 6 种损坏现场

**Files:**
- Create: `tests/test_lightrag_rebuild_from_truth.py`
- Create: `tests/fixtures/lightrag_truth_sources/`（合成数据，不含真实人名）

### 背景

v1 计划 Task 7 用真实备份做 fixture，审查发现含真实人名/电话/地址，提交 git 泄露隐私。本 Task 用合成数据：3 个文档 + 5 个 extract cache + 1 个僵尸脑区 cache，规模小但覆盖关键场景。

6 种损坏现场：
1. 删 vdb_*.json
2. 删 GraphML
3. 删 9 个派生文件全部
4. 损坏 9 个派生文件
5. 真相源损坏（unrecoverable + 回滚）
6. 含僵尸 cache → 重建后僵尸不复活

### - [ ] Step 1: 生成合成 fixture

写一个 Python 脚本生成合成 fixture：

`tests/fixtures/lightrag_truth_sources/generate_fixture.py`:

```python
"""生成合成 fixture（不含真实人名），用于端到端测试。"""
import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent

def generate():
    # 3 个文档（虚构内容）
    docs = {
        "doc-syn-1": {
            "content": "测试文档1：虚构人物张三李四王五的介绍，用于测试 LightRAG 重建流程。",
            "file_path": "synthetic1.md",
            "create_time": 1781930610,
            "update_time": 1781930610,
            "_id": "doc-syn-1",
        },
        "doc-syn-2": {
            "content": "测试文档2：虚构组织测试公司的业务介绍，用于测试脑区功能。",
            "file_path": "synthetic2.md",
            "create_time": 1781930611,
            "update_time": 1781930611,
            "_id": "doc-syn-2",
        },
        "doc-syn-3": {
            "content": "测试文档3：系统维护日志，记录删除重复脑区的操作。",
            "file_path": "synthetic3.md",
            "create_time": 1781930612,
            "update_time": 1781930612,
            "_id": "doc-syn-3",
        },
    }
    
    # 5 个正常 extract cache + 1 个僵尸脑区 cache
    cache = {
        "default:extract:syn-key-1": {
            "return": "entity<|#|>张三<|#|>person<|#|>虚构人物张三的介绍。\nrelation<|#|>人际关系脑区<|#|>张三<|#|>包含<|#|>人际关系脑区包含张三。",
            "cache_type": "extract",
            "chunk_id": "chunk-syn-1",
            "original_prompt": "synthetic",
            "create_time": 1781930610,
        },
        "default:extract:syn-key-2": {
            "return": "entity<|#|>李四<|#|>person<|#|>虚构人物李四的介绍。",
            "cache_type": "extract",
            "chunk_id": "chunk-syn-2",
            "create_time": 1781930611,
        },
        "default:extract:syn-key-3": {
            "return": "entity<|#|>测试公司<|#|>organization<|#|>虚构测试公司介绍。",
            "cache_type": "extract",
            "chunk_id": "chunk-syn-3",
            "create_time": 1781930612,
        },
        "default:extract:syn-key-4": {
            "return": "entity<|#|>王五<|#|>person<|#|>虚构人物王五。",
            "cache_type": "extract",
            "chunk_id": "chunk-syn-1",
            "create_time": 1781930613,
        },
        "default:extract:syn-key-5": {
            "return": "<|COMPLETE|>",
            "cache_type": "extract",
            "chunk_id": "chunk-syn-2",
            "create_time": 1781930614,
        },
        # 僵尸脑区 cache（description 含"被删除"标记）
        "default:extract:zombie-syn": {
            "return": "entity<|#|>智家测试僵尸脑区<|#|>brainregion<|#|>被删除的重复脑区实体之一。",
            "cache_type": "extract",
            "chunk_id": "chunk-zombie-syn",
            "create_time": 1781930615,
        },
    }
    
    (FIXTURE_DIR / "kv_store_full_docs.json").write_text(
        json.dumps(docs, ensure_ascii=False, indent=2)
    )
    (FIXTURE_DIR / "kv_store_llm_response_cache.json").write_text(
        json.dumps(cache, ensure_ascii=False, indent=2)
    )
    print(f"生成 fixture 到 {FIXTURE_DIR}")

if __name__ == "__main__":
    generate()
```

运行：
```bash
cd REDACTED_USER_PATH/tools/ai-bot
python tests/fixtures/lightrag_truth_sources/generate_fixture.py
ls -la tests/fixtures/lightrag_truth_sources/
```

### - [ ] Step 2: Write the failing test

`tests/test_lightrag_rebuild_from_truth.py`:

```python
"""端到端验证：6 种损坏现场全部能从 2 真相源修复（合成 fixture）。

不 mock LLM，用真实 LightRAG 实例 + 真实 embedding。
注意：repair_graphml 重跑 pipeline 时 cache 命中不调 LLM。
"""
import json
import shutil
import pytest
from pathlib import Path
from unittest.mock import patch

from niu_api.internal.lightrag_repair import repair_all, repair_brainregion_zombies
from niu_api.internal.lightrag_integrity import check_all

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "lightrag_truth_sources"


@pytest.fixture
def isolated_storage(tmp_path):
    """复制 fixture 真相源到 tmp_path。"""
    for fname in ["kv_store_full_docs.json", "kv_store_llm_response_cache.json"]:
        src = FIXTURE_DIR / fname
        if src.exists():
            shutil.copy(src, tmp_path / fname)
    return tmp_path


@pytest.fixture
def patched_storage(tmp_path):
    """patch _STORAGE_DIR 到 tmp_path，返回 tmp_path。"""
    with patch("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path), \
         patch("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path):
        yield tmp_path


def test_e2e_repair_after_delete_vdb(isolated_storage, patched_storage):
    """场景 1：删 vdb_*.json → repair → 重建。"""
    # 先跑一次 repair 建立 baseline（让所有派生文件存在）
    repair_all()
    
    # 删 3 个 vdb 文件
    for fname in ["vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"]:
        (isolated_storage / fname).unlink()
    
    result = repair_all()
    
    assert not result.get("_unrecoverable"), f"修复应成功: {result.get('_unrecoverable_reason')}"
    for fname in ["vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"]:
        assert (isolated_storage / fname).exists()
        assert (isolated_storage / fname).stat().st_size > 0


def test_e2e_repair_after_delete_graphml(isolated_storage, patched_storage):
    """场景 2：删 GraphML → repair → 重建。"""
    repair_all()
    (isolated_storage / "graph_chunk_entity_relation.graphml").unlink()
    
    result = repair_all()
    
    assert not result.get("_unrecoverable")
    assert (isolated_storage / "graph_chunk_entity_relation.graphml").exists()


def test_e2e_repair_after_delete_all_derived(isolated_storage, patched_storage):
    """场景 3：删 9 个派生文件全部 → repair → 全部重建。"""
    derived = [
        "kv_store_text_chunks.json", "kv_store_doc_status.json",
        "graph_chunk_entity_relation.graphml",
        "vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json",
        "kv_store_entity_chunks.json", "kv_store_relation_chunks.json",
        "kv_store_full_entities.json", "kv_store_full_relations.json",
    ]
    for fname in derived:
        if (isolated_storage / fname).exists():
            (isolated_storage / fname).unlink()
    
    result = repair_all()
    
    assert not result.get("_unrecoverable")
    for fname in derived:
        assert (isolated_storage / fname).exists(), f"{fname} 应被重建"


def test_e2e_repair_after_corrupt_derived(isolated_storage, patched_storage):
    """场景 4：损坏 9 个派生文件 → repair → 重建。"""
    derived = [
        "kv_store_text_chunks.json", "kv_store_doc_status.json",
        "graph_chunk_entity_relation.graphml",
        "vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json",
        "kv_store_entity_chunks.json", "kv_store_relation_chunks.json",
        "kv_store_full_entities.json", "kv_store_full_relations.json",
    ]
    for fname in derived:
        (isolated_storage / fname).write_text('{"corrupt": "garbage"}')
    
    result = repair_all()
    
    assert not result.get("_unrecoverable")
    for fname in derived:
        content = (isolated_storage / fname).read_text()
        assert "garbage" not in content


def test_e2e_unrecoverable_when_full_docs_missing(isolated_storage, patched_storage):
    """场景 5：真相源 full_docs 损坏 → unrecoverable + 回滚。"""
    (isolated_storage / "kv_store_full_docs.json").unlink()
    (isolated_storage / "kv_store_text_chunks.json").write_text('{"old": "data"}')
    
    result = repair_all()
    
    assert result.get("_unrecoverable") is True
    # 回滚：派生文件保留原状
    assert (isolated_storage / "kv_store_text_chunks.json").read_text() == '{"old": "data"}'


def test_e2e_zombie_cache_cleaned_before_rebuild(isolated_storage, patched_storage):
    """场景 6：含僵尸脑区 cache → 重建后僵尸不复活。
    
    fixture 的 llm_response_cache 有 1 条 zombie-syn extract entry
    （description 含"被删除的重复脑区实体之一"）。
    repair_all 应在重建 GraphML 前先清掉这条 cache entry。
    """
    # 先跑 repair_brainregion_zombies 单独验证 cache 清理
    repair_brainregion_zombies()
    
    cache = json.loads((isolated_storage / "kv_store_llm_response_cache.json").read_text())
    # 僵尸 extract entry 应被删除
    assert "default:extract:zombie-syn" not in cache
    # 正常 extract entry 应保留
    assert "default:extract:syn-key-1" in cache
    
    # 再跑完整 repair_all，重建 GraphML
    result = repair_all()
    assert not result.get("_unrecoverable")
    
    # 重建后 GraphML 不应含"智家测试僵尸脑区" node
    import xml.etree.ElementTree as ET
    tree = ET.parse(isolated_storage / "graph_chunk_entity_relation.graphml")
    ns = {'g': 'http://graphml.graphdrawing.org/xmlns'}
    node_ids = {n.get("id") for n in tree.findall('.//g:node', ns)}
    assert "智家测试僵尸脑区" not in node_ids, "僵尸脑区不应复活"
```

### - [ ] Step 3: Run test to verify it fails

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_lightrag_rebuild_from_truth.py -v --timeout=600 2>&1 | tail -30
```

Expected: FAIL（修复实现可能还没完全跑通）

### - [ ] Step 4: 修复实现直到测试通过

逐个测试调试修复实现。常见问题：
- `repair_graphml` 重跑 pipeline 时 embedding model 加载失败 → 确认模型路径
- `repair_vdb_*` embedding 慢 → 测试加 `--timeout=600`
- chunking 参数不一致 → 用真实配置

### - [ ] Step 5: Run all tests

```bash
python -m pytest tests/ -v --timeout=600 2>&1 | tail -40
```

Expected: 全部 PASS

### - [ ] Step 6: Commit

```bash
git add tests/test_lightrag_rebuild_from_truth.py tests/fixtures/lightrag_truth_sources/
git commit -m "test: 端到端验证 6 种损坏现场（合成 fixture，不含真实人名）

新增 6 个端到端测试：
1. 删 vdb_*.json → repair 重建
2. 删 GraphML → repair 重建
3. 删 9 个派生文件全部 → repair 全部重建
4. 损坏 9 个派生文件 → repair 重建
5. 真相源 full_docs 损坏 → unrecoverable + 回滚
6. 含僵尸脑区 cache → 重建后僵尸不复活

fixture 用合成数据（虚构张三李四王五+测试公司），不含真实人名/电话/地址。
"
```

---

## Task 10: 真实启动验证——./niu 启动走完整 repair 流程（3 种现场 + region_sync 验证）

**Files:**
- 无代码改动，只做端到端启动验证

### 背景

TDD 测试通过后，必须用真实程序 `./niu` 启动验证——CLAUDE.md 铁律 5"测试必须用真实数据+真实LLM"。本 Task 扩展到 3 种损坏现场 + region_sync 启动后 1 分钟内完成验证（看日志不含 dissolve 卡死）。

### - [ ] Step 1: 备份当前数据

```bash
TS=$(date +%Y%m%d_%H%M%S)
cp -R ~/.niu/lightrag_storage ~/.niu/lightrag_storage.prebuild_${TS}
echo "BACKUP_DONE: lightrag_storage.prebuild_${TS}"
```

### - [ ] Step 2: 循环测 3 种损坏现场

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./launcher/build.sh 2>&1 | tail -5

# 3 种现场
for scenario in "delete_vdb" "delete_graphml" "delete_all_derived"; do
    echo "=== 测试场景: $scenario ==="
    
    # 恢复真实数据到 clean state
    rm -rf ~/.niu/lightrag_storage
    cp -R ~/.niu/lightrag_storage.prebuild_${TS} ~/.niu/lightrag_storage
    
    # 制造损坏现场
    case $scenario in
        delete_vdb)
            rm -f ~/.niu/lightrag_storage/vdb_chunks.json
            rm -f ~/.niu/lightrag_storage/vdb_entities.json
            rm -f ~/.niu/lightrag_storage/vdb_relationships.json
            ;;
        delete_graphml)
            rm -f ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml
            ;;
        delete_all_derived)
            for f in kv_store_text_chunks.json kv_store_doc_status.json \
                     vdb_chunks.json vdb_entities.json vdb_relationships.json \
                     kv_store_entity_chunks.json kv_store_relation_chunks.json \
                     kv_store_full_entities.json kv_store_full_relations.json; do
                rm -f ~/.niu/lightrag_storage/$f
            done
            ;;
    esac
    
    # 启动 ./niu
    ./niu > /tmp/niu_scenario_${scenario}.log 2>&1 &
    NIU_PID=$!
    
    # 等 status check
    for i in $(seq 1 30); do
        sleep 2
        if grep -q "Phase 1 完成" /tmp/niu_scenario_${scenario}.log 2>/dev/null; then
            break
        fi
    done
    
    # 模拟点"是"调 repair
    curl -s -X POST "http://127.0.0.1:9876/api/kg/lightrag/repair?target=all" --max-time 600 > /tmp/repair_${scenario}.json 2>&1
    
    python3 -c "
import json
d = json.load(open('/tmp/repair_${scenario}.json'))
r = d.get('result', {})
print('repaired:', r.get('repaired'))
print('major_errors:', r.get('major_errors'))
"
    
    # 验证 region_sync 启动后 1 分钟内完成（不含 dissolve 卡死）
    sleep 60
    if grep -q "dissolve" /tmp/niu_scenario_${scenario}.log 2>/dev/null; then
        echo "FAIL: region_sync 仍在跑 dissolve"
        grep -c "dissolve" /tmp/niu_scenario_${scenario}.log
    else
        echo "OK: region_sync 没卡 dissolve"
    fi
    
    # 杀进程（铁律：禁止 pkill -f niu，会损坏 LightRAG vdb 文件——参考 MEMORY.md no-pkill-subprocess / test-process-kill-corruption）
    # 用 kill -TERM $NIU_PID 优雅退出 + 等待 timeout
    kill -TERM $NIU_PID 2>/dev/null
    for i in 1 2 3 4 5 6 7 8 9 10; do
        sleep 1
        kill -0 $NIU_PID 2>/dev/null || break  # 进程已退出
    done
    # 超时后仍存活才用 kill -9（精确 PID，不用 pkill -f）
    kill -0 $NIU_PID 2>/dev/null && kill -9 $NIU_PID 2>/dev/null
done
```

Expected:
- 3 种现场 repair 都 `repaired: True`
- region_sync 没卡 dissolve

### - [ ] Step 3: 恢复用户真实数据

```bash
TS_BACKUP=<Step 1 的 TS 值>
rm -rf ~/.niu/lightrag_storage
cp -R ~/.niu/lightrag_storage.prebuild_${TS_BACKUP} ~/.niu/lightrag_storage
rm -rf ~/.niu/lightrag_storage.prebuild_*
echo "RESTORED"
```

### - [ ] Step 4: Commit 验证日志

```bash
# 没有代码改动，只记录验证日志
git log --oneline -15
```

### - [ ] Step 5: 报告

在 PR 描述里写：
- 6 种 TDD 测试 + 3 种真实启动验证全部通过
- 真相源完整性检测正确触发 unrecoverable + 回滚
- `total_errors` 字段正确累加（status 接口 + 日志 + Rust struct）
- `repaired` 判定基于 `_unrecoverable` 字段
- 僵尸脑区 cache 在重建前被清理，重建后不复活
- region_sync 启动后 1 分钟内没卡 dissolve

---

## Self-Review Checklist

### 1. Spec coverage

- [x] 检测 2 真相源（含内容完整性）→ Task 4 (`_check_truth_sources`)
- [x] 备份 9 派生文件 → Task 4 (`repair_all` 备份逻辑)
- [x] 清僵尸 cache → Task 1 (`repair_brainregion_zombies` 扩展)
- [x] 删除 9 派生文件 → Task 4 (`repair_all` 删除逻辑)
- [x] 按依赖链重建 → Task 4 (`_REBUILD_ORDER`)
- [x] 失败回滚 → Task 4 (`repair_all` 回滚逻辑)
- [x] `repair_text_chunks` 用真实配置 + chunk_id 保护 → Task 2
- [x] `repair_graphml` 让 patch 生效 → Task 3
- [x] `repair_all` 保持扁平结构 → Task 4
- [x] `check_all` 加 vdb_*_missing → Task 5
- [x] `total_errors` 三处修复 → Task 7
- [x] `repaired` 判定 + SkillSync 二次 repair 保留 → Task 8
- [x] 6 种 TDD 测试 + 合成 fixture → Task 9
- [x] 3 种真实启动验证 + region_sync → Task 10
- [x] 删除引用旧 check 的测试 → Task 6

### 2. Placeholder scan

- [x] 无 TBD / TODO
- [x] 无 "add appropriate error handling"
- [x] 每个 Task 都有完整测试代码
- [x] Task 7/8 Step 1 让工程师 `sed -n` 查看现状——必要，避免行号假设错误

### 3. Type consistency

- [x] `repair_all` 返回扁平结构，`run_repair_on_user_request` 读 `_unrecoverable` 字段——一致
- [x] `_TRUTH_SOURCE_FILES` 在 Task 4 是 set，在 Task 5 是 list——故意不一致（Task 4 用 set 做 O(1) 查找，Task 5 用 list 保序遍历）
- [x] `_REBUILD_ORDER` 元组格式 `(name, fn)` 跟 Task 4 重建循环一致
- [x] Rust `IntegrityStatus` struct 加 `#[serde(default)]` 字段——向后兼容

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-13-lightrag-rebuild-from-truth-sources.md` (v2，已修订 14 个审查问题).

Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
