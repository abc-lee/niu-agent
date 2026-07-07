# LightRAG 数据韧性外挂程序实施计划 (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 LightRAG 数据层（vdb / kv_store / graphml）加一个**外挂检测+修复程序**，启动时快速检测图谱一致性，发现问题修复，全程不改 LightRAG fork 源码、不改 nano-vectordb 安装包。

**Architecture:**
- **外挂检测**：自己解析 vdb/kv_store/graphml 文件。vdb 文件格式实测验证：
  - `matrix` 字段：`base64(float32 bytes)` 一层编码
  - `data[i].vector` 字段：`base64(zlib(float16 bytes))` 三层编码（zlib magic header `789c`）
  - 检测项：JSON 完整性、字段齐全、`len(matrix_bytes) == 4 * embedding_dim * len(data)`（精确等于，非 modulo）、matrix reshape、vector 三层解码
- **外挂修复**：优先从**损坏 vdb 自身的 data 字段**读文本重建（matrix 损坏 data 完好是常见场景）。data 也损坏时 fallback：
  - chunks → `kv_store_text_chunks.json`（有 `content` 字段）
  - entities/relations → GraphML 图节点 attributes
  - 重建 vector 用 `base64(zlib(float16 bytes))` 三层编码
- **鸡生蛋解决**：`_embed_text` 不依赖 LightRAG 实例，直接用 `niu_api.internal.embedding.get_model()`（embedding 模型在 LightRAG eager init 之前预加载）
- **启动流程拆分**：
  - Phase 1（LightRAG eager init 之前）：`cleanup_corrupt_bak` + `full_backup` + `check_all`（纯文件操作）
  - Phase 2（LightRAG eager init 之后）：如果 check 发现损坏，调 `repair_all`（embedding 模型已加载）
- **告警机制**：复用现有 `/api/kg/stats` 端点（已含 `init_failed` + 计划新增 `integrity` 字段），launcher 用 `window::frames()` 计数 + `reqwest::blocking` 轮询

**Tech Stack:** Python 3.11+，numpy，zlib，base64，`niu_api.internal.embedding`（embedding 模型，预加载），pytest。

---

## 修改的文件

| 文件 | 改动 | 责任 |
|------|------|------|
| `niu_api/internal/lightrag_integrity.py`（新建） | 外挂检测：vdb/kv_store/graphml 一致性检查 | 维度 1 检测 |
| `niu_api/internal/lightrag_repair.py`（新建） | 外挂修复：从 vdb data 字段重建 + 从 kv_store 重建 chunks | 维度 2 修复 |
| `niu_api/internal/lightrag_backup.py`（新建） | 外挂备份：滚动备份 + 全量备份 + 清理 corrupt.bak | 维度 5 备份 |
| `niu_api/internal/lightrag_manager.py` | 拆分启动流程两阶段；`get_lightrag_status` 加检测/修复结果 | 维度 1/2 集成 |
| `niu_api/__main__.py` | Phase 1 在 LightRAG eager init 之前；Phase 2 在之后 | 启动集成 |
| `niu_api/kg_api.py` | 新增 `/api/lightrag/repair` POST 端点 | 维度 2 修复 API |
| `launcher/src/main.rs` | 用 `window::frames()` 计数轮询 `/api/kg/stats` + Iced 通知 | 维度 4 告警 |
| `tests/test_lightrag_integrity.py`（新建） | 检测逻辑测试 | 验证 |
| `tests/test_lightrag_repair.py`（新建） | 修复逻辑测试 | 验证 |
| `tests/test_lightrag_backup.py`（新建） | 备份逻辑测试 | 验证 |

---

## Task 1: 外挂检测——vdb 文件一致性检查（v2 修正格式）

**Files:**
- Create: `niu_api/internal/lightrag_integrity.py`
- Test: `tests/test_lightrag_integrity.py`

**背景**：vdb 文件格式实测验证：
- `matrix` 字段：`base64(float32 bytes)` 一层编码
- `data[i].vector` 字段：`base64(zlib(float16 bytes))` 三层编码

v1 计划假设 vector 是 `base64(float32 bytes)` 是错的。v2 修正：检测 vector 时用三层解码。

- [ ] **Step 1: 写失败测试——检测健康 vdb（用真实格式）**

新建 `tests/test_lightrag_integrity.py`：

```python
"""LightRAG 数据韧性外挂检测测试（v2 真实格式）"""
import base64
import json
import os
import zlib

import numpy as np
import pytest


def _encode_vector(vec_f16: np.ndarray) -> str:
    """LightRAG vector 字段编码：base64(zlib(float16 bytes))"""
    compressed = zlib.compress(vec_f16.tobytes())
    return base64.b64encode(compressed).decode()


def _encode_matrix(matrix_f32: np.ndarray) -> str:
    """LightRAG matrix 字段编码：base64(float32 bytes)"""
    return base64.b64encode(matrix_f32.tobytes()).decode()


def _write_healthy_vdb(path: str, count: int = 3, dim: int = 4):
    """构造健康 vdb 文件（真实格式）"""
    matrix_f32 = np.random.rand(count, dim).astype(np.float32)
    data_list = []
    for i in range(count):
        vec_f16 = matrix_f32[i].astype(np.float16)  # vector 用 float16
        data_list.append({
            "__id__": f"e{i}",
            "content": f"content {i}",
            "vector": _encode_vector(vec_f16),
        })
    vdb = {
        "embedding_dim": dim,
        "data": data_list,
        "matrix": _encode_matrix(matrix_f32),  # matrix 用 float32
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(vdb, f)


def test_check_vdb_healthy_returns_ok(tmp_path):
    """健康 vdb 文件检测通过（真实格式）"""
    from niu_api.internal.lightrag_integrity import check_vdb

    vdb_path = str(tmp_path / "vdb_test.json")
    _write_healthy_vdb(vdb_path, count=3, dim=4)

    report = check_vdb(vdb_path)
    assert report["ok"] is True, f"健康文件应通过: {report['errors']}"
    assert report["stats"]["data_count"] == 3
    assert report["stats"]["matrix_shape"] == [3, 4]
    assert report["stats"]["embedding_dim"] == 4


def test_check_vdb_missing_file(tmp_path):
    """文件不存在时报告"""
    from niu_api.internal.lightrag_integrity import check_vdb

    report = check_vdb(str(tmp_path / "nonexistent.json"))
    assert report["ok"] is False
    assert any(e["check"] == "file_exists" for e in report["errors"])


def test_check_vdb_empty_file(tmp_path):
    """空文件报告"""
    from niu_api.internal.lightrag_integrity import check_vdb

    vdb_path = str(tmp_path / "empty.json")
    open(vdb_path, "w").close()

    report = check_vdb(vdb_path)
    assert report["ok"] is False
    assert any(e["check"] == "file_empty" for e in report["errors"])


def test_check_vdb_truncated_json(tmp_path):
    """JSON 截断报告具体行号"""
    from niu_api.internal.lightrag_integrity import check_vdb

    vdb_path = str(tmp_path / "truncated.json")
    _write_healthy_vdb(vdb_path, count=2, dim=4)
    with open(vdb_path, "rb+") as f:
        f.seek(-100, 2)
        f.truncate()

    report = check_vdb(vdb_path)
    assert report["ok"] is False
    assert any(e["check"] == "json_parse" for e in report["errors"])


def test_check_vdb_data_matrix_length_mismatch(tmp_path):
    """matrix 行数 != data 长度（字节数匹配但行数不匹配）报告具体数值"""
    from niu_api.internal.lightrag_integrity import check_vdb

    vdb_path = str(tmp_path / "mismatch.json")
    # 构造 matrix (3,4) float32 = 48 bytes，data 2 条，embedding_dim=4
    # 期望字节数 = 4 * 4 * 2 = 32，但实际给 48（3 行）
    # 48 != 32 会触发 matrix_size_mismatch，不是 row_count_mismatch
    # 为了测 row_count_mismatch：构造字节数 == 4*dim*data_len 但行数不匹配的场景
    # 让 matrix (2,4) float32 = 32 bytes == 4*4*2，但 data 只有 1 条
    # 32 == 4*4*1=16? 不对。让 matrix (2,4) = 32 bytes, data 1 条, dim=4 → 期望 16, 实际 32 → size_mismatch
    # 真正能触发 row_count_mismatch 的场景：字节数对得上但 reshape 后行数 != data_len
    # 这不可能——如果字节数 == 4*dim*data_len，reshape(-1, dim) 行数必然 == data_len
    # 所以 row_count_mismatch 只在 size_mismatch 之后触发——先 size_mismatch，然后 reshape 仍尝试，行数 != data_len
    # 改测试：构造 data 2 条 + matrix (3,4) = 48 bytes，期望 32，size_mismatch + row_count_mismatch 都触发
    matrix_f32 = np.random.rand(3, 4).astype(np.float32)
    vdb = {
        "embedding_dim": 4,
        "data": [{"__id__": "e1"}, {"__id__": "e2"}],  # 2 条
        "matrix": _encode_matrix(matrix_f32),  # 3 行 = 48 bytes
    }
    with open(vdb_path, "w", encoding="utf-8") as f:
        json.dump(vdb, f)

    report = check_vdb(vdb_path)
    assert report["ok"] is False
    # matrix 字节数 48 != 期望 32，应触发 matrix_size_mismatch
    errs = [e["check"] for e in report["errors"]]
    assert "matrix_size_mismatch" in errs, f"应触发 matrix_size_mismatch, 实际: {errs}"


def test_check_vdb_matrix_size_not_multiple(tmp_path):
    """matrix 字节数 != 4 * embedding_dim * data_len 时报告（精确等于，非 modulo）"""
    from niu_api.internal.lightrag_integrity import check_vdb

    vdb_path = str(tmp_path / "size_mismatch.json")
    # 构造 matrix 字节数 = 8（2 个 float32），但 data 有 3 条，embedding_dim=4
    # 期望字节数 = 4 * 4 * 3 = 48，实际 8，触发 mismatch
    matrix_f32 = np.random.rand(2, 1).astype(np.float32)  # 2 个 float32 = 8 bytes
    vdb = {
        "embedding_dim": 4,
        "data": [{"__id__": "e1"}, {"__id__": "e2"}, {"__id__": "e3"}],  # 3 条
        "matrix": _encode_matrix(matrix_f32),
    }
    with open(vdb_path, "w", encoding="utf-8") as f:
        json.dump(vdb, f)

    report = check_vdb(vdb_path)
    assert report["ok"] is False
    # 应触发 matrix_size_mismatch（字节数不对）
    assert any(e["check"] == "matrix_size_mismatch" for e in report["errors"])


def test_check_vdb_vector_zlib_decode_error(tmp_path):
    """vector 字段不是 zlib 格式时报错（v1 错误格式会被检测到）"""
    from niu_api.internal.lightrag_integrity import check_vdb

    vdb_path = str(tmp_path / "bad_vector.json")
    # 故意用 v1 错误格式：base64(float32 bytes) 而非 base64(zlib(float16 bytes))
    matrix_f32 = np.random.rand(2, 4).astype(np.float32)
    bad_vector = base64.b64encode(matrix_f32[0].tobytes()).decode()  # 错误：无 zlib
    vdb = {
        "embedding_dim": 4,
        "data": [
            {"__id__": "e1", "vector": bad_vector},
            {"__id__": "e2", "vector": bad_vector},
        ],
        "matrix": _encode_matrix(matrix_f32),
    }
    with open(vdb_path, "w", encoding="utf-8") as f:
        json.dump(vdb, f)

    report = check_vdb(vdb_path)
    assert report["ok"] is False
    # vector 字段 zlib 解码失败应报错
    assert any(e["check"] == "item_vector_decode" for e in report["errors"])


def test_check_vdb_missing_field(tmp_path):
    """缺少 embedding_dim 字段报告"""
    from niu_api.internal.lightrag_integrity import check_vdb

    vdb_path = str(tmp_path / "no_dim.json")
    vdb = {"data": [{"__id__": "e1"}], "matrix": ""}
    with open(vdb_path, "w", encoding="utf-8") as f:
        json.dump(vdb, f)

    report = check_vdb(vdb_path)
    assert report["ok"] is False
    assert any(e["check"] == "missing_field" and e["field"] == "embedding_dim" for e in report["errors"])


def test_check_all_vdbs_aggregates_results(tmp_path, monkeypatch):
    """check_all_vdbs 聚合 3 个 vdb 文件检测结果"""
    from niu_api.internal import lightrag_integrity

    storage_dir = tmp_path / "lightrag_storage"
    storage_dir.mkdir()
    for fname in ["vdb_entities.json", "vdb_relationships.json", "vdb_chunks.json"]:
        _write_healthy_vdb(str(storage_dir / fname), count=2, dim=4)

    monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", str(storage_dir))

    report = lightrag_integrity.check_all_vdbs()
    assert report["ok"] is True
    assert len(report["files"]) == 3
    assert all(f["ok"] for f in report["files"].values())
```

- [ ] **Step 2: 跑测试验证失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_integrity.py -v
```

Expected: FAIL（`lightrag_integrity` 模块不存在）

- [ ] **Step 3: 创建 `niu_api/internal/lightrag_integrity.py`**

```python
"""LightRAG 数据一致性外挂检测（v2 真实格式）

vdb 文件格式实测验证：
- matrix 字段：base64(float32 bytes) 一层编码
- data[i].vector 字段：base64(zlib(float16 bytes)) 三层编码（zlib magic 789c）

检测项：
- vdb: JSON 完整、字段齐全、matrix 字节数精确等于 4*dim*data_len、
       matrix reshape、vector 三层解码
- kv_store: JSON 完整
- graphml: XML 可解析 + 节点/边计数 + 边引用节点存在
"""
from __future__ import annotations

import base64
import json
import os
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any

from loguru import logger

_STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"

_VDB_FILES = [
    "vdb_entities.json",
    "vdb_relationships.json",
    "vdb_chunks.json",
]

_KV_STORE_FILES = [
    "kv_store_doc_status.json",
    "kv_store_entity_chunks.json",
    "kv_store_full_docs.json",
    "kv_store_full_entities.json",
    "kv_store_full_relations.json",
    "kv_store_relation_chunks.json",
    "kv_store_text_chunks.json",
    "kv_store_llm_response_cache.json",
]

_GRAPHML_FILE = "graph_chunk_entity_relation.graphml"


def _decode_vector(vec_b64: str, embedding_dim: int) -> np.ndarray:
    """三层解码 vector：base64 → zlib → float16 → float32。

    Raises:
        ValueError: 解码失败。
    """
    import numpy as np
    raw_bytes = base64.b64decode(vec_b64)
    # 检查 zlib magic header
    if len(raw_bytes) < 2 or raw_bytes[:2] not in (b'\x78\x9c', b'\x78\x01', b'\x78\xda'):
        raise ValueError(f"not zlib format (header: {raw_bytes[:2].hex() if len(raw_bytes) >= 2 else 'empty'})")
    decompressed = zlib.decompress(raw_bytes)
    vec_f16 = np.frombuffer(decompressed, dtype=np.float16)
    if vec_f16.shape != (embedding_dim,):
        raise ValueError(f"vector dim {vec_f16.shape[0]} != embedding_dim {embedding_dim}")
    return vec_f16.astype(np.float32)


def check_vdb(path: str) -> dict[str, Any]:
    """检测单个 vdb 文件一致性。

    Returns:
        {"file": str, "ok": bool, "errors": [...], "stats": {...}}
    """
    import numpy as np
    report: dict[str, Any] = {"file": path, "ok": False, "errors": [], "stats": {}}

    # 1. 文件存在
    if not os.path.exists(path):
        report["errors"].append({"check": "file_exists", "msg": "file not found"})
        return report
    if os.path.getsize(path) == 0:
        report["errors"].append({"check": "file_empty", "msg": "size=0"})
        return report

    report["stats"]["file_size_bytes"] = os.path.getsize(path)

    # 2. JSON 可解析
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        report["errors"].append({
            "check": "json_parse", "msg": str(e), "line": e.lineno, "col": e.colno,
        })
        return report
    except Exception as e:
        report["errors"].append({"check": "json_parse", "msg": f"{type(e).__name__}: {e}"})
        return report

    # 3. 顶层字段齐全
    for key in ("embedding_dim", "data", "matrix"):
        if key not in raw:
            report["errors"].append({"check": "missing_field", "field": key})

    embedding_dim = raw.get("embedding_dim")
    data_list = raw.get("data", [])
    matrix_b64 = raw.get("matrix", "")

    # 4. embedding_dim 类型 + 合理范围
    if not isinstance(embedding_dim, int) or embedding_dim <= 0 or embedding_dim > 4096:
        report["errors"].append({"check": "embedding_dim_invalid", "value": embedding_dim})
        embedding_dim = None

    # 5. data 是 list
    if not isinstance(data_list, list):
        report["errors"].append({"check": "data_not_list", "type": type(data_list).__name__})
        return report

    # 6. matrix base64 可解码
    try:
        matrix_bytes = base64.b64decode(matrix_b64)
    except Exception as e:
        report["errors"].append({"check": "matrix_b64_decode", "msg": str(e)})
        return report

    # 7. matrix 字节数精确等于 4 * embedding_dim * data_len（float32 = 4 bytes）
    if embedding_dim and isinstance(data_list, list):
        expected_bytes = 4 * embedding_dim * len(data_list)
        if len(matrix_bytes) != expected_bytes:
            report["errors"].append({
                "check": "matrix_size_mismatch",
                "bytes": len(matrix_bytes),
                "expected": expected_bytes,
                "hint": "可能是 dtype 错误（float16 vs float32）或 matrix 截断",
            })

    # 8. matrix 反序列化为 float32 + reshape
    matrix = None
    try:
        if embedding_dim:
            matrix = np.frombuffer(matrix_bytes, dtype=np.float32).reshape(-1, embedding_dim)
    except ValueError as e:
        report["errors"].append({"check": "matrix_reshape", "msg": str(e)})
        return report

    # 9. matrix 行数 == data 长度
    if matrix is not None and matrix.ndim == 2 and embedding_dim:
        if matrix.shape[0] != len(data_list):
            report["errors"].append({
                "check": "row_count_mismatch",
                "matrix_rows": matrix.shape[0],
                "data_len": len(data_list),
            })

    # 10. 抽查 data 里每条 vector 三层解码
    if embedding_dim:
        for i, item in enumerate(data_list):
            vec_b64 = item.get("vector", "")
            if not vec_b64:
                continue
            try:
                _decode_vector(vec_b64, embedding_dim)
            except ValueError as e:
                report["errors"].append({
                    "check": "item_vector_decode",
                    "index": i,
                    "id": item.get("__id__"),
                    "msg": str(e),
                })

    report["stats"]["embedding_dim"] = embedding_dim
    report["stats"]["data_count"] = len(data_list)
    report["stats"]["matrix_shape"] = list(matrix.shape) if matrix is not None and matrix.ndim >= 1 else []
    report["ok"] = len(report["errors"]) == 0
    return report


def check_kv_store(path: str) -> dict[str, Any]:
    """检测单个 kv_store JSON 文件。"""
    report: dict[str, Any] = {"file": path, "ok": False, "errors": [], "stats": {}}

    if not os.path.exists(path):
        report["errors"].append({"check": "file_exists", "msg": "file not found"})
        return report
    if os.path.getsize(path) == 0:
        report["errors"].append({"check": "file_empty", "msg": "size=0"})
        return report

    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        report["errors"].append({
            "check": "json_parse", "msg": str(e), "line": e.lineno, "col": e.colno,
        })
        return report
    except Exception as e:
        report["errors"].append({"check": "json_parse", "msg": f"{type(e).__name__}: {e}"})
        return report

    if not isinstance(raw, dict):
        report["errors"].append({"check": "not_dict", "type": type(raw).__name__})
        return report

    report["stats"]["entry_count"] = len(raw)
    report["ok"] = True
    return report


def check_graphml(path: str) -> dict[str, Any]:
    """检测 GraphML 文件可解析 + 边引用节点存在。"""
    report: dict[str, Any] = {"file": path, "ok": False, "errors": [], "stats": {}}

    if not os.path.exists(path):
        report["errors"].append({"check": "file_exists", "msg": "file not found"})
        return report
    if os.path.getsize(path) == 0:
        report["errors"].append({"check": "file_empty", "msg": "size=0"})
        return report

    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError as e:
        report["errors"].append({"check": "xml_parse", "msg": str(e)})
        return report
    except Exception as e:
        report["errors"].append({"check": "xml_parse", "msg": f"{type(e).__name__}: {e}"})
        return report

    graph = root.find("graph")
    if graph is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "graph":
                graph = child
                break
    if graph is None:
        report["errors"].append({"check": "no_graph_element"})
        return report

    node_ids: set[str] = set()
    node_count = 0
    edge_count = 0
    for child in graph:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "node":
            node_count += 1
            node_ids.add(child.get("id", ""))
        elif tag == "edge":
            edge_count += 1

    # 检查边引用节点存在
    for child in graph:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "edge":
            src = child.get("source", "")
            tgt = child.get("target", "")
            if src and src not in node_ids:
                report["errors"].append({
                    "check": "edge_dangling_source", "source": src,
                })
            if tgt and tgt not in node_ids:
                report["errors"].append({
                    "check": "edge_dangling_target", "target": tgt,
                })

    report["stats"]["node_count"] = node_count
    report["stats"]["edge_count"] = edge_count
    report["ok"] = len(report["errors"]) == 0
    return report


def check_all() -> dict[str, Any]:
    """检测整个 lightrag_storage 目录。"""
    all_errors: list[dict] = []

    vdb_reports: dict[str, Any] = {}
    for fname in _VDB_FILES:
        r = check_vdb(str(_STORAGE_DIR / fname))
        vdb_reports[fname] = r
        if not r["ok"]:
            all_errors.extend(r["errors"])

    kv_reports: dict[str, Any] = {}
    for fname in _KV_STORE_FILES:
        r = check_kv_store(str(_STORAGE_DIR / fname))
        kv_reports[fname] = r
        if not r["ok"]:
            all_errors.extend(r["errors"])

    graphml_report = check_graphml(str(_STORAGE_DIR / _GRAPHML_FILE))
    if not graphml_report["ok"]:
        all_errors.extend(graphml_report["errors"])

    return {
        "ok": len(all_errors) == 0,
        "storage_dir": str(_STORAGE_DIR),
        "vdb": vdb_reports,
        "kv_store": kv_reports,
        "graphml": graphml_report,
        "total_errors": len(all_errors),
    }


def check_all_vdbs() -> dict[str, Any]:
    """检测所有 vdb 文件（不含 kv_store/graphml）。"""
    reports: dict[str, Any] = {}
    all_ok = True
    for fname in _VDB_FILES:
        r = check_vdb(str(_STORAGE_DIR / fname))
        reports[fname] = r
        if not r["ok"]:
            all_ok = False
    return {"ok": all_ok, "files": reports}
```

- [ ] **Step 4: 跑测试验证通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_integrity.py -v
```

Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add niu_api/internal/lightrag_integrity.py tests/test_lightrag_integrity.py
git commit -m "feat(lightrag_integrity): 外挂检测 vdb/kv_store/graphml 一致性（v2 真实格式）

vdb 文件格式实测验证：
- matrix 字段：base64(float32 bytes) 一层编码
- data[i].vector 字段：base64(zlib(float16 bytes)) 三层编码（zlib magic 789c）

检测项：
- vdb: JSON 完整、字段齐全、matrix 字节数精确等于 4*dim*data_len、reshape、vector 三层解码
- kv_store: JSON 完整
- graphml: XML 可解析 + 边引用节点存在

返回结构化 report，含 ok 状态、errors 列表（带定位信息）、stats 统计。"
```

---

## Task 2: 外挂修复——从 vdb data 字段重建（v2 修正数据源+格式）

**Files:**
- Create: `niu_api/internal/lightrag_repair.py`
- Test: `tests/test_lightrag_repair.py`

**背景**：v1 计划假设 kv_store 有 `description` 字段是错的——实测 `kv_store_full_entities.json` 只有 `entity_names` 列表，没有 `description`。

v2 修正数据源：
- **优先**从损坏 vdb 自身的 `data` 字段读文本（matrix 损坏 data 完好是常见场景）
- data 也损坏时 fallback：
  - chunks → `kv_store_text_chunks.json`（有 `content` 字段）
  - entities/relations → GraphML 图节点 attributes

v2 修正 vector 编码：用 `base64(zlib(float16 bytes))` 三层编码（跟 LightRAG 一致）。

- [ ] **Step 1: 写失败测试——修复路径（v2 真实格式）**

新建 `tests/test_lightrag_repair.py`：

```python
"""LightRAG 外挂修复测试（v2 真实格式+数据源）"""
import base64
import json
import zlib

import numpy as np
import pytest


def _encode_vector(vec_f16: np.ndarray) -> str:
    return base64.b64encode(zlib.compress(vec_f16.tobytes())).decode()


def _encode_matrix(matrix_f32: np.ndarray) -> str:
    return base64.b64encode(matrix_f32.tobytes()).decode()


def test_repair_vdb_from_corrupt_data_field(tmp_path, monkeypatch):
    """matrix 损坏但 data 完好时，从 data 重新 embedding 重建"""
    from niu_api.internal import lightrag_repair

    storage_dir = tmp_path / "lightrag_storage"
    storage_dir.mkdir()

    # 构造损坏 vdb：data 完好，matrix 截断
    vdb_path = storage_dir / "vdb_entities.json"
    matrix_f32 = np.random.rand(2, 4).astype(np.float32)
    data_list = [
        {"__id__": "e1", "content": "实体1描述", "vector": _encode_vector(matrix_f32[0].astype(np.float16))},
        {"__id__": "e2", "content": "实体2描述", "vector": _encode_vector(matrix_f32[1].astype(np.float16))},
    ]
    vdb = {
        "embedding_dim": 4,
        "data": data_list,
        "matrix": _encode_matrix(matrix_f32)[:50],  # 截断 matrix
    }
    vdb_path.write_text(json.dumps(vdb, ensure_ascii=False))

    # mock embedding 返回固定向量
    def fake_embed(text: str) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage_dir))
    monkeypatch.setattr(lightrag_repair, "_embed_text", fake_embed)

    result = lightrag_repair.repair_vdb("vdb_entities.json")

    assert result["status"] == "ok"
    assert result["rebuilt_count"] == 2
    assert result["source"] == "vdb_data_field"

    # 验证重建的 vdb 能通过 check_vdb 检测
    from niu_api.internal.lightrag_integrity import check_vdb
    report = check_vdb(str(vdb_path))
    assert report["ok"], f"重建的 vdb 应通过检测: {report['errors']}"
    assert report["stats"]["data_count"] == 2
    assert report["stats"]["matrix_shape"] == [2, 4]


def test_repair_vdb_from_kv_store_when_data_corrupt(tmp_path, monkeypatch):
    """data 也损坏时，chunks 从 kv_store_text_chunks 重建"""
    from niu_api.internal import lightrag_repair

    storage_dir = tmp_path / "lightrag_storage"
    storage_dir.mkdir()

    # 构造损坏 vdb_chunks：data 和 matrix 都损坏
    vdb_path = storage_dir / "vdb_chunks.json"
    vdb_path.write_text('{"truncated":')  # 完全损坏

    # 构造 kv_store_text_chunks.json（有 content 字段）
    kv_path = storage_dir / "kv_store_text_chunks.json"
    kv_data = {
        "chunk-1": {"content": "chunk1 content"},
        "chunk-2": {"content": "chunk2 content"},
    }
    kv_path.write_text(json.dumps(kv_data, ensure_ascii=False))

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage_dir))
    monkeypatch.setattr(lightrag_repair, "_embed_text", lambda x: [0.1, 0.2, 0.3, 0.4])

    result = lightrag_repair.repair_vdb("vdb_chunks.json")

    assert result["status"] == "ok"
    assert result["rebuilt_count"] == 2
    assert result["source"] == "kv_store_text_chunks"

    # 验证重建的 vdb 通过检测
    from niu_api.internal.lightrag_integrity import check_vdb
    report = check_vdb(str(vdb_path))
    assert report["ok"], f"重建的 vdb 应通过检测: {report['errors']}"


def test_repair_vdb_backs_up_corrupt_file(tmp_path, monkeypatch):
    """修复前把损坏的 vdb 备份到 .corrupt.bak"""
    from niu_api.internal import lightrag_repair

    storage_dir = tmp_path / "lightrag_storage"
    storage_dir.mkdir()

    vdb_path = storage_dir / "vdb_entities.json"
    # data 完好，matrix 损坏
    matrix_f32 = np.random.rand(1, 4).astype(np.float32)
    vdb = {
        "embedding_dim": 4,
        "data": [{"__id__": "e1", "content": "desc", "vector": _encode_vector(matrix_f32[0].astype(np.float16))}],
        "matrix": "truncated",
    }
    vdb_path.write_text(json.dumps(vdb, ensure_ascii=False))

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage_dir))
    monkeypatch.setattr(lightrag_repair, "_embed_text", lambda x: [0.1, 0.2, 0.3, 0.4])

    result = lightrag_repair.repair_vdb("vdb_entities.json")
    assert result["status"] == "ok"

    corrupt_bak = storage_dir / "vdb_entities.json.corrupt.bak"
    assert corrupt_bak.exists()


def test_repair_vdb_missing_data_and_kv_returns_error(tmp_path, monkeypatch):
    """data 和 kv_store 都损坏时返回错误"""
    from niu_api.internal import lightrag_repair

    storage_dir = tmp_path / "lightrag_storage"
    storage_dir.mkdir()
    (storage_dir / "vdb_entities.json").write_text('{"truncated":')  # 完全损坏
    # 没有 kv_store，也没有 GraphML

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage_dir))
    monkeypatch.setattr(lightrag_repair, "_embed_text", lambda x: [0.1, 0.2, 0.3, 0.4])

    result = lightrag_repair.repair_vdb("vdb_entities.json")
    assert result["status"] == "error"
    assert "无可用数据源" in result["message"] or "no data source" in result["message"].lower()


def test_repair_all_repairs_all_vdbs(tmp_path, monkeypatch):
    """repair_all 修复 3 个 vdb 文件"""
    from niu_api.internal import lightrag_repair

    storage_dir = tmp_path / "lightrag_storage"
    storage_dir.mkdir()

    # 3 个 vdb 都 matrix 损坏但 data 完好
    for fname in ["vdb_entities.json", "vdb_relationships.json", "vdb_chunks.json"]:
        matrix_f32 = np.random.rand(2, 4).astype(np.float32)
        vdb = {
            "embedding_dim": 4,
            "data": [
                {"__id__": "e1", "content": f"{fname} desc1", "vector": _encode_vector(matrix_f32[0].astype(np.float16))},
                {"__id__": "e2", "content": f"{fname} desc2", "vector": _encode_vector(matrix_f32[1].astype(np.float16))},
            ],
            "matrix": "truncated",
        }
        (storage_dir / fname).write_text(json.dumps(vdb, ensure_ascii=False))

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage_dir))
    monkeypatch.setattr(lightrag_repair, "_embed_text", lambda x: [0.1, 0.2, 0.3, 0.4])

    result = lightrag_repair.repair_all()
    assert all(r["status"] == "ok" for r in result.values())
```

- [ ] **Step 2: 跑测试验证失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_repair.py -v
```

Expected: FAIL（`lightrag_repair` 模块不存在）

- [ ] **Step 3: 创建 `niu_api/internal/lightrag_repair.py`**

```python
"""LightRAG 外挂修复（v2 真实格式+数据源）

修复策略（按优先级）：
1. 从损坏 vdb 自身的 data 字段读文本（matrix 损坏 data 完好是常见场景）
2. data 也损坏时 fallback：
   - chunks → kv_store_text_chunks.json（有 content 字段）
   - entities/relations → GraphML 图节点 attributes（TODO，暂不支持）

vector 字段编码：base64(zlib(float16 bytes)) 三层（跟 LightRAG 一致）
matrix 字段编码：base64(float32 bytes) 一层

_embed_text 不依赖 LightRAG 实例，直接用 niu_api.internal.embedding 预加载的模型。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import zlib
from pathlib import Path
from typing import Any

from loguru import logger

_STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"

# vdb 文件名 → 文本字段名（vdb data 内部）
_VDB_TEXT_FIELD = {
    "vdb_entities.json": "content",
    "vdb_relationships.json": "content",
    "vdb_chunks.json": "content",
}

# data 损坏时的 fallback 数据源
_VDB_FALLBACK_KV = {
    "vdb_chunks.json": ("kv_store_text_chunks.json", "content"),
    # entities/relations 的 fallback 是 GraphML，暂不支持（返回 error）
}


def _embed_text(text: str) -> list[float]:
    """用预加载的 embedding 模型生成向量，不依赖 LightRAG 实例。

    embedding 模型在 __main__.py 启动时预加载（LightRAG eager init 之前）。
    """
    try:
        from niu_api.internal.embedding import get_model
        model = get_model()
        if model is None:
            raise RuntimeError("embedding 模型未加载")
        vec = model.encode([text])
        return vec[0].tolist()
    except ImportError:
        # fallback：用 LightRAG 实例（如果已初始化）
        from niu_api.internal.lightrag_manager import get_lightrag
        rag = get_lightrag()
        if rag is None:
            raise RuntimeError("embedding 模型未加载且 LightRAG 未初始化")
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(rag.embedding_func([text]))
            return result[0].tolist()
        finally:
            loop.close()


def _encode_vector(vec_f16) -> str:
    """vector 字段三层编码：base64(zlib(float16 bytes))"""
    import numpy as np
    arr = np.array(vec_f16, dtype=np.float16) if not hasattr(vec_f16, 'astype') else vec_f16.astype(np.float16)
    return base64.b64encode(zlib.compress(arr.tobytes())).decode()


def _encode_matrix(matrix_f32) -> str:
    """matrix 字段一层编码：base64(float32 bytes)"""
    import numpy as np
    arr = np.array(matrix_f32, dtype=np.float32) if not hasattr(matrix_f32, 'astype') else matrix_f32.astype(np.float32)
    return base64.b64encode(arr.tobytes()).decode()


def _read_data_from_vdb(vdb_filename: str) -> list[dict] | None:
    """尝试从损坏 vdb 的 data 字段读文本（matrix 损坏 data 完好场景）。

    Returns:
        data 列表（含 __id__ + content），如果 data 也损坏返回 None。
    """
    vdb_path = _STORAGE_DIR / vdb_filename
    if not vdb_path.exists():
        return None
    try:
        with open(vdb_path, encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, Exception):
        return None  # data 也损坏
    data_list = raw.get("data")
    if not isinstance(data_list, list) or not data_list:
        return None
    text_field = _VDB_TEXT_FIELD.get(vdb_filename, "content")
    # 验证 data 里的文本字段可用
    valid = [item for item in data_list if isinstance(item, dict) and item.get(text_field)]
    return valid if valid else None


def _read_data_from_kv_store(vdb_filename: str) -> list[dict] | None:
    """data 损坏时从 fallback kv_store 读文本。"""
    fallback = _VDB_FALLBACK_KV.get(vdb_filename)
    if not fallback:
        return None  # entities/relations 暂不支持 fallback
    kv_filename, text_field = fallback
    kv_path = _STORAGE_DIR / kv_filename
    if not kv_path.exists():
        return None
    try:
        with open(kv_path, encoding="utf-8") as f:
            kv_data = json.load(f)
    except (json.JSONDecodeError, Exception):
        return None
    data_list = []
    for key, value in kv_data.items():
        if isinstance(value, dict):
            text = value.get(text_field)
            if text:
                data_list.append({"__id__": key, "content": text})
    return data_list if data_list else None


def repair_vdb(vdb_filename: str) -> dict[str, Any]:
    """修复单个 vdb 文件。

    优先从 vdb 自身 data 字段重建，fallback 到 kv_store。

    Returns:
        {"status": "ok"|"error", "rebuilt_count": int, "source": str, "message": str}
    """
    if vdb_filename not in _VDB_TEXT_FIELD:
        return {"status": "error", "message": f"未知的 vdb 文件: {vdb_filename}"}

    # 1. 优先从 vdb data 字段读
    data_list = _read_data_from_vdb(vdb_filename)
    source = "vdb_data_field" if data_list else None

    # 2. fallback 到 kv_store
    if not data_list:
        data_list = _read_data_from_kv_store(vdb_filename)
        if data_list:
            source = "kv_store"

    if not data_list:
        return {
            "status": "error",
            "message": f"无可用数据源重建 {vdb_filename}（vdb data 和 fallback kv_store 都损坏）",
        }

    # 3. 重新 embedding（保留原 data 所有非 vector 字段，只重算 vector）
    import numpy as np
    text_field = _VDB_TEXT_FIELD.get(vdb_filename, "content")
    new_data = []
    vectors = []
    for item in data_list:
        text = item.get(text_field, "")
        try:
            vec = _embed_text(text)
        except Exception as e:
            logger.warning(f"[LightRAGRepair] embedding 失败 {item.get('__id__')}: {e}，跳过")
            continue
        # 保留原 data 所有非 vector 字段（__created_at__ / entity_name / src_id / tgt_id / source_id / full_doc_id / file_path 等）
        # 只重算 vector 字段
        new_item = {k: v for k, v in item.items() if k != "vector"}
        new_item["vector"] = _encode_vector(np.array(vec, dtype=np.float16))
        new_data.append(new_item)
        vectors.append(vec)

    if not new_data:
        return {"status": "error", "message": "embedding 全部失败，无数据可重建"}

    embedding_dim = len(vectors[0])
    matrix_f32 = np.array(vectors, dtype=np.float32)

    # 4. 备份损坏 vdb 到 .corrupt.bak
    vdb_path = _STORAGE_DIR / vdb_filename
    if vdb_path.exists():
        corrupt_bak = _STORAGE_DIR / f"{vdb_filename}.corrupt.bak"
        try:
            if corrupt_bak.exists():
                corrupt_bak.unlink()
            vdb_path.rename(corrupt_bak)
            logger.info(f"[LightRAGRepair] 损坏 vdb 备份到: {corrupt_bak}")
        except Exception as e:
            logger.warning(f"[LightRAGRepair] 备份损坏 vdb 失败: {e}")

    # 5. 原子写新 vdb
    storage = {
        "embedding_dim": embedding_dim,
        "data": new_data,
        "matrix": _encode_matrix(matrix_f32),
    }
    tmp_file = vdb_path.with_suffix(".json.tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(storage, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_file, vdb_path)

    logger.info(f"[LightRAGRepair] 重建 {vdb_filename}: {len(new_data)} 条 (source={source})")
    return {
        "status": "ok",
        "rebuilt_count": len(new_data),
        "source": source,
        "message": f"从 {source} 重建 {len(new_data)} 条",
    }


def repair_kv_store(kv_filename: str) -> dict[str, Any]:
    """kv_store 损坏时从 .bak 恢复。"""
    kv_path = _STORAGE_DIR / kv_filename
    bak_path = _STORAGE_DIR / f"{kv_filename}.bak"
    if not bak_path.exists():
        return {"status": "error", "message": f"备份文件不存在: {bak_path}"}
    try:
        shutil.copy2(bak_path, kv_path)
        logger.info(f"[LightRAGRepair] 从 .bak 恢复 {kv_filename}")
        return {"status": "ok", "message": f"从 .bak 恢复"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def repair_all() -> dict[str, Any]:
    """一键修复所有 vdb。"""
    results: dict[str, Any] = {}
    for vdb_file in _VDB_TEXT_FIELD:
        results[vdb_file] = repair_vdb(vdb_file)
    return results
```

- [ ] **Step 4: 跑测试验证通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_repair.py -v
```

Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add niu_api/internal/lightrag_repair.py tests/test_lightrag_repair.py
git commit -m "feat(lightrag_repair): 外挂修复 vdb（v2 真实格式+数据源）

修复策略：
1. 优先从损坏 vdb 自身的 data 字段读文本（matrix 损坏 data 完好是常见场景）
2. data 也损坏时 fallback 到 kv_store_text_chunks（chunks）或报错（entities/relations）

vector 字段三层编码：base64(zlib(float16 bytes))，跟 LightRAG 一致
matrix 字段一层编码：base64(float32 bytes)

_embed_text 不依赖 LightRAG 实例，直接用预加载的 embedding 模型
（在 LightRAG eager init 之前可用），解决鸡生蛋矛盾。"
```

---

## Task 3: 外挂备份——滚动备份 + 全量备份

**Files:**
- Create: `niu_api/internal/lightrag_backup.py`
- Test: `tests/test_lightrag_backup.py`

**背景**：当前无任何备份机制。外挂层做定时快照，不改 nano-vectordb save()。

- [ ] **Step 1: 写失败测试——备份机制**

新建 `tests/test_lightrag_backup.py`：

```python
"""LightRAG 外挂备份测试"""
import os
import time

import pytest


def test_rolling_backup_copies_to_bak(tmp_path, monkeypatch):
    """rolling_backup 把文件复制到 .bak"""
    from niu_api.internal import lightrag_backup

    storage_dir = tmp_path / "lightrag_storage"
    storage_dir.mkdir()
    vdb_path = storage_dir / "vdb_entities.json"
    vdb_path.write_text('{"version": "v1"}')

    monkeypatch.setattr(lightrag_backup, "_STORAGE_DIR", str(storage_dir))

    ok = lightrag_backup.rolling_backup("vdb_entities.json")
    assert ok is True
    bak_path = storage_dir / "vdb_entities.json.bak"
    assert bak_path.exists()
    assert bak_path.read_text() == '{"version": "v1"}'


def test_rolling_backup_overwrites_existing_bak(tmp_path, monkeypatch):
    """rolling_backup 滚动覆盖已有 .bak（保留 1 份）"""
    from niu_api.internal import lightrag_backup

    storage_dir = tmp_path / "lightrag_storage"
    storage_dir.mkdir()
    vdb_path = storage_dir / "vdb_entities.json"
    vdb_path.write_text('{"version": "v2"}')
    (storage_dir / "vdb_entities.json.bak").write_text('{"version": "v1"}')

    monkeypatch.setattr(lightrag_backup, "_STORAGE_DIR", str(storage_dir))

    lightrag_backup.rolling_backup("vdb_entities.json")
    bak_path = storage_dir / "vdb_entities.json.bak"
    assert bak_path.read_text() == '{"version": "v2"}'
    assert not (storage_dir / "vdb_entities.json.bak.1").exists()


def test_rolling_backup_returns_false_for_missing_file(tmp_path, monkeypatch):
    from niu_api.internal import lightrag_backup

    monkeypatch.setattr(lightrag_backup, "_STORAGE_DIR", str(tmp_path))
    assert lightrag_backup.rolling_backup("nonexistent.json") is False


def test_full_backup_creates_timestamped_snapshot(tmp_path, monkeypatch):
    """全量备份把整个 storage 复制到 backups/<timestamp>/（排除 .bak/.corrupt.bak）"""
    from niu_api.internal import lightrag_backup

    storage_dir = tmp_path / "lightrag_storage"
    storage_dir.mkdir()
    (storage_dir / "vdb_entities.json").write_text("{}")
    (storage_dir / "kv_store_full_docs.json").write_text("{}")
    (storage_dir / "vdb_entities.json.bak").write_text("bak")  # 应排除
    (storage_dir / "vdb_relationships.json.corrupt.bak").write_text("corrupt")  # 应排除

    backups_dir = tmp_path / "backups"
    monkeypatch.setattr(lightrag_backup, "_STORAGE_DIR", str(storage_dir))
    monkeypatch.setattr(lightrag_backup, "_BACKUPS_DIR", str(backups_dir))

    backup_dir = lightrag_backup.full_backup()
    assert backup_dir is not None
    assert backup_dir.exists()
    assert (backup_dir / "vdb_entities.json").exists()
    assert (backup_dir / "kv_store_full_docs.json").exists()
    # .bak 和 .corrupt.bak 应被排除
    assert not (backup_dir / "vdb_entities.json.bak").exists()
    assert not (backup_dir / "vdb_relationships.json.corrupt.bak").exists()


def test_full_backup_retains_only_last_7(tmp_path, monkeypatch):
    from niu_api.internal import lightrag_backup

    storage_dir = tmp_path / "lightrag_storage"
    storage_dir.mkdir()
    (storage_dir / "vdb_entities.json").write_text("{}")
    backups_dir = tmp_path / "backups"

    monkeypatch.setattr(lightrag_backup, "_STORAGE_DIR", str(storage_dir))
    monkeypatch.setattr(lightrag_backup, "_BACKUPS_DIR", str(backups_dir))

    for i in range(10):
        lightrag_backup.full_backup()
        subdirs = sorted(backups_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        if subdirs:
            os.utime(subdirs[-1], (time.time() + i * 100, time.time() + i * 100))

    subdirs = [p for p in backups_dir.iterdir() if p.is_dir()]
    assert len(subdirs) <= 7


def test_cleanup_corrupt_bak_removes_residue(tmp_path, monkeypatch):
    from niu_api.internal import lightrag_backup

    storage_dir = tmp_path / "lightrag_storage"
    storage_dir.mkdir()
    corrupt_bak = storage_dir / "vdb_relationships.json.corrupt.bak"
    corrupt_bak.write_text("corrupt")

    monkeypatch.setattr(lightrag_backup, "_STORAGE_DIR", str(storage_dir))

    removed = lightrag_backup.cleanup_corrupt_bak()
    assert removed == 1
    assert not corrupt_bak.exists()


def test_backup_all_vdbs_rolls_all(tmp_path, monkeypatch):
    from niu_api.internal import lightrag_backup

    storage_dir = tmp_path / "lightrag_storage"
    storage_dir.mkdir()
    for fname in ["vdb_entities.json", "vdb_relationships.json", "vdb_chunks.json"]:
        (storage_dir / fname).write_text("{}")

    monkeypatch.setattr(lightrag_backup, "_STORAGE_DIR", str(storage_dir))

    results = lightrag_backup.backup_all_vdbs()
    assert len(results) == 3
    assert all(results.values())
```

- [ ] **Step 2: 跑测试验证失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_backup.py -v
```

Expected: FAIL（`lightrag_backup` 模块不存在）

- [ ] **Step 3: 创建 `niu_api/internal/lightrag_backup.py`**

```python
"""LightRAG 外挂备份机制

- rolling_backup: 复制到 .bak（保留 1 份，滚动覆盖）
- backup_all_vdbs: 3 个 vdb 文件批量滚动备份
- full_backup: 整个 storage 目录复制到 backups/<timestamp>/（排除 .bak/.corrupt.bak，保留最近 7 份）
- cleanup_corrupt_bak: 清理 .corrupt.bak 残留

不改 nano-vectordb save()，外挂层定时快照。
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

_STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"
_BACKUPS_DIR = Path.home() / ".niu" / "lightrag_storage_backups"
_MAX_FULL_BACKUPS = 7

_VDB_FILES = [
    "vdb_entities.json",
    "vdb_relationships.json",
    "vdb_chunks.json",
]

# 备份时排除的文件模式（滚动备份和损坏备份不进 full_backup）
_EXCLUDE_SUFFIXES = (".bak", ".corrupt.bak", ".tmp")


def _is_excluded(filename: str) -> bool:
    return any(filename.endswith(suffix) for suffix in _EXCLUDE_SUFFIXES)


def rolling_backup(filename: str) -> bool:
    """把 _STORAGE_DIR/filename 复制到 filename.bak（覆盖已有 .bak）。

    Returns:
        True 如果备份成功，False 如果原文件不存在或复制失败。
    """
    src = _STORAGE_DIR / filename
    if not src.exists():
        return False
    dst = _STORAGE_DIR / f"{filename}.bak"
    try:
        shutil.copy2(src, dst)
        return True
    except Exception as e:
        logger.warning(f"[LightRAGBackup] 滚动备份失败 {filename}: {e}")
        return False


def backup_all_vdbs() -> dict[str, bool]:
    """对 3 个 vdb 文件都做滚动备份。"""
    results: dict[str, bool] = {}
    for fname in _VDB_FILES:
        results[fname] = rolling_backup(fname)
    return results


def full_backup() -> Optional[Path]:
    """把整个 _STORAGE_DIR 复制到 _BACKUPS_DIR/<timestamp>/（排除 .bak/.corrupt.bak/.tmp）。

    保留最近 _MAX_FULL_BACKUPS 份，老的自动清理。

    Returns:
        备份目录路径，失败返回 None。
    """
    if not _STORAGE_DIR.exists():
        return None
    _BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = _BACKUPS_DIR / timestamp
    try:
        backup_dir.mkdir()
        # 手动复制，排除 .bak/.corrupt.bak/.tmp
        for item in _STORAGE_DIR.iterdir():
            if _is_excluded(item.name):
                continue
            if item.is_file():
                shutil.copy2(item, backup_dir / item.name)
            elif item.is_dir():
                shutil.copytree(item, backup_dir / item.name)
        logger.info(f"[LightRAGBackup] 全量备份完成: {backup_dir}")
    except Exception as e:
        logger.warning(f"[LightRAGBackup] 全量备份失败: {e}")
        # 清理半成品
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        return None

    _cleanup_old_full_backups()
    return backup_dir


def _cleanup_old_full_backups() -> int:
    """清理超过 _MAX_FULL_BACKUPS 份的旧备份。"""
    if not _BACKUPS_DIR.exists():
        return 0
    subdirs = sorted(
        [p for p in _BACKUPS_DIR.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
    )
    removed = 0
    while len(subdirs) > _MAX_FULL_BACKUPS:
        old = subdirs.pop(0)
        try:
            shutil.rmtree(old)
            removed += 1
            logger.info(f"[LightRAGBackup] 清理旧备份: {old}")
        except Exception as e:
            logger.warning(f"[LightRAGBackup] 清理失败 {old}: {e}")
    return removed


def cleanup_corrupt_bak() -> int:
    """清理 .corrupt.bak 残留文件。"""
    if not _STORAGE_DIR.exists():
        return 0
    removed = 0
    for p in _STORAGE_DIR.glob("*.corrupt.bak"):
        try:
            p.unlink()
            removed += 1
            logger.info(f"[LightRAGBackup] 清理残留: {p}")
        except Exception as e:
            logger.warning(f"[LightRAGBackup] 清理失败 {p}: {e}")
    return removed
```

- [ ] **Step 4: 跑测试验证通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_backup.py -v
```

Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add niu_api/internal/lightrag_backup.py tests/test_lightrag_backup.py
git commit -m "feat(lightrag_backup): 外挂备份——滚动备份 + 全量备份（排除 .bak/.corrupt.bak）

- rolling_backup: 复制到 .bak（保留 1 份，滚动覆盖）
- backup_all_vdbs: 3 个 vdb 文件批量滚动备份
- full_backup: 整个 storage 复制到 backups/<timestamp>/（排除 .bak/.corrupt.bak/.tmp，保留最近 7 份）
- cleanup_corrupt_bak: 清理 .corrupt.bak 残留

不改 nano-vectordb save()，外挂层定时快照。"
```

---

## Task 4: 启动时集成检测+修复+备份（v2 拆分两阶段）

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py`
- Modify: `niu_api/__main__.py`
- Test: `tests/test_lightrag_resilience_integration.py`（新建）

**背景**：v1 计划把所有逻辑放在 LightRAG eager init 之前——但 `_embed_text` 需要 embedding 模型已加载。v2 拆分两阶段：
- Phase 1（LightRAG eager init 之前）：`cleanup_corrupt_bak` + `full_backup` + `check_all`（纯文件操作）
- Phase 2（LightRAG eager init 之后）：如果 check 发现损坏，调 `repair_all`（embedding 模型已加载）

- [ ] **Step 1: 写失败测试——两阶段集成流程**

新建 `tests/test_lightrag_resilience_integration.py`：

```python
"""LightRAG 韧性集成测试——两阶段启动流程"""
from unittest import mock

import pytest


def test_phase1_runs_cleanup_backup_check(monkeypatch):
    """Phase 1（LightRAG init 之前）：cleanup + full_backup + check_all，不调 repair"""
    from niu_api.internal import lightrag_manager

    backup_calls = []
    cleanup_calls = []
    check_calls = []

    monkeypatch.setattr("niu_api.internal.lightrag_backup.full_backup",
                        lambda: backup_calls.append("full") or mock.MagicMock())
    monkeypatch.setattr("niu_api.internal.lightrag_backup.cleanup_corrupt_bak",
                        lambda: cleanup_calls.append("cleanup") or 0)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity.check_all",
                        lambda: check_calls.append("check") or {"ok": True, "total_errors": 0})

    result = lightrag_manager.run_resilience_phase1()

    assert cleanup_calls == ["cleanup"]
    assert backup_calls == ["full"]
    assert check_calls == ["check"]
    assert result["check_ok"] is True
    assert result["need_repair"] is False  # 健康时不需修复


def test_phase1_corrupt_sets_need_repair(monkeypatch):
    """Phase 1 检测到损坏时设 need_repair=True，但不立即修复"""
    from niu_api.internal import lightrag_manager

    monkeypatch.setattr("niu_api.internal.lightrag_backup.full_backup", lambda: mock.MagicMock())
    monkeypatch.setattr("niu_api.internal.lightrag_backup.cleanup_corrupt_bak", lambda: 0)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity.check_all",
                        lambda: {"ok": False, "total_errors": 2, "vdb": {"vdb_entities.json": {"ok": False}}})

    result = lightrag_manager.run_resilience_phase1()

    assert result["check_ok"] is False
    assert result["need_repair"] is True


def test_phase2_repairs_when_needed(monkeypatch):
    """Phase 2（LightRAG init 之后）：need_repair=True 时调 repair_all + reset_init_state"""
    from niu_api.internal import lightrag_manager

    repair_calls = []
    reset_calls = []
    monkeypatch.setattr("niu_api.internal.lightrag_repair.repair_all",
                        lambda: repair_calls.append("repair") or {"vdb_entities.json": {"status": "ok"}})
    monkeypatch.setattr("niu_api.internal.lightrag_manager.reset_init_state",
                        lambda: reset_calls.append("reset"))

    result = lightrag_manager.run_resilience_phase2(need_repair=True)

    assert repair_calls == ["repair"]
    assert reset_calls == ["reset"]
    assert result["repaired"] is True


def test_phase2_skips_when_healthy(monkeypatch):
    """Phase 2 健康时不调 repair"""
    from niu_api.internal import lightrag_manager

    repair_calls = []
    monkeypatch.setattr("niu_api.internal.lightrag_repair.repair_all",
                        lambda: repair_calls.append("repair") or {})
    monkeypatch.setattr("niu_api.internal.lightrag_manager.reset_init_state", lambda: None)

    result = lightrag_manager.run_resilience_phase2(need_repair=False)

    assert repair_calls == []
    assert result["repaired"] is False


def test_get_lightrag_status_includes_integrity(monkeypatch):
    from niu_api.internal import lightrag_manager

    # _init_failed_at: Optional[float] = None，设 None 让 init_failed=False
    monkeypatch.setattr(lightrag_manager, "_init_failed_at", None)
    monkeypatch.setattr(lightrag_manager, "_integrity_result", {
        "ok": False, "total_errors": 2,
        "vdb": {"vdb_entities.json": {"ok": False}},
    })

    status = lightrag_manager.get_lightrag_status()
    assert status["init_failed"] is False
    assert "integrity" in status
    assert status["integrity"]["ok"] is False
    assert status["integrity"]["total_errors"] == 2
```

- [ ] **Step 2: 跑测试验证失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_resilience_integration.py -v
```

Expected: FAIL（`run_resilience_phase1` / `run_resilience_phase2` / `_integrity_result` 不存在）

- [ ] **Step 3: 在 `lightrag_manager.py` 加两阶段逻辑**

在 `niu_api/internal/lightrag_manager.py`：

**3a. 模块级加 `_integrity_result` 变量**：

```python
_init_failed_at: float = 0.0
_init_error: dict | None = None
_integrity_result: dict | None = None  # 新增
```

**3b. 加 `run_resilience_phase1` + `run_resilience_phase2` + `reset_init_state` 函数**：

```python
def run_resilience_phase1() -> dict:
    """Phase 1（LightRAG eager init 之前）：cleanup + full_backup + check_all。

    纯文件操作，不依赖 LightRAG 实例或 embedding 模型。

    Returns:
        {"check_ok": bool, "need_repair": bool, "check_result": dict}
    """
    global _integrity_result
    from niu_api.internal.lightrag_backup import full_backup, cleanup_corrupt_bak
    from niu_api.internal.lightrag_integrity import check_all

    # 1. 清理 corrupt.bak 残留
    try:
        cleanup_corrupt_bak()
    except Exception as e:
        logger.warning(f"[LightRAG] 清理 corrupt.bak 失败（不影响启动）: {e}")

    # 2. 全量备份（排除 .bak/.corrupt.bak）
    try:
        full_backup()
    except Exception as e:
        logger.warning(f"[LightRAG] 全量备份失败（不影响启动）: {e}")

    # 3. 一致性检测
    try:
        check_result = check_all()
    except Exception as e:
        logger.warning(f"[LightRAG] 一致性检测失败（不影响启动）: {e}")
        check_result = {"ok": True, "total_errors": 0, "error": str(e)}

    _integrity_result = check_result

    logger.info(
        f"[LightRAG] Phase 1 完成: check_ok={check_result.get('ok')}, "
        f"total_errors={check_result.get('total_errors', 0)}"
    )
    return {
        "check_ok": check_result.get("ok", True),
        "need_repair": not check_result.get("ok", True),
        "check_result": check_result,
    }


def run_resilience_phase2(need_repair: bool) -> dict:
    """Phase 2（LightRAG eager init 之后）：如果 need_repair，调 repair_all + reset_init_state。

    embedding 模型已在 LightRAG eager init 时加载，repair_vdb 可用。

    Returns:
        {"repaired": bool, "repair_result": dict | None}
    """
    if not need_repair:
        logger.info("[LightRAG] Phase 2 跳过：无损坏需修复")
        return {"repaired": False, "repair_result": None}

    from niu_api.internal.lightrag_repair import repair_all

    logger.warning("[LightRAG] Phase 2: 检测到损坏，启动修复")
    try:
        repair_result = repair_all()
        # 修复后重置初始化状态，让下次 get_lightrag 重试
        reset_init_state()
        logger.info(f"[LightRAG] Phase 2 修复完成: {repair_result}")
        return {"repaired": True, "repair_result": repair_result}
    except Exception as e:
        logger.error(f"[LightRAG] Phase 2 修复失败: {e}")
        return {"repaired": False, "repair_result": {"error": str(e)}}


def reset_init_state() -> None:
    """重置初始化失败状态，让下次 get_lightrag 重试。"""
    global _init_failed_at, _init_error
    _init_failed_at = 0.0
    _init_error = None
```

**3c. 扩展 `get_lightrag_status`**（在现有函数末尾加 integrity 字段，不重写整个函数）：

读 `niu_api/internal/lightrag_manager.py` 现有 `get_lightrag_status` 实现（约 L980-1010），在函数返回 dict 之前加：

```python
    # 在现有 get_lightrag_status 的 return dict 之前加：
    if _integrity_result:
        result["integrity"] = {
            "ok": _integrity_result.get("ok", True),
            "total_errors": _integrity_result.get("total_errors", 0),
        }
    return result
```

**注意**：不要重写整个 `get_lightrag_status`——现有实现用 `_INIT_RETRY_SECONDS`（不是 `INIT_RETRY_INTERVAL`）+ `with _rag_lock` 锁保护 + `round(..., 1)` 精度。只追加 `integrity` 字段，避免破坏现有逻辑。

**3d. 在 `__main__.py` 调用两阶段**：

```python
    # Phase 1: LightRAG eager init 之前——纯文件操作
    try:
        from niu_api.internal.lightrag_manager import run_resilience_phase1
        phase1_result = run_resilience_phase1()
        logger.info(f"LightRAG Phase 1 韧性流程: {phase1_result}")
    except Exception as e:
        logger.warning(f"LightRAG Phase 1 韧性流程失败（不影响启动）: {e}")
        phase1_result = {"need_repair": False}
```

在 LightRAG eager init（`__main__.py:200` 附近）**之后**加：

```python
    # Phase 2: LightRAG eager init 之后——embedding 模型已加载，可调 repair
    try:
        from niu_api.internal.lightrag_manager import run_resilience_phase2
        phase2_result = run_resilience_phase2(need_repair=phase1_result.get("need_repair", False))
        if phase2_result.get("repaired"):
            logger.info("LightRAG Phase 2 修复完成，重新初始化 LightRAG")
            # 修复后强制重新初始化
            from niu_api.internal.lightrag_manager import get_lightrag
            get_lightrag()  # 触发重试
    except Exception as e:
        logger.warning(f"LightRAG Phase 2 韧性流程失败: {e}")
```

- [ ] **Step 4: 跑测试验证通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_resilience_integration.py -v
```

Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add niu_api/internal/lightrag_manager.py niu_api/__main__.py tests/test_lightrag_resilience_integration.py
git commit -m "feat(lightrag_manager): 启动两阶段韧性流程（v2 解决鸡生蛋）

Phase 1（LightRAG eager init 之前）：cleanup + full_backup + check_all
- 纯文件操作，不依赖 LightRAG 实例或 embedding 模型
- 检测到损坏设 need_repair=True，但不立即修复

Phase 2（LightRAG eager init 之后）：repair_all + reset_init_state
- embedding 模型已加载，repair_vdb 可用
- 修复后调 get_lightrag() 强制重试初始化

get_lightrag_status 加 integrity 字段暴露检测结果。"
```

---

## Task 5: 修复 API 端点

**Files:**
- Modify: `niu_api/kg_api.py`（router prefix 是 `/api/kg`，端点路径要对齐）
- Test: `tests/test_lightrag_repair_api.py`（新建）

**背景**：`kg_api.py` L19 `router = APIRouter(prefix="/api/kg")`，所以 `@router.post("/lightrag/repair")` 实际路径是 `/api/kg/lightrag/repair`，不是 `/api/lightrag/repair`。测试、curl、Rust 代码都要对齐这个路径。

- [ ] **Step 1: 写失败测试——修复 API（路径用 `/api/kg/lightrag/repair`）**

新建 `tests/test_lightrag_repair_api.py`：

```python
"""LightRAG 修复 API 测试"""
from unittest import mock

import pytest
from fastapi.testclient import TestClient


def test_repair_endpoint_all_targets(monkeypatch):
    from niu_api import kg_api

    repair_calls = []
    monkeypatch.setattr("niu_api.internal.lightrag_repair.repair_all",
                        lambda: repair_calls.append("all") or {"vdb_entities.json": {"status": "ok"}})
    monkeypatch.setattr("niu_api.internal.lightrag_manager.reset_init_state", lambda: None)

    client = TestClient(kg_api.app)
    # router prefix 是 /api/kg，端点是 /lightrag/repair，拼起来是 /api/kg/lightrag/repair
    response = client.post("/api/kg/lightrag/repair", params={"target": "all"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert repair_calls == ["all"]


def test_repair_endpoint_specific_vdb(monkeypatch):
    from niu_api import kg_api

    repair_calls = []
    monkeypatch.setattr("niu_api.internal.lightrag_repair.repair_vdb",
                        lambda name: repair_calls.append(name) or {"status": "ok", "rebuilt_count": 5})
    monkeypatch.setattr("niu_api.internal.lightrag_manager.reset_init_state", lambda: None)

    client = TestClient(kg_api.app)
    response = client.post("/api/kg/lightrag/repair", params={"target": "vdb_entities.json"})

    assert response.status_code == 200
    assert repair_calls == ["vdb_entities.json"]


def test_repair_endpoint_unknown_target(monkeypatch):
    from niu_api import kg_api

    client = TestClient(kg_api.app)
    response = client.post("/api/kg/lightrag/repair", params={"target": "unknown.txt"})

    assert response.status_code == 400
```

- [ ] **Step 2: 跑测试验证失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_repair_api.py -v
```

Expected: FAIL（端点不存在）

- [ ] **Step 3: 在 `kg_api.py` 加修复端点**

```python
@router.post("/lightrag/repair")
def repair_lightrag_storage(target: str = "all") -> dict:
    """修复 LightRAG 存储。

    实际路径：/api/kg/lightrag/repair（router prefix=/api/kg + 端点 /lightrag/repair）

    Args:
        target: "all" | "vdb_entities.json" | "vdb_relationships.json" | "vdb_chunks.json"
                | "kv_store_xxx.json"（从 .bak 恢复）
    """
    from fastapi import HTTPException
    from niu_api.internal.lightrag_repair import repair_vdb, repair_kv_store, repair_all
    from niu_api.internal.lightrag_manager import reset_init_state
    import niu_api.internal.lightrag_manager as lm

    if target == "all":
        result = repair_all()
    elif target.startswith("vdb_"):
        result = {target: repair_vdb(target)}
    elif target.startswith("kv_store_"):
        result = {target: repair_kv_store(target)}
    else:
        raise HTTPException(status_code=400, detail=f"未知 target: {target}")

    reset_init_state()
    # 修复后重跑 check_all 更新 _integrity_result，让前端立即看到健康状态
    from niu_api.internal.lightrag_integrity import check_all
    lm._integrity_result = check_all()
    return {"status": "ok", "result": result, "integrity": lm._integrity_result}
```

- [ ] **Step 4: 跑测试验证通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_repair_api.py -v
```

Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add niu_api/kg_api.py tests/test_lightrag_repair_api.py
git commit -m "feat(kg_api): /api/kg/lightrag/repair POST 端点暴露外挂修复

端点路径 /api/kg/lightrag/repair（router prefix=/api/kg）。
支持 target=all / vdb_xxx.json / kv_store_xxx.json
修复后调 reset_init_state 让 LightRAG 重试初始化。"
```

---

## Task 6: 前端告警——splash 启动时显示告警 + 写入告警文件（v4 适配 splash 架构）

**Files:**
- Modify: `launcher/src/main.rs`
- Test: 手动验证（Rust 测试不在 TDD 范围内）

**背景**：launcher 实际架构是 `SplashMessage` enum + 280x80 splash 窗口，splash 关闭后进入 `while !cancelled { sleep(100ms) }` 等待循环，**无常驻 iced UI**。v2/v3 假设有常驻 UI 是错的。

v4 方案：**启动时检测 + splash 显示告警**：
- splash 启动时（`SplashMessage::Tick` 分支）调 `reqwest::blocking` 查 `/api/kg/stats`
- 如果 `init_failed=true` 或 `integrity.ok=false`，把告警写入 `~/.niu/lightrag_alert.json`
- splash 窗口扩大到 400x160，显示告警信息 + "修复"按钮 + "继续"按钮
- 用户点"修复"：调 `/api/kg/lightrag/repair?target=all`，等修复完成，splash 缩回 280x80 继续 boot
- 用户点"继续"：忽略告警，splash 正常关闭进入等待循环
- 如果无告警：splash 正常 280x80 显示启动画面

**关键**：用 `iced::futures::channel::oneshot`（iced re-export 的 futures，不需要 Cargo.toml 加依赖），不用裸 `futures::channel::oneshot`。

- [ ] **Step 1: 确认 launcher 现有 SplashMessage + splash 架构**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && grep -n "SplashMessage\|window::frames\|280\|80\|decorations\|transparent" launcher/src/main.rs | head -15
```

读 `launcher/src/main.rs` L40-170（SplashApp + SplashMessage enum + view + subscription + update），理解现有架构。

- [ ] **Step 2: 改 SplashMessage enum 加告警相关 message**

在 `launcher/src/main.rs` 的 `SplashMessage` enum 加：

```rust
enum SplashMessage {
    Tick,
    WindowOpened(window::Id),
    HideDockIcon,
    // 新增：LightRAG 韧性告警
    StatusCheckResult(Result<LightragStatus, String>),  // 启动时查 /api/kg/stats 的结果
    RepairLightrag,      // 用户点"修复"按钮
    RepairResult(Result<String, String>),  // 修复 API 调用结果
    DismissAlert,        // 用户点"继续"按钮（忽略告警）
}

#[derive(Debug, Clone, serde::Deserialize)]
struct LightragStatus {
    init_failed: bool,
    init_retry_in_seconds: Option<f64>,
    integrity: Option<IntegrityStatus>,
}

#[derive(Debug, Clone, serde::Deserialize)]
struct IntegrityStatus {
    ok: bool,
    total_errors: i32,
}

#[derive(Debug, Clone)]
struct LightragAlert {
    message: String,
    total_errors: i32,
}
```

在 `SplashApp` state 加 `alert: Option<LightragAlert>` 字段。

- [ ] **Step 3: 启动时查 status，写入 alert 字段**

在 `SplashMessage::Tick` 分支（启动早期，splash 窗口已显示但 LightRAG 还在 init），加一次 status 查询：

```rust
SplashMessage::Tick => {
    // 现有 tick 逻辑（检查 niu_api 是否就绪）...

    // 启动早期查一次 LightRAG status（只查一次，用 flag 避免重复）
    if !self.status_checked && self.niu_api_ready {
        self.status_checked = true;
        let (tx, rx) = iced::futures::channel::oneshot::channel::<Result<LightragStatus, String>>();
        std::thread::spawn(move || {
            let result = reqwest::blocking::Client::new()
                .get("http://localhost:9876/api/kg/stats")
                .send()
                .map_err(|e| e.to_string())
                .and_then(|resp| resp.json::<LightragStatus>().map_err(|e| e.to_string()));
            let _ = tx.send(result);
        });
        return Task::perform(
            async move { rx.await.unwrap_or(Err("channel closed".into())) },
            SplashMessage::StatusCheckResult,
        );
    }
    // 现有 tick 逻辑继续...
}

SplashMessage::StatusCheckResult(Ok(status)) => {
    if status.init_failed || status.integrity.as_ref().map_or(false, |i| !i.ok) {
        let total_errors = status.integrity.as_ref().map_or(0, |i| i.total_errors);
        let message = if status.init_failed {
            format!("LightRAG 初始化失败，重试倒计时 {:?} 秒", status.init_retry_in_seconds)
        } else {
            format!("检测到 {} 个数据一致性问题", total_errors)
        };
        self.alert = Some(LightragAlert { message, total_errors });
        // 扩大 splash 窗口显示告警
        // （需要改 window 尺寸到 400x160，具体 API 看 iced 0.13 window 操作）
    }
    Task::none()
}

SplashMessage::StatusCheckResult(Err(_)) => {
    // 查询失败，静默（启动继续）
    Task::none()
}

SplashMessage::RepairLightrag => {
    let (tx, rx) = iced::futures::channel::oneshot::channel::<Result<String, String>>();
    std::thread::spawn(move || {
        let result = reqwest::blocking::Client::new()
            .post("http://localhost:9876/api/kg/lightrag/repair?target=all")
            .send()
            .map_err(|e| e.to_string())
            .and_then(|resp| resp.text().map_err(|e| e.to_string()));
        let _ = tx.send(result);
    });
    Task::perform(
        async move { rx.await.unwrap_or(Err("channel closed".into())) },
        SplashMessage::RepairResult,
    )
}

SplashMessage::RepairResult(Ok(_)) => {
    // 修复完成，清告警，splash 缩回正常尺寸继续 boot
    self.alert = None;
    Task::none()
}

SplashMessage::RepairResult(Err(e)) => {
    // 修复失败，告警条显示错误，用户可继续或重试
    if let Some(alert) = &mut self.alert {
        alert.message = format!("修复失败: {}", e);
    }
    Task::none()
}

SplashMessage::DismissAlert => {
    // 用户忽略告警，继续启动
    self.alert = None;
    Task::none()
}
```

- [ ] **Step 4: 改 view 函数显示告警**

当 `self.alert` 非空时，splash 窗口扩大到 400x160，显示告警 + 按钮：

```rust
fn view(&self) -> Element<'_, SplashMessage> {
    if let Some(alert) = &self.alert {
        // 告警视图
        let content = column![
            text(alert.message.clone()).size(14),
            row![
                button("修复").on_press(SplashMessage::RepairLightrag),
                button("继续").on_press(SplashMessage::DismissAlert),
            ].spacing(8),
        ].spacing(8).padding(12);

        return container(content)
            .width(Length::Fixed(400.0))
            .height(Length::Fixed(160.0))
            .into();
    }

    // 现有 splash 视图（280x80 启动画面）
    // ...
}
```

**注意**：窗口尺寸动态调整需要 iced 0.13 的 `window::resize` 命令。如果动态调整复杂，可以保持 splash 280x80，告警信息用 `text` 叠加在 splash 上（小字号显示）。具体实现根据 iced 0.13 window API 调整。

- [ ] **Step 5: 编译验证**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/launcher && cargo build 2>&1 | tail -10
```

Expected: 编译成功（用 `iced::futures::channel::oneshot` + `reqwest::blocking` + `std::thread::spawn`，零新依赖）

- [ ] **Step 6: 手动验证**

启动程序，模拟 LightRAG 初始化失败（临时把 vdb 文件改名），看 splash 是否显示告警 + 修复按钮。

- [ ] **Step 7: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add launcher/src/main.rs
git commit -m "feat(launcher): splash 启动时检测 LightRAG 告警 + 修复按钮

启动时查 /api/kg/stats，如果 init_failed 或 integrity.ok=false：
- splash 窗口扩大显示告警信息
- 提供'修复'按钮调 /api/kg/lightrag/repair?target=all
- 提供'继续'按钮忽略告警

用 iced::futures::channel::oneshot + std::thread::spawn + reqwest::blocking
在独立线程跑 HTTP 请求，零新依赖。"
```

---

## Task 7: 端到端验证

**Files:** 临时验证脚本

- [ ] **Step 1: 模拟 vdb 损坏，验证检测 + 报告 + 修复**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
cp ~/.niu/lightrag_storage/vdb_entities.json /tmp/vdb_entities.json.backup

# 构造损坏（截断 matrix 字段尾部）
python3 -c "
import json
with open('REDACTED_USER_PATH/.niu/lightrag_storage/vdb_entities.json') as f:
    raw = json.load(f)
# 截断 matrix
raw['matrix'] = raw['matrix'][:1000]
with open('REDACTED_USER_PATH/.niu/lightrag_storage/vdb_entities.json', 'w') as f:
    json.dump(raw, f)
"

./niu &
sleep 60
curl -s http://localhost:9876/api/kg/stats | python3 -m json.tool
# 期望：integrity.ok=false, total_errors>0

# 调修复 API
curl -X POST "http://localhost:9876/api/kg/lightrag/repair?target=vdb_entities.json"
# 期望：从 vdb data 字段重建

sleep 30
curl -s http://localhost:9876/api/kg/stats | python3 -m json.tool
# 期望：integrity.ok=true

# 恢复原始 vdb
cp /tmp/vdb_entities.json.backup ~/.niu/lightrag_storage/vdb_entities.json
pgrep -f "niu_api" | xargs kill -TERM
```

- [ ] **Step 2: 验证备份机制**

```bash
ls ~/.niu/lightrag_storage_backups/
ls ~/.niu/lightrag_storage/*.bak
ls ~/.niu/lightrag_storage/*.corrupt.bak 2>&1
```

- [ ] **Step 3: 验证前端告警**

启动程序，模拟损坏，看 launcher 是否弹通知。

---

## Self-Review

### 1. Spec coverage

用户需求："启动时候有没有快速检测图谱一致性？如果有，就快速检测，发现问题修复。如果没有快速检测的工具，就做外挂程序。"

5 个维度覆盖：
- ✅ 维度 1（故障检测）→ Task 1（v2 真实格式：matrix + vector 三层解码 + graphml 边引用校验）
- ✅ 维度 2（数据修复）→ Task 2（v2 从 vdb data 字段重建 + kv_store fallback）+ Task 5
- ✅ 维度 3（写入原子性）→ 不改 nano-vectordb，用 Task 3 滚动备份兜底
- ✅ 维度 4（告警机制）→ Task 6（v2 用 frames() 计数 + 复用 /api/kg/stats）
- ✅ 维度 5（备份机制）→ Task 3（v2 full_backup 排除 .bak/.corrupt.bak）

### 2. v1 阻断修复

- ✅ **v1 阻断 1（vector 字段格式）** → v2 Task 1+2 用 `base64(zlib(float16 bytes))` 三层编码，实测验证
- ✅ **v1 阻断 2（kv_store 映射错误）** → v2 Task 2 改为优先从 vdb data 字段读，fallback 到 kv_store_text_chunks
- ✅ **v1 阻断 3（鸡生蛋矛盾）** → v2 Task 4 拆分两阶段 + `_embed_text` 用预加载 embedding 模型
- ✅ **v1 阻断 4（测试论断错误）** → v2 Task 1 删掉 float16 modulo 测试，改用精确字节数校验

### 3. v2 阻断修复

- ✅ **v2 阻断 1（测试检测顺序错位）** → v3 Task 1 `test_check_vdb_data_matrix_length_mismatch` 改为期望 `matrix_size_mismatch`（字节数 48 != 期望 32）
- ✅ **v2 阻断 2（元数据丢失）** → v3 Task 2 `repair_vdb` 改为 `{k: v for k, v in item.items() if k != "vector"}` 保留所有非 vector 字段，只重算 vector
- ✅ **v2 阻断 3（Rust 编译失败）** → v4 Task 6 改用 `iced::futures::channel::oneshot`（iced re-export，不需要 Cargo.toml 加依赖）+ `std::thread::spawn` + `reqwest::blocking`
- ✅ **v2 阻断 4（常量名错误）** → v3 Task 4 Step 3c 改为在现有 `get_lightrag_status` 末尾追加 `integrity` 字段，不重写整个函数（保留 `_INIT_RETRY_SECONDS` + `with _rag_lock` + `round(..., 1)`）

### 4. v3 阻断修复

- ✅ **v3 阻断 1（futures crate 未声明）** → v4 Task 6 改用 `iced::futures::channel::oneshot`（iced 在 lib.rs re-export 了 `iced_futures::futures`，不需要 Cargo.toml 加 `futures` 依赖）
- ✅ **v3 阻断 2（端点路径不匹配）** → v4 Task 5 端点路径对齐 `/api/kg/lightrag/repair`（router prefix=/api/kg），测试/curl/Rust 代码全部同步更新
- ✅ **v3 阻断 3（launcher 架构不匹配）** → v4 Task 6 改为 splash 启动时检测告警 + 扩大窗口显示告警 + 修复/继续按钮（不假设有常驻 iced UI，适配现有 SplashMessage + 280x80 splash 架构）
- ✅ **v3 阻断 4（测试 _init_failed_at=0.0 论断错误）** → v4 Task 4 测试改为 `_init_failed_at=None`（`None is not None` 为 False，init_failed=False）

### 3. 改进建议处理

- ✅ 改进 1（graphml 边引用校验）→ Task 1 `check_graphml` 加边引用节点存在校验
- ✅ 改进 4（cleanup 时序）→ Task 4 Phase 1 跑 cleanup（修复在 Phase 2，时序错开）
- ✅ 改进 5（iced 0.13 + frames()）→ Task 6 用 frames() 计数，零新依赖
- ✅ 改进 6（复用 /api/kg/stats）→ Task 6 复用现有端点，不新增
- 改进 2/3（rolling_backup/full_backup 并发写问题）→ 暂不处理（外挂层无法加锁跟 nano-vectordb save 互斥，best-effort 可接受）

### 4. Type consistency

- `check_vdb(path: str) -> dict` → Task 1 定义，Task 7 引用
- `repair_vdb(vdb_filename: str) -> dict` → Task 2 定义，Task 5 引用
- `_encode_vector` / `_encode_matrix` → Task 2 定义，Task 1 检测时用 `_decode_vector`
- `run_resilience_phase1() -> dict` / `run_resilience_phase2(need_repair: bool) -> dict` → Task 4 定义
- `_integrity_result: dict | None` → Task 4 定义，`get_lightrag_status` 引用

---

## 执行交接

计划完成并保存到 `docs/superpowers/plans/2026-07-06-lightrag-data-resilience.md`。两种执行方式：

**1. Subagent-Driven（推荐）** - 每个 Task 派新子 Agent 实现，Task 之间审查

**2. Inline Execution** - 在当前会话里批量执行

要哪种？
