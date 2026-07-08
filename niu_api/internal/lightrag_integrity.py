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


def _resolve_storage_dir() -> Path:
    """返回 _STORAGE_DIR 的 Path 形式（兼容 monkeypatch 注入 str 的场景）。"""
    return Path(_STORAGE_DIR)


def _decode_vector(vec_b64: str, embedding_dim: int) -> "np.ndarray":
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


def check_all_vdbs() -> dict[str, Any]:
    """检测所有 vdb 文件（不含 kv_store/graphml）。"""
    reports: dict[str, Any] = {}
    all_ok = True
    for fname in _VDB_FILES:
        r = check_vdb(str(_resolve_storage_dir() / fname))
        reports[fname] = r
        if not r["ok"]:
            all_ok = False
    return {"ok": all_ok, "files": reports}
