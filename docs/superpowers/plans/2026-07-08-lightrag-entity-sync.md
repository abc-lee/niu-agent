# LightRAG vdb_entities ↔ GraphML 实体同步性检测+修复 Implementation Plan (v3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复启动检测漏报 vdb_entities 跟 GraphML 实体不同步的问题。实测发现 157 个"孤儿向量"全是大小写不一致导致（vdb 用 "Niu"，GraphML node id 用 "niu"）。**用户铁律：知识图谱语义查询不能大小写敏感，所有写入必须转小写，没转的就是 bug**。LightRAG 设计上 node id 全部 lower 化（networkx_impl.py L31-35），但 vdb 的 entity_name 没有归一化。修复策略：**vdb 的 entity_name 全部 lower 化**，跟 GraphML 对齐；大写就是 bug，检测要报 error 触发弹窗。

**Architecture:** 在 `lightrag_integrity.py` 新增 `check_entity_sync` 函数，对比时统一 lower 化——**case_mismatch（vdb 有大写）算 error 触发弹窗**，真孤儿（lower 后 GraphML 仍没有）也算 error，缺失向量算 error。在 `lightrag_repair.py` 新增 `repair_entity_sync` 函数：①vdb 大写改小写（matrix 不动，向量不变）②vdb 重复 lower_name（如 "Niu"+"niu"）优先保留已小写条目，丢弃大写重复 ③真孤儿删除 ④缺失向量从 GraphML d2(description) 重新 embedding，source_id 用 d3 真实 chunk-id。备份用 `shutil.copy2`（不先 unlink，copy2 本身处理已存在目标），备份失败立即 abort。`corrupt.bak` 加时间戳后缀不被覆盖。`check_all` / `repair_all` 自动调用新函数。

**Tech Stack:** Python 3.11+，xml.etree.ElementTree（GraphML 解析），numpy（向量编码），niu_api.internal.embedding（预加载 bge-base-zh 模型，768d）

---

## Context

### 当前 bug

启动检测说"OK"不弹窗，但运行时 LightRAG 报 `WARNING: Some nodes are missing, maybe the storage is damaged`。

**根因**：`check_all` 只检文件结构完整性，没检 vdb_entities 跟 GraphML 的实体名同步性。

### 用户铁律（必须遵守）

- "大小写问题在我们的KG开发字典里有明确的要求，所有写入内容必须转换为小写"
- "知识图谱是语义查询，不能大小写敏感。Apple 和 apple 都是苹果，凭什么不是一个东西"
- "如果你发现哪里有没有把它转换成小写就写入的，那它一定是错误的"

**核心原则**：vdb 里有大写 entity_name 就是 bug，检测要报 error 触发弹窗，修复要改成小写。

### 小写化边界（仅这些字段强制小写）

- `entity_name` / `__id__`：**强制小写**（语义查询的 key，必须归一化）
- `content`（描述文本）：**保留原样**（自然语言，大小写有语义）
- `source_id`（chunk-id）：**保留原样**（已是 `chunk-<hex>` 小写格式，但不在本计划强制范围）
- `file_path`：**保留原样**（文件路径，macOS 大小写不敏感但语义保留）

### 实测数据状态（2026-07-08）

- vdb_entities 有 2350 个实体，entity_name 是原始大小写（如 "Niu"、"XX分行高速公路苏通卡项目"）
- GraphML 有 2319 个节点，node id 全部小写（如 "niu"、"xx分行高速公路苏通卡项目"）——LightRAG networkx_impl.py L31-35 设计上 lower 化
- vdb 有但 GraphML 没有的实体：157 个，**全部是大小写不一致**（vdb "Niu" vs GraphML "niu"），不是真孤儿
- d0(entity_id) 跟 node id 也有 139 个大小写不一致——d0 保留原始大小写，node id 是 lower 化的

### LightRAG 大小写设计（已查证）

`<lightrag_fork_path>/lightrag/kg/networkx_impl.py` L30-35：

```python
@staticmethod
def _normalize_node_id(node_id: str | None) -> str:
    """Knowledge graphs should treat 'Apple' and 'apple' as the same entity.
    This ensures consistent node identity regardless of case variation
    from LLM extraction or external injection.
    """
    return node_id.lower() if isinstance(node_id, str) else node_id
```

GraphML 的 node id 在写入时被 `_normalize_node_id` lower 化。vdb 的 entity_name 应该也 lower 化对齐，但历史数据没做。

### GraphML key 定义（已实测）

| key | for | attr.name | 用途 |
|-----|-----|-----------|------|
| d0 | node | entity_id | 实体名（原始大小写，跟 node id 可能不一致） |
| d1 | node | entity_type | 实体类型 |
| d2 | node | description | 实体描述文本（**重建向量用这个**） |
| d3 | node | source_id | chunk-id（**重建时写真实 source_id**） |
| d4 | node | file_path | 文件路径 |
| d5 | node | created_at | 创建时间 |

### 修复策略（以 GraphML 为真相源，统一小写）

1. **vdb 大写但 GraphML 有对应小写节点**（157 个，如 vdb "Niu" vs GraphML "niu"）→ 改 vdb 的 `__id__` 和 `entity_name` 字段为小写（matrix 不动，向量不变，无数据丢失）
2. **vdb 有重复 lower_name**（如 "Niu" + "niu" 共存）→ 优先保留已小写的条目（`orig_name == lower_name`），丢弃大写重复条目（大写重复是 bug，丢弃是修正）
3. **lower 化后 vdb 仍多出来的**（真孤儿，lower 后 GraphML 没对应节点）→ 从 vdb 删除条目 + matrix 对应行
4. **lower 化后 GraphML 有但 vdb 没有的**（缺失向量）→ 从 GraphML d2(description) 重新 embedding 写入 vdb，entity_name 用小写，source_id 用 d3 真实 chunk-id

### 关键约束（用户铁律）

- **禁止直接修改 LightRAG fork 安装包**——只能改 `niu_api/internal/` 下的外挂检测/修复
- **修改前必须先做临时提交备份**（铁律 #3）
- **测试必须用真实数据**（铁律 #5）——单元测试用 mock embedding 维度要匹配（768d），端到端用真实数据
- **禁止 `git reset --hard`** / `git push`
- **embedding 模型用预加载的**（`niu_api.internal.embedding.get_model()`，bge-base-zh-v1.5，768d）

### 关键代码位置

| 文件 | 行号 | 内容 |
|------|------|------|
| `niu_api/internal/lightrag_integrity.py` | L44 | `_GRAPHML_FILE = "graph_chunk_entity_relation.graphml"` |
| `niu_api/internal/lightrag_integrity.py` | L49 | `_resolve_storage_dir()` 返回 `Path(_STORAGE_DIR)` |
| `niu_api/internal/lightrag_integrity.py` | L52-67 | `_decode_vector(vec_b64, embedding_dim)` 函数（repair 需 import） |
| `niu_api/internal/lightrag_integrity.py` | L212-273 | `check_graphml` 函数 |
| `niu_api/internal/lightrag_integrity.py` | L276-305 | `check_all` 函数 |
| `niu_api/internal/lightrag_repair.py` | L42-66 | `_embed_text` 函数（模块级，可 mock） |
| `niu_api/internal/lightrag_repair.py` | L69-80 | `_encode_vector` / `_encode_matrix` 编码函数 |
| `niu_api/internal/lightrag_repair.py` | L83-85 | `_storage_dir()` 返回 `Path(_STORAGE_DIR)` |
| `niu_api/internal/lightrag_repair.py` | L141-224 | `repair_vdb` 函数（含原子写 + 备份） |
| `niu_api/internal/lightrag_repair.py` | L241-246 | `repair_all` 函数 |
| `niu_api/internal/lightrag_manager.py` | L1023-1040 | `run_repair_on_user_request`（调 `repair_all` + `reset_init_state` + `get_lightrag`） |

### vdb_entities 文件结构

```json
{
  "embedding_dim": 768,
  "data": [
    {
      "__id__": "Niu",          // 原始大小写，修复后改 "niu"
      "__created_at__": 1781930491,
      "entity_name": "Niu",     // 原始大小写，修复后改 "niu"
      "content": "实体描述文本",  // 保留原样
      "source_id": "chunk-xxx", // 保留原样
      "file_path": "/path/to/doc.pptx", // 保留原样
      "vector": "base64(zlib(float16 bytes))"
    }
  ],
  "matrix": "base64(float32 bytes, 行数=data长度, 列数=embedding_dim)"
}
```

---

## File Structure

### 修改文件

- `niu_api/internal/lightrag_integrity.py` — 新增 `check_entity_sync` 函数 + `check_all` 调用
- `niu_api/internal/lightrag_repair.py` — 新增 `repair_entity_sync` 函数 + `repair_all` 调用 + import `_decode_vector`
- `tests/test_lightrag_repair.py` — 更新 `test_repair_all_repairs_all_vdbs` 加 GraphML 文件（避免回归失败）

### 新建文件

- `tests/test_lightrag_entity_sync.py` — `check_entity_sync` + `repair_entity_sync` 单元测试

### 不改文件

- `lightrag_manager.py` — `run_resilience_phase1` / `run_repair_on_user_request` 不改
- `kg_api.py` — `/api/kg/lightrag/repair` 端点不改（前端响应体多了 `entity_sync` key，serde 默认忽略未知字段；仓库内无前端 JS/TS 文件，不影响）
- `launcher/src/main.rs` — rfd 弹窗逻辑不改（IntegrityStatus 只读 ok/total_errors，多余字段 serde 默认忽略，无 `deny_unknown_fields`）

---

## Task 1: 写 check_entity_sync 函数（case_mismatch 算 error）

**目标：** 在 `lightrag_integrity.py` 新增 `check_entity_sync`，对比时统一 lower 化。**case_mismatch（vdb 有大写）算 error 触发弹窗**——符合"大写就是 bug"铁律。重复 lower_name（如 "Niu"+"niu"）也报 error。

**Files:**
- Modify: `niu_api/internal/lightrag_integrity.py`（新增函数 + check_all 调用）
- Test: `tests/test_lightrag_entity_sync.py`（新建）

- [ ] **Step 1: 临时备份（铁律 #3）**

```bash
cd <repo_root>
git add -A && git commit -m "backup: 新增 check_entity_sync 前临时备份" || echo "nothing to commit"
```

- [ ] **Step 2: 写失败测试 — vdb 大写但 GraphML 有小写（case_mismatch 算 error）**

创建 `tests/test_lightrag_entity_sync.py`：

```python
"""check_entity_sync + repair_entity_sync 单元测试。

测试维度用 768d（跟真实 bge-base-zh 一致），避免维度相关 bug 漏测。
"""
import base64
import json
import os
import tempfile
import zlib
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest


def _encode_vector_768(vec_f16) -> str:
    """三层编码：base64(zlib(float16 bytes))，模拟 LightRAG vector 字段。"""
    arr = np.array(vec_f16, dtype=np.float16) if not hasattr(vec_f16, 'astype') else vec_f16.astype(np.float16)
    return base64.b64encode(zlib.compress(arr.tobytes())).decode()


def _encode_matrix_768(matrix_f32) -> str:
    """一层编码：base64(float32 bytes)，模拟 LightRAG matrix 字段。"""
    arr = np.array(matrix_f32, dtype=np.float32) if not hasattr(matrix_f32, 'astype') else matrix_f32.astype(np.float32)
    return base64.b64encode(arr.tobytes()).decode()


def _write_vdb(path: Path, data_list: list[dict], embedding_dim: int = 768):
    """写一个 vdb 文件，vector/matrix 自动生成。"""
    vectors = []
    for item in data_list:
        vec = np.full(embedding_dim, 0.1, dtype=np.float16)  # 768d 向量
        item = {**item, "vector": _encode_vector_768(vec)}
        vectors.append(vec)
    matrix = np.array(vectors, dtype=np.float32) if vectors else np.zeros((0, embedding_dim), dtype=np.float32)
    storage = {
        "embedding_dim": embedding_dim,
        "data": data_list,
        "matrix": _encode_matrix_768(matrix),
    }
    path.write_text(json.dumps(storage))


def _write_graphml(path: Path, nodes: list[tuple[str, str, str]]):
    """写一个最小 GraphML 文件。
    nodes: [(node_id, description, source_id), ...]
    node id 已 lower 化（模拟 LightRAG 行为）。
    """
    nodes_xml = "".join(
        f'<node id="{nid}">'
        f'<data key="d0">{nid}</data>'           # entity_id
        f'<data key="d1">entity_type</data>'
        f'<data key="d2">{desc}</data>'          # description
        f'<data key="d3">{src}</data>'           # source_id
        f'</node>'
        for nid, desc, src in nodes
    )
    path.write_text(
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<graphml xmlns="http://graphml.graphdrawing.org/xmlns">'
        f'<key id="d0" for="node" attr.name="entity_id" attr.type="string"/>'
        f'<key id="d1" for="node" attr.name="entity_type" attr.type="string"/>'
        f'<key id="d2" for="node" attr.name="description" attr.type="string"/>'
        f'<key id="d3" for="node" attr.name="source_id" attr.type="string"/>'
        f'<graph>{nodes_xml}</graph>'
        f'</graphml>'
    )


def test_check_entity_sync_case_mismatch_is_error():
    """vdb 用大写 entity_name，GraphML 有小写 node id → 大写就是 bug，case_mismatch 算 error，ok=False。"""
    from niu_api.internal import lightrag_integrity
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_vdb(storage / "vdb_entities.json", [
            {"__id__": "Niu", "entity_name": "Niu", "content": "desc Niu", "source_id": "chunk-1"},
            {"__id__": "Apple", "entity_name": "Apple", "content": "desc Apple", "source_id": "chunk-2"},
        ])
        _write_graphml(storage / "graph_chunk_entity_relation.graphml", [
            ("niu", "desc Niu", "chunk-1"),
            ("apple", "desc Apple", "chunk-2"),
        ])
        with patch.object(lightrag_integrity, "_STORAGE_DIR", storage):
            report = lightrag_integrity.check_entity_sync()
        # 大写就是 bug，必须报 error 触发弹窗
        assert not report["ok"], f"大写 entity_name 应触发 ok=False，实际 ok={report['ok']}"
        case_errors = [e for e in report["errors"] if e.get("check") == "case_mismatch"]
        assert len(case_errors) == 2, f"应有 2 个 case_mismatch error，实际 {len(case_errors)}"
        assert report["stats"]["case_mismatch"] == 2
        assert report["stats"]["orphan_in_vdb"] == 0  # lower 后 GraphML 有对应，不是孤儿
        assert report["stats"]["missing_in_vdb"] == 0


def test_check_entity_sync_duplicate_lower_name():
    """vdb 有 'Niu' 和 'niu'（lower 后冲突）→ 报 duplicate_in_vdb error。"""
    from niu_api.internal import lightrag_integrity
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_vdb(storage / "vdb_entities.json", [
            {"__id__": "Niu", "entity_name": "Niu", "content": "desc Niu 1", "source_id": "chunk-1"},
            {"__id__": "niu", "entity_name": "niu", "content": "desc niu 2", "source_id": "chunk-2"},
        ])
        _write_graphml(storage / "graph_chunk_entity_relation.graphml", [
            ("niu", "desc niu", "chunk-1"),
        ])
        with patch.object(lightrag_integrity, "_STORAGE_DIR", storage):
            report = lightrag_integrity.check_entity_sync()
        assert not report["ok"]
        dup_errors = [e for e in report["errors"] if e.get("check") == "duplicate_in_vdb"]
        assert len(dup_errors) >= 1, "应有 duplicate_in_vdb error"
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd <repo_root>
python -m pytest tests/test_lightrag_entity_sync.py::test_check_entity_sync_case_mismatch_is_error tests/test_lightrag_entity_sync.py::test_check_entity_sync_duplicate_lower_name -v
```

Expected: FAIL with `AttributeError: module 'niu_api.internal.lightrag_integrity' has no attribute 'check_entity_sync'`

- [ ] **Step 4: 写 check_entity_sync 实现（case_mismatch + duplicate 算 error）**

在 `niu_api/internal/lightrag_integrity.py` 的 `check_all` 函数之前（约 L275）插入：

```python
def check_entity_sync() -> dict[str, Any]:
    """检测 vdb_entities 的 entity_name 集合与 GraphML 节点 id 集合的同步性。

    LightRAG 设计上 GraphML node id 全部 lower 化（networkx_impl.py _normalize_node_id）。
    vdb 的 entity_name 应该也 lower 化对齐（用户铁律：所有写入必须转小写）。

    检测逻辑（统一 lower 化后对比）：
    - vdb 大写 entity_name（orig != lower）→ case_mismatch（算 error，触发弹窗修复）
    - vdb 有重复 lower_name（如 'Niu'+'niu'）→ duplicate_in_vdb（算 error）
    - lower 后 vdb 有但 GraphML 没有 → orphan_in_vdb（真孤儿，算 error）
    - lower 后 GraphML 有但 vdb 没有 → missing_in_vdb（缺失向量，算 error）
    """
    report: dict[str, Any] = {"ok": False, "errors": [], "stats": {}}

    storage_dir = _resolve_storage_dir()
    vdb_path = storage_dir / "vdb_entities.json"
    graphml_path = storage_dir / _GRAPHML_FILE

    # 读 vdb_entities：lower_name -> list[原始名]（检测重复）
    vdb_lower_to_orig: dict[str, list[str]] = {}
    if vdb_path.exists():
        try:
            raw = json.loads(vdb_path.read_text(encoding="utf-8"))
            for item in raw.get("data", []):
                name = item.get("entity_name") or item.get("__id__")
                if name:
                    lower = name.lower()
                    vdb_lower_to_orig.setdefault(lower, []).append(name)
        except Exception as e:
            report["errors"].append({"check": "vdb_read", "msg": str(e)})
            return report
    else:
        report["errors"].append({"check": "vdb_missing", "path": str(vdb_path)})
        return report

    # 读 GraphML 节点 id（防御性 lower 化，防外部工具写入大写）
    graphml_names: set[str] = set()
    if graphml_path.exists():
        try:
            tree = ET.parse(graphml_path)
            root = tree.getroot()
            ns = "{http://graphml.graphdrawing.org/xmlns}"
            for node in root.findall(f".//{ns}node"):
                nid = node.get("id")
                if nid:
                    graphml_names.add(nid.lower())
        except Exception as e:
            report["errors"].append({"check": "graphml_read", "msg": str(e)})
            return report
    else:
        report["errors"].append({"check": "graphml_missing", "path": str(graphml_path)})
        return report

    # 统计 case_mismatch + duplicate
    case_mismatch = 0
    duplicates = 0
    for lower_name, orig_list in vdb_lower_to_orig.items():
        for orig in orig_list:
            if orig != lower_name:
                case_mismatch += 1
                report["errors"].append({
                    "check": "case_mismatch",
                    "entity_name": orig,
                    "should_be": lower_name,
                    "hint": "vdb entity_name 未转小写（违反 KG 规范），修复时改小写",
                })
        if len(orig_list) > 1:
            duplicates += 1
            report["errors"].append({
                "check": "duplicate_in_vdb",
                "entity_name": lower_name,
                "origins": orig_list,
                "hint": "vdb 有重复 lower_name（大小写变体），修复时保留已小写条目，丢弃大写重复",
            })

    # lower 后对比
    vdb_lower_names = set(vdb_lower_to_orig.keys())
    orphan_in_vdb = vdb_lower_names - graphml_names
    missing_in_vdb = graphml_names - vdb_lower_names

    for name in sorted(orphan_in_vdb):
        report["errors"].append({
            "check": "orphan_in_vdb",
            "entity_name": name,
            "hint": "vdb 有向量但 GraphML 无对应节点（lower 化后仍无），应从 vdb 删除",
        })
    for name in sorted(missing_in_vdb):
        report["errors"].append({
            "check": "missing_in_vdb",
            "entity_name": name,
            "hint": "GraphML 有节点但 vdb 无向量，应从 GraphML d2(description) 重建向量",
        })

    report["stats"]["vdb_count"] = sum(len(v) for v in vdb_lower_to_orig.values())
    report["stats"]["graphml_count"] = len(graphml_names)
    report["stats"]["case_mismatch"] = case_mismatch
    report["stats"]["duplicate_in_vdb"] = duplicates
    report["stats"]["orphan_in_vdb"] = len(orphan_in_vdb)
    report["stats"]["missing_in_vdb"] = len(missing_in_vdb)
    report["ok"] = len(report["errors"]) == 0
    return report
```

- [ ] **Step 5: 跑测试确认通过**

```bash
python -m pytest tests/test_lightrag_entity_sync.py::test_check_entity_sync_case_mismatch_is_error tests/test_lightrag_entity_sync.py::test_check_entity_sync_duplicate_lower_name -v
```

Expected: PASS

- [ ] **Step 6: 写第三个测试 — 真孤儿**

```python
def test_check_entity_sync_real_orphan():
    """vdb 有实体但 GraphML 完全没有（lower 化后也没有）→ 真孤儿。"""
    from niu_api.internal import lightrag_integrity
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_vdb(storage / "vdb_entities.json", [
            {"__id__": "real_orphan", "entity_name": "real_orphan", "content": "desc", "source_id": "chunk-x"},
        ])
        _write_graphml(storage / "graph_chunk_entity_relation.graphml", [])
        with patch.object(lightrag_integrity, "_STORAGE_DIR", storage):
            report = lightrag_integrity.check_entity_sync()
        assert not report["ok"]
        orphan_names = [e["entity_name"] for e in report["errors"] if e["check"] == "orphan_in_vdb"]
        assert "real_orphan" in orphan_names
```

- [ ] **Step 7: 写第四个测试 — 缺失向量**

```python
def test_check_entity_sync_missing_in_vdb():
    """GraphML 有节点但 vdb 没有对应向量 → missing_in_vdb。"""
    from niu_api.internal import lightrag_integrity
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_vdb(storage / "vdb_entities.json", [])
        _write_graphml(storage / "graph_chunk_entity_relation.graphml", [
            ("ghost", "desc ghost", "chunk-3"),
        ])
        with patch.object(lightrag_integrity, "_STORAGE_DIR", storage):
            report = lightrag_integrity.check_entity_sync()
        assert not report["ok"]
        missing = [e["entity_name"] for e in report["errors"] if e["check"] == "missing_in_vdb"]
        assert "ghost" in missing
```

- [ ] **Step 8: 写第五个测试 — 完全同步 ok=True**

```python
def test_check_entity_sync_perfectly_synced():
    """vdb 全小写且跟 GraphML 完全同步 → ok=True。"""
    from niu_api.internal import lightrag_integrity
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_vdb(storage / "vdb_entities.json", [
            {"__id__": "niu", "entity_name": "niu", "content": "desc", "source_id": "chunk-1"},
            {"__id__": "apple", "entity_name": "apple", "content": "desc", "source_id": "chunk-2"},
        ])
        _write_graphml(storage / "graph_chunk_entity_relation.graphml", [
            ("niu", "desc niu", "chunk-1"),
            ("apple", "desc apple", "chunk-2"),
        ])
        with patch.object(lightrag_integrity, "_STORAGE_DIR", storage):
            report = lightrag_integrity.check_entity_sync()
        assert report["ok"], f"应 ok=True，实际 errors: {report['errors']}"
        assert report["stats"]["case_mismatch"] == 0
        assert report["stats"]["orphan_in_vdb"] == 0
        assert report["stats"]["missing_in_vdb"] == 0
```

- [ ] **Step 9: 跑全部测试**

```bash
python -m pytest tests/test_lightrag_entity_sync.py -v
```

Expected: 全部 PASS

- [ ] **Step 10: check_all 调用 check_entity_sync**

在 `niu_api/internal/lightrag_integrity.py` 的 `check_all` 函数（L276-305）里，在 `graphml_report` 之后加 `entity_sync_report`：

```python
def check_all() -> dict[str, Any]:
    """检测整个 lightrag_storage 目录。"""
    all_errors: list[dict] = []

    vdb_reports: dict[str, Any] = {}
    for fname in _VDB_FILES:
        r = check_vdb(str(_resolve_storage_dir() / fname))
        vdb_reports[fname] = r
        if not r["ok"]:
            all_errors.extend(r["errors"])

    kv_reports: dict[str, Any] = {}
    for fname in _KV_STORE_FILES:
        r = check_kv_store(str(_resolve_storage_dir() / fname))
        kv_reports[fname] = r
        if not r["ok"]:
            all_errors.extend(r["errors"])

    graphml_report = check_graphml(str(_resolve_storage_dir() / _GRAPHML_FILE))
    if not graphml_report["ok"]:
        all_errors.extend(graphml_report["errors"])

    # 新增：vdb_entities 跟 GraphML 实体同步性检测
    entity_sync_report = check_entity_sync()
    if not entity_sync_report["ok"]:
        all_errors.extend(entity_sync_report["errors"])

    return {
        "ok": len(all_errors) == 0,
        "storage_dir": str(_STORAGE_DIR),
        "vdb": vdb_reports,
        "kv_store": kv_reports,
        "graphml": graphml_report,
        "entity_sync": entity_sync_report,
        "total_errors": len(all_errors),
    }
```

- [ ] **Step 11: 跑全部 integrity 测试确认无回归**

```bash
python -m pytest tests/test_lightrag_entity_sync.py tests/test_lightrag_integrity*.py tests/test_lightrag_resilience*.py -v 2>&1 | tail -30
```

Expected: 全部 PASS。老测试只读 ok/total_errors，新增 entity_sync 字段不影响。

- [ ] **Step 12: 提交**

```bash
git add niu_api/internal/lightrag_integrity.py tests/test_lightrag_entity_sync.py
git commit -m "feat(integrity): 新增 check_entity_sync 检测 vdb_entities 跟 GraphML 同步性

LightRAG 设计上 GraphML node id 全部 lower 化，但 vdb entity_name
历史数据没归一化。用户铁律：所有写入必须转小写，大写就是 bug。

检测逻辑（统一 lower 化对比，case_mismatch 算 error 触发弹窗）：
- vdb 大写 entity_name → case_mismatch（error，修复时改小写）
- vdb 重复 lower_name（大小写变体）→ duplicate_in_vdb（error）
- lower 后 vdb 有但 GraphML 没有 → orphan_in_vdb（error）
- lower 后 GraphML 有但 vdb 没有 → missing_in_vdb（error）
check_all 自动包含新检测。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: 写 repair_entity_sync 函数

**目标：** 在 `lightrag_repair.py` 新增 `repair_entity_sync`：①vdb 大写改小写（matrix 不动）②vdb 重复 lower_name 优先保留已小写条目，丢弃大写重复 ③真孤儿删除 ④缺失向量从 GraphML d2 重建，source_id 用 d3。备份用 `shutil.copy2`（不先 unlink），备份失败 abort。corrupt.bak 加时间戳。

**Files:**
- Modify: `niu_api/internal/lightrag_repair.py`（新增函数 + repair_all 调用 + import `_decode_vector`）
- Modify: `tests/test_lightrag_repair.py`（更新 `test_repair_all_repairs_all_vdbs` 加 GraphML）
- Test: `tests/test_lightrag_entity_sync.py`（追加测试）

- [ ] **Step 1: 临时备份**

```bash
cd <repo_root>
git add -A && git commit -m "backup: 新增 repair_entity_sync 前临时备份" || echo "nothing to commit"
```

- [ ] **Step 2: 写失败测试 — 大写改小写（无数据丢失）**

在 `tests/test_lightrag_entity_sync.py` 追加：

```python
def test_repair_entity_sync_case_rename(monkeypatch):
    """vdb 用大写 entity_name，GraphML 有小写 → 修复后 vdb 改小写，matrix 和向量不变。"""
    from niu_api.internal import lightrag_integrity, lightrag_repair
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_vdb(storage / "vdb_entities.json", [
            {"__id__": "Niu", "entity_name": "Niu", "content": "desc Niu", "source_id": "chunk-1"},
            {"__id__": "Apple", "entity_name": "Apple", "content": "desc Apple", "source_id": "chunk-2"},
        ])
        _write_graphml(storage / "graph_chunk_entity_relation.graphml", [
            ("niu", "desc Niu", "chunk-1"),
            ("apple", "desc Apple", "chunk-2"),
        ])
        monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", storage)
        monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))

        # 备份原 matrix 验证不变
        orig_vdb = json.loads((storage / "vdb_entities.json").read_text())
        orig_matrix = np.frombuffer(base64.b64decode(orig_vdb["matrix"]), dtype=np.float32).reshape(-1, orig_vdb["embedding_dim"])

        result = lightrag_repair.repair_entity_sync()

        assert result["status"] == "ok"
        assert result["renamed"] == 2
        assert result["removed"] == 0
        assert result["rebuilt"] == 0
        # 验证 vdb entity_name 改小写
        vdb = json.loads((storage / "vdb_entities.json").read_text())
        names = [item["entity_name"] for item in vdb["data"]]
        assert "niu" in names
        assert "apple" in names
        assert "Niu" not in names
        # 验证 matrix 值不变（用 allclose 容忍 float16→float32 精度）
        new_matrix = np.frombuffer(base64.b64decode(vdb["matrix"]), dtype=np.float32).reshape(-1, vdb["embedding_dim"])
        assert new_matrix.shape == orig_matrix.shape
        assert np.allclose(new_matrix, orig_matrix, rtol=1e-3)
```

- [ ] **Step 3: 跑测试确认失败**

```bash
python -m pytest tests/test_lightrag_entity_sync.py::test_repair_entity_sync_case_rename -v
```

Expected: FAIL with `AttributeError: module 'niu_api.internal.lightrag_repair' has no attribute 'repair_entity_sync'`

- [ ] **Step 4: 写 repair_entity_sync 实现**

在 `niu_api/internal/lightrag_repair.py` 顶部 import 区加（如果还没有）：

```python
from niu_api.internal.lightrag_integrity import _decode_vector
```

在 `repair_all` 函数之前（约 L240）插入：

```python
def repair_entity_sync() -> dict[str, Any]:
    """修复 vdb_entities 跟 GraphML 的实体同步性。

    LightRAG 设计上 GraphML node id 全部 lower 化。用户铁律：所有写入必须转小写。
    修复策略（以 GraphML 为真相源，统一小写）：
    1. vdb 大写但 GraphML 有小写 → 改 vdb __id__/entity_name 为小写（matrix 不动，向量不变）
    2. vdb 有重复 lower_name（如 'Niu'+'niu'）→ 优先保留已小写条目（orig==lower），丢弃大写重复
    3. lower 后 vdb 有但 GraphML 没有（真孤儿）→ 删除条目 + matrix 对应行
    4. lower 后 GraphML 有但 vdb 没有（缺失向量）→ 从 GraphML d2(description) 重新 embedding，
       source_id 用 d3 真实 chunk-id

    Returns:
        {"status": "ok"|"error", "renamed": int, "removed": int, "rebuilt": int, "message": str}
    """
    import time
    import xml.etree.ElementTree as ET
    import numpy as np

    storage_dir = _storage_dir()
    vdb_path = storage_dir / "vdb_entities.json"
    graphml_path = storage_dir / "graph_chunk_entity_relation.graphml"

    if not vdb_path.exists() or not graphml_path.exists():
        return {"status": "error", "message": "vdb_entities 或 GraphML 不存在", "renamed": 0, "removed": 0, "rebuilt": 0}

    # 1. 读 vdb
    try:
        raw = json.loads(vdb_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"status": "error", "message": f"vdb 读取失败: {e}", "renamed": 0, "removed": 0, "rebuilt": 0}

    embedding_dim = raw.get("embedding_dim")
    data_list = raw.get("data", [])
    if not isinstance(embedding_dim, int) or not isinstance(data_list, list):
        return {"status": "error", "message": "vdb 格式异常", "renamed": 0, "removed": 0, "rebuilt": 0}

    # 2. 读 GraphML：node id + d2(description) + d3(source_id)
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    graphml_nodes: dict[str, tuple[str, str]] = {}  # lower_name -> (description, source_id)
    try:
        tree = ET.parse(graphml_path)
        root = tree.getroot()
        for node in root.findall(f".//{ns}node"):
            nid = node.get("id")
            if not nid:
                continue
            desc = ""
            src = ""
            for data in node.findall(f"{ns}data"):
                key = data.get("key")
                if key == "d2":
                    desc = data.text or ""
                elif key == "d3":
                    src = data.text or ""
            graphml_nodes[nid.lower()] = (desc, src)
    except Exception as e:
        return {"status": "error", "message": f"GraphML 读取失败: {e}", "renamed": 0, "removed": 0, "rebuilt": 0}

    graphml_lower_names = set(graphml_nodes.keys())

    # 3. 分类 vdb 条目（按 lower_name 分组，检测重复）
    grouped: dict[str, list[dict]] = {}
    for item in data_list:
        orig_name = item.get("entity_name") or item.get("__id__")
        if not orig_name:
            continue
        lower_name = orig_name.lower()
        grouped.setdefault(lower_name, []).append(item)

    renamed = 0
    removed = 0
    rebuilt = 0
    new_data: list[dict] = []
    new_vectors: list[np.ndarray] = []

    for lower_name, items in grouped.items():
        # 真孤儿（lower 后 GraphML 没有）→ 跳过（删除）
        if lower_name not in graphml_lower_names:
            removed += len(items)
            continue

        # 重复时优先保留已小写条目（orig==lower），没有则用第一个
        chosen = None
        for it in items:
            orig = it.get("entity_name") or it.get("__id__")
            if orig == lower_name:
                chosen = it
                break
        if chosen is None:
            chosen = items[0]
        # 丢弃的重复条目计入 removed（大写重复是 bug，丢弃是修正）
        removed += len(items) - 1

        # 大写改小写
        new_item = {k: v for k, v in chosen.items() if k != "vector"}
        orig_name = chosen.get("entity_name") or chosen.get("__id__")
        new_item["__id__"] = lower_name
        new_item["entity_name"] = lower_name
        if orig_name != lower_name:
            renamed += 1

        # 解码原向量保留
        try:
            vec = _decode_vector(chosen.get("vector", ""), embedding_dim)
            new_vectors.append(np.array(vec, dtype=np.float16))
        except Exception:
            # 向量损坏，用 content 重新 embedding
            try:
                vec = _embed_text(chosen.get("content", ""))
                new_vectors.append(np.array(vec, dtype=np.float16))
                rebuilt += 1
            except Exception as e:
                logger.warning(f"[LightRAGRepair] 重建 {lower_name} 向量失败: {e}，跳过")
                continue
        new_data.append(new_item)

    # 4. 缺失向量：GraphML 有但 vdb 没有
    for lower_name, (desc, src) in graphml_nodes.items():
        if lower_name in grouped:
            continue
        if not desc:
            logger.warning(f"[LightRAGRepair] GraphML 节点 {lower_name} 无 d2(description)，跳过")
            continue
        try:
            vec = _embed_text(desc)
        except Exception as e:
            logger.warning(f"[LightRAGRepair] 重建 {lower_name} embedding 失败: {e}，跳过")
            continue
        new_data.append({
            "__id__": lower_name,
            "entity_name": lower_name,
            "content": desc,
            "source_id": src or f"repair-from-graphml-{lower_name}",
        })
        new_vectors.append(np.array(vec, dtype=np.float16))
        rebuilt += 1

    if not new_data:
        return {"status": "error", "message": "修复后无数据", "renamed": renamed, "removed": removed, "rebuilt": rebuilt}

    # 5. 备份损坏 vdb（用 shutil.copy2，不先 unlink；加时间戳防覆盖）
    timestamp = int(time.time() * 1000)  # 毫秒级，防 1 秒内连续 repair 覆盖
    corrupt_bak = storage_dir / f"vdb_entities.json.corrupt.{timestamp}.bak"
    try:
        shutil.copy2(vdb_path, corrupt_bak)  # copy2 自动覆盖已存在目标
        logger.info(f"[LightRAGRepair] 损坏 vdb 备份到: {corrupt_bak}")
    except Exception as e:
        # 备份失败立即 abort，不继续写新 vdb（避免数据丢失）
        logger.error(f"[LightRAGRepair] 备份损坏 vdb 失败: {e}，abort 修复")
        return {"status": "error", "message": f"备份失败: {e}", "renamed": renamed, "removed": removed, "rebuilt": rebuilt}

    # 6. 原子写新 vdb
    matrix_f32 = np.array(new_vectors, dtype=np.float32)
    for i, item in enumerate(new_data):
        item["vector"] = _encode_vector(new_vectors[i])

    storage = {
        "embedding_dim": embedding_dim,
        "data": new_data,
        "matrix": _encode_matrix(matrix_f32),
    }
    tmp_file = vdb_path.with_name(vdb_path.name + ".tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(storage, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_file, vdb_path)

    logger.info(
        f"[LightRAGRepair] 实体同步修复完成: 改名 {renamed}, 删除 {removed}（含重复丢弃）, 重建 {rebuilt}, 总计 {len(new_data)}"
    )
    return {
        "status": "ok",
        "renamed": renamed,
        "removed": removed,
        "rebuilt": rebuilt,
        "message": f"改大小写 {renamed} 条，删除孤儿/重复 {removed} 条，重建 {rebuilt} 条",
    }
```

**关键设计**：
- 用 `shutil.copy2` 备份（不先 unlink，copy2 自动覆盖已存在目标）
- `corrupt_bak` 加时间戳后缀（`corrupt.{timestamp}.bak`），不被下次 repair 覆盖
- 备份失败立即 abort（不继续写新 vdb，避免数据丢失）
- 重复 lower_name 优先保留已小写条目（`orig == lower`），大写重复丢弃计入 removed
- 重建向量用 GraphML d2(description)，source_id 用 d3 真实 chunk-id
- 大写改小写时 matrix 不动（向量不变），只改 `__id__`/`entity_name` 字段

- [ ] **Step 5: 跑测试确认通过**

```bash
python -m pytest tests/test_lightrag_entity_sync.py::test_repair_entity_sync_case_rename -v
```

Expected: PASS

- [ ] **Step 6: 写第二个测试 — 重复 lower_name 优先保留小写**

```python
def test_repair_entity_sync_duplicate_prefer_lowercase(monkeypatch):
    """vdb 有 'Niu'+'niu'（重复 lower_name）→ 保留 'niu'（已小写），丢弃 'Niu'。"""
    from niu_api.internal import lightrag_integrity, lightrag_repair
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_vdb(storage / "vdb_entities.json", [
            {"__id__": "Niu", "entity_name": "Niu", "content": "desc Niu uppercase", "source_id": "chunk-1"},
            {"__id__": "niu", "entity_name": "niu", "content": "desc niu lowercase", "source_id": "chunk-2"},
        ])
        _write_graphml(storage / "graph_chunk_entity_relation.graphml", [
            ("niu", "desc niu", "chunk-1"),
        ])
        monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", storage)
        monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))

        result = lightrag_repair.repair_entity_sync()

        assert result["status"] == "ok"
        assert result["removed"] == 1  # 丢弃大写重复
        vdb = json.loads((storage / "vdb_entities.json").read_text())
        # 应保留 'niu'（小写），content 是 lowercase 那条
        niu_item = next(i for i in vdb["data"] if i["entity_name"] == "niu")
        assert niu_item["content"] == "desc niu lowercase"
        assert len(vdb["data"]) == 1  # 只剩 1 个
```

- [ ] **Step 7: 跑测试确认通过**

```bash
python -m pytest tests/test_lightrag_entity_sync.py::test_repair_entity_sync_duplicate_prefer_lowercase -v
```

Expected: PASS

- [ ] **Step 8: 写第三个测试 — 删真孤儿**

```python
def test_repair_entity_sync_remove_orphan(monkeypatch):
    """vdb 有真孤儿（lower 后 GraphML 也没有）→ 删除，matrix 同步。"""
    from niu_api.internal import lightrag_integrity, lightrag_repair
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_vdb(storage / "vdb_entities.json", [
            {"__id__": "keep", "entity_name": "keep", "content": "desc keep", "source_id": "chunk-1"},
            {"__id__": "orphan", "entity_name": "orphan", "content": "desc orphan", "source_id": "chunk-x"},
        ])
        _write_graphml(storage / "graph_chunk_entity_relation.graphml", [
            ("keep", "desc keep", "chunk-1"),
        ])
        monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", storage)
        monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))

        result = lightrag_repair.repair_entity_sync()

        assert result["status"] == "ok"
        assert result["removed"] == 1
        vdb = json.loads((storage / "vdb_entities.json").read_text())
        names = [item["entity_name"] for item in vdb["data"]]
        assert "keep" in names
        assert "orphan" not in names
        matrix_bytes = base64.b64decode(vdb["matrix"])
        matrix = np.frombuffer(matrix_bytes, dtype=np.float32).reshape(-1, vdb["embedding_dim"])
        assert matrix.shape[0] == len(vdb["data"])
```

- [ ] **Step 9: 跑测试确认通过**

```bash
python -m pytest tests/test_lightrag_entity_sync.py::test_repair_entity_sync_remove_orphan -v
```

Expected: PASS

- [ ] **Step 10: 写第四个测试 — 重建缺失向量**

```python
def test_repair_entity_sync_rebuild_missing(monkeypatch):
    """GraphML 有节点但 vdb 没向量 → 从 d2(description) 重建，source_id 用 d3。"""
    from niu_api.internal import lightrag_integrity, lightrag_repair
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_vdb(storage / "vdb_entities.json", [
            {"__id__": "exists", "entity_name": "exists", "content": "desc", "source_id": "chunk-1"},
        ])
        _write_graphml(storage / "graph_chunk_entity_relation.graphml", [
            ("exists", "desc exists", "chunk-1"),
            ("ghost", "desc ghost", "chunk-3"),  # vdb 没有
        ])
        monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", storage)
        monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))

        def fake_embed(text):
            return [0.5] * 768
        monkeypatch.setattr(lightrag_repair, "_embed_text", fake_embed)

        result = lightrag_repair.repair_entity_sync()

        assert result["status"] == "ok"
        assert result["rebuilt"] == 1
        vdb = json.loads((storage / "vdb_entities.json").read_text())
        names = [item["entity_name"] for item in vdb["data"]]
        assert "exists" in names
        assert "ghost" in names
        ghost_item = next(i for i in vdb["data"] if i["entity_name"] == "ghost")
        assert ghost_item["source_id"] == "chunk-3"  # 用 GraphML d3 真实 chunk-id
```

- [ ] **Step 11: 跑测试确认通过**

```bash
python -m pytest tests/test_lightrag_entity_sync.py::test_repair_entity_sync_rebuild_missing -v
```

Expected: PASS

- [ ] **Step 12: 写第五个测试 — 无需修复**

```python
def test_repair_entity_sync_noop(monkeypatch):
    """vdb 和 GraphML 已同步（全小写，无重复）→ 无需修复。"""
    from niu_api.internal import lightrag_integrity, lightrag_repair
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_vdb(storage / "vdb_entities.json", [
            {"__id__": "synced", "entity_name": "synced", "content": "desc", "source_id": "chunk-1"},
        ])
        _write_graphml(storage / "graph_chunk_entity_relation.graphml", [
            ("synced", "desc synced", "chunk-1"),
        ])
        monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", storage)
        monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))

        result = lightrag_repair.repair_entity_sync()

        assert result["status"] == "ok"
        assert result["renamed"] == 0
        assert result["removed"] == 0
        assert result["rebuilt"] == 0
```

- [ ] **Step 13: 跑全部测试**

```bash
python -m pytest tests/test_lightrag_entity_sync.py -v
```

Expected: 全部 PASS

- [ ] **Step 14: repair_all 调用 repair_entity_sync + 更新老测试**

在 `niu_api/internal/lightrag_repair.py` 的 `repair_all` 函数（L241-246）里加 `repair_entity_sync`：

```python
def repair_all() -> dict[str, Any]:
    """一键修复所有 vdb。"""
    results: dict[str, Any] = {}
    for vdb_file in _VDB_TEXT_FIELD:
        results[vdb_file] = repair_vdb(vdb_file)
    # 新增：实体同步性修复
    results["entity_sync"] = repair_entity_sync()
    return results
```

**更新老测试** `tests/test_lightrag_repair.py` 的 `test_repair_all_repairs_all_vdbs`：

读该测试，确认它 monkeypatch `_STORAGE_DIR` 但没写 GraphML 文件。修复方式：在该测试的临时目录里写一个空 GraphML 文件（用 `_write_graphml` helper）：

```python
# 在 test_repair_all_repairs_all_vdbs 里，设置 _STORAGE_DIR 后加：
from tests.test_lightrag_entity_sync import _write_graphml
_write_graphml(storage / "graph_chunk_entity_relation.graphml", [])
```

这样 `repair_entity_sync` 会因为 vdb 有数据但 GraphML 为空，把 vdb 条目全当孤儿删掉，返回 `status: ok, removed: N`。如果该测试断言 vdb 数据保留，需调整断言。**执行者读该测试代码后决定具体改法**——关键是让 `repair_all` 调 `repair_entity_sync` 后该测试仍 pass。

- [ ] **Step 15: 跑全部 repair 测试确认无回归**

```bash
python -m pytest tests/test_lightrag_entity_sync.py tests/test_lightrag_repair*.py tests/test_lightrag_resilience*.py -v 2>&1 | tail -30
```

Expected: 全部 PASS

- [ ] **Step 16: 提交**

```bash
git add niu_api/internal/lightrag_repair.py tests/test_lightrag_entity_sync.py tests/test_lightrag_repair.py
git commit -m "feat(repair): 新增 repair_entity_sync 修复 vdb_entities 跟 GraphML 同步性

修复策略（以 GraphML 为真相源，统一小写）：
1. vdb 大写但 GraphML 有小写 → 改 vdb __id__/entity_name 为小写（matrix 不动）
2. vdb 有重复 lower_name（如 'Niu'+'niu'）→ 优先保留已小写条目，丢弃大写重复
3. lower 后 vdb 有但 GraphML 没有（真孤儿）→ 删除条目 + matrix 同步
4. lower 后 GraphML 有但 vdb 没有 → 从 GraphML d2(description) 重新 embedding，
   source_id 用 d3 真实 chunk-id

备份用 shutil.copy2（不先 unlink，copy2 自动覆盖），corrupt.bak 加时间戳防覆盖，
备份失败立即 abort。repair_all 自动包含新修复。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: 端到端验证（真实数据）

**目标：** 用真实 `~/.niu/lightrag_storage/` 数据验证检测+修复走通。

**Files:**
- 无改动（只跑验证）

- [ ] **Step 1: 临时备份真实数据**

```bash
cp ~/.niu/lightrag_storage/vdb_entities.json ~/.niu/lightrag_storage/vdb_entities.json.bak.before-sync-repair
```

- [ ] **Step 2: 跑 check_all 看是否检出同步性问题**

```bash
cd <repo_root>
python3 -c "
from niu_api.internal.lightrag_integrity import check_all
import json
r = check_all()
print(json.dumps({
    'ok': r['ok'],
    'total_errors': r['total_errors'],
    'vdb_ok': r['vdb'].get('vdb_entities.json', {}).get('ok'),
    'entity_sync': r.get('entity_sync', {}).get('stats', {}),
}, indent=2, ensure_ascii=False))
"
```

Expected:
- `ok: False`（case_mismatch 算 error，触发弹窗）
- `vdb_ok: True`（vdb 文件结构本身没坏）
- `entity_sync.case_mismatch: 157`（实测 157 个大小写不一致）
- `entity_sync.orphan_in_vdb: 0`（lower 后没有真孤儿）
- `entity_sync.missing_in_vdb: 0`

**如果 `vdb_ok: False`**：先跑 `repair_vdb("vdb_entities.json")` 修 vdb 结构，再跑 check_all。

- [ ] **Step 3: 跑 repair_entity_sync 修复**

```bash
python3 -c "
from niu_api.internal.lightrag_repair import repair_entity_sync
import json
r = repair_entity_sync()
print(json.dumps(r, indent=2, ensure_ascii=False))
"
```

Expected: `status: ok`，`renamed: 157`（大小写改小写），`removed: 0`，`rebuilt: 0`

- [ ] **Step 4: 再跑 check_all 确认 ok=True**

```bash
python3 -c "
from niu_api.internal.lightrag_integrity import check_all
r = check_all()
print(f'ok={r[\"ok\"]}, total_errors={r[\"total_errors\"]}')
print(f'entity_sync: {r[\"entity_sync\"][\"stats\"]}')
"
```

Expected: `ok=True`，`case_mismatch=0`，`orphan_in_vdb=0`，`missing_in_vdb=0`

- [ ] **Step 5: 启动 ./niu 验证不再报 "nodes are missing"**

```bash
./niu
```

观察日志，触发一次 LightRAG 查询（如说"查一下知识库"），确认不再出现 `WARNING: Some nodes are missing, maybe the storage is damaged`。

- [ ] **Step 6: 验证通过后保留备份 24 小时**

```bash
ls -la ~/.niu/lightrag_storage/vdb_entities.json.bak.before-sync-repair
echo "备份保留 24 小时，确认无问题后手动删：rm ~/.niu/lightrag_storage/vdb_entities.json.bak.before-sync-repair"
```

**注意**：不立即删备份。如果 Step 5 验证失败（仍报 "nodes are missing"），回退：

```bash
cp ~/.niu/lightrag_storage/vdb_entities.json.bak.before-sync-repair ~/.niu/lightrag_storage/vdb_entities.json
```

报告失败，等用户决定。

- [ ] **Step 7: 提交验证结果**

```bash
git add -A
git commit -m "test(integrity): 端到端验证实体同步性检测+修复走通

真实数据实测：157 个大小写不一致（vdb 大写 entity_name vs GraphML 小写 node id），
修复后全部改为小写，check_all ok=True，启动后 LightRAG 不再报 'nodes are missing'。
无真孤儿，无缺失向量。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>" || echo "nothing to commit"
```

---

## Self-Review

### 1. Spec coverage 检查

- ✅ vdb 大写但 GraphML 有小写 → 改 vdb 小写（Task 2 Step 4 分类 1）
- ✅ vdb 有重复 lower_name → 优先保留已小写，丢弃大写重复（Task 2 Step 4 分类 2）
- ✅ vdb 有真孤儿 → 删（Task 2 Step 4 分类 3）
- ✅ GraphML 有但 vdb 没有 → 从 d2 重建，source_id 用 d3（Task 2 Step 4 分类 4）
- ✅ 以 GraphML 为真相源，统一小写（Task 2 函数注释 + 修复策略）
- ✅ case_mismatch 算 error 触发弹窗（Task 1 Step 4，符合"大写就是 bug"铁律）
- ✅ duplicate_in_vdb 算 error（Task 1 Step 4）
- ✅ 启动检测包含新检测（Task 1 Step 10 check_all 调用）
- ✅ 修复程序包含新修复（Task 2 Step 14 repair_all 调用）
- ✅ GraphML content 字段用 d2（实测确认，不是 d0）
- ✅ source_id 用 d3 真实 chunk-id（不伪造）
- ✅ 备份用 shutil.copy2（不先 unlink，copy2 自动覆盖）
- ✅ corrupt.bak 加时间戳（不被覆盖）
- ✅ 备份失败 abort（不继续写）
- ✅ _decode_vector import（Task 2 Step 4 顶部 import）
- ✅ 老测试 test_repair_all_repairs_all_vdbs 不回归（Task 2 Step 14 处理）
- ✅ 测试用 768d 向量（跟真实 bge-base-zh 一致）
- ✅ matrix 断言用 np.allclose（容忍 float16→float32 精度）
- ✅ 小写化边界明确（仅 entity_name/__id__，content/source_id/file_path 保留）
- ✅ GraphML node id 防御性 lower（check_entity_sync L `nid.lower()`）
- ✅ 端到端验证（Task 3）

### 2. Placeholder 检查

- 无 "TBD"、"TODO"、"implement later"
- Task 2 Step 14 的老测试更新给了明确方案（写空 GraphML + 调整断言），执行者读代码后决定

### 3. Type consistency 检查

- `check_entity_sync` 返回 `{"ok": bool, "errors": list, "stats": dict}` — 与其他 check 函数一致
- `repair_entity_sync` 返回 `{"status": "ok"|"error", "renamed": int, "removed": int, "rebuilt": int, "message": str}` — `renamed`/`removed`/`rebuilt` 三个计数清晰
- `_STORAGE_DIR` patch：Task 1 用 `patch.object(lightrag_integrity, "_STORAGE_DIR", storage)`（Path），Task 2 用 `monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))`（str）——跟两个模块的 `_resolve_storage_dir()`/`_storage_dir()` 返回 `Path(_STORAGE_DIR)` 一致
- `case_mismatch` / `duplicate_in_vdb` / `orphan_in_vdb` / `missing_in_vdb` 在 check 和 repair 里命名一致

### 4. 已知边界

- **vdb_relationships 跟 GraphML 边的同步性**：本计划不覆盖（id 格式不同，复杂度高，留后续任务）
- **embedding 模型未加载时的 fallback**：`_embed_text` 已有 fallback 逻辑（L48-66），复用即可
- **修复时 LightRAG 实例内存句柄**：`run_repair_on_user_request`（lightrag_manager.py L1023-1040）已调 `reset_init_state()` + `get_lightrag()` 重试初始化，会重新加载 vdb。本计划的 `repair_entity_sync` 在 `run_repair_on_user_request` 链路里被调用，复用此机制
- **前端响应兼容**：仓库内无前端 JS/TS 文件，`/api/kg/lightrag/repair` 响应体多了 `entity_sync` key 不影响（前端不存在或在外部仓库）

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-08-lightrag-entity-sync.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?

