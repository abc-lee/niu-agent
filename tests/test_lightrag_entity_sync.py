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
        f'<data key="d0">{nid}</data>'
        f'<data key="d1">entity_type</data>'
        f'<data key="d2">{desc}</data>'
        f'<data key="d3">{src}</data>'
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
        assert not report["ok"], f"大写 entity_name 应触发 ok=False，实际 ok={report['ok']}"
        case_errors = [e for e in report["errors"] if e.get("check") == "case_mismatch"]
        assert len(case_errors) == 2, f"应有 2 个 case_mismatch error，实际 {len(case_errors)}"
        assert report["stats"]["case_mismatch"] == 2
        assert report["stats"]["orphan_in_vdb"] == 0
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
