"""repair_relationship_sync 测试 — 修 vdb_relationships 的 src_id/tgt_id 大写 + 重算 __id__。

关键：LightRAG 写入关系 id 时会先 sorted((src, tgt)) 再 compute_mdhash_id(sorted_src + sorted_tgt, prefix='rel-')。
repair 必须对齐这个逻辑，否则修复后的 id 跟新写入 id 不一致。
"""
import base64
import json
import tempfile
import zlib
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from lightrag.utils import compute_mdhash_id


def _encode_vector_768(vec_f16) -> str:
    arr = np.array(vec_f16, dtype=np.float16) if not hasattr(vec_f16, 'astype') else vec_f16.astype(np.float16)
    return base64.b64encode(zlib.compress(arr.tobytes())).decode()


def _encode_matrix_768(matrix_f32) -> str:
    arr = np.array(matrix_f32, dtype=np.float32) if not hasattr(matrix_f32, 'astype') else matrix_f32.astype(np.float32)
    return base64.b64encode(arr.tobytes()).decode()


def _write_rel_vdb(path: Path, data_list: list[dict], embedding_dim: int = 768):
    vectors = []
    for item in data_list:
        vec = np.full(embedding_dim, 0.1, dtype=np.float16)
        item = {**item, "vector": _encode_vector_768(vec)}
        vectors.append(vec)
    matrix = np.array(vectors, dtype=np.float32) if vectors else np.zeros((0, embedding_dim), dtype=np.float32)
    path.write_text(json.dumps({
        "embedding_dim": embedding_dim,
        "data": data_list,
        "matrix": _encode_matrix_768(matrix),
    }))


def _write_graphml_with_edges(path: Path, edges: list[tuple[str, str]]):
    """写含边的 GraphML，edges: [(src, tgt), ...]，src/tgt 已 lower。"""
    nodes = set()
    for src, tgt in edges:
        nodes.add(src)
        nodes.add(tgt)
    nodes_xml = "".join(
        f'<node id="{n}"><data key="d0">{n}</data><data key="d2">desc</data></node>'
        for n in nodes
    )
    edges_xml = "".join(
        f'<edge source="{src}" target="{tgt}"/>'
        for src, tgt in edges
    )
    path.write_text(
        f'<?xml version="1.0"?><graphml xmlns="http://graphml.graphdrawing.org/xmlns">'
        f'<key id="d0" for="node" attr.name="entity_id" attr.type="string"/>'
        f'<key id="d2" for="node" attr.name="description" attr.type="string"/>'
        f'<graph>{nodes_xml}{edges_xml}</graph></graphml>'
    )


def test_repair_relationship_sync_src_tgt_lowered_and_sorted(monkeypatch):
    """vdb_relationships 的 src_id='Banana' tgt_id='Apple'（src>tgt 顺序）→ 修复后 lower + sorted + 重算 __id__。

    LightRAG 写入时 sorted → src='apple' tgt='banana' → id=compute_mdhash_id('apple'+'banana', prefix='rel-')。
    repair 必须对齐：lower + sorted 后 id=compute_mdhash_id('apple'+'banana', prefix='rel-')。
    如果 repair 只 lower 不 sort，会算成 compute_mdhash_id('banana'+'apple')，跟新写入 id 不一致。
    """
    from niu_api.internal import lightrag_integrity, lightrag_repair
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        # 存量关系：大写 src/tgt（src>tgt 顺序），__id__ 用大写算
        old_id = compute_mdhash_id("Banana" + "Apple", prefix="rel-")
        _write_rel_vdb(storage / "vdb_relationships.json", [
            {"__id__": old_id, "src_id": "Banana", "tgt_id": "Apple", "content": "rel desc"},
        ])
        # GraphML 边是 lower + sorted
        _write_graphml_with_edges(storage / "graph_chunk_entity_relation.graphml", [
            ("apple", "banana"),
        ])
        monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", storage)
        monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))

        result = lightrag_repair.repair_relationship_sync()

        assert result["status"] == "ok"
        vdb = json.loads((storage / "vdb_relationships.json").read_text())
        # src_id/tgt_id 应 lower + sorted（src='apple', tgt='banana'）
        assert vdb["data"][0]["src_id"] == "apple"
        assert vdb["data"][0]["tgt_id"] == "banana"
        # __id__ 应重算为 sorted 后的 lower 化 src+tgt 的 hash
        expected_id = compute_mdhash_id("apple" + "banana", prefix="rel-")
        assert vdb["data"][0]["__id__"] == expected_id, f"__id__ 应是 {expected_id}，实际: {vdb['data'][0]['__id__']}"
        assert vdb["data"][0]["__id__"].startswith("rel-")


def test_repair_relationship_sync_content_preserved(monkeypatch):
    """content/description/keywords 字段保留原样不 lower（自然语言）。"""
    from niu_api.internal import lightrag_integrity, lightrag_repair
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_rel_vdb(storage / "vdb_relationships.json", [
            {"__id__": compute_mdhash_id("Apple" + "Banana", prefix="rel-"),
             "src_id": "Apple", "tgt_id": "Banana",
             "content": "Apple founded Banana", "description": "Apple founded Banana",
             "keywords": "founder"},
        ])
        _write_graphml_with_edges(storage / "graph_chunk_entity_relation.graphml", [
            ("apple", "banana"),
        ])
        monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", storage)
        monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))

        result = lightrag_repair.repair_relationship_sync()

        assert result["status"] == "ok"
        vdb = json.loads((storage / "vdb_relationships.json").read_text())
        # content/description/keywords 保留原样
        assert vdb["data"][0]["content"] == "Apple founded Banana"
        assert vdb["data"][0]["description"] == "Apple founded Banana"


def test_repair_relationship_sync_orphan_edge_deleted(monkeypatch):
    """vdb 关系的 src/tgt 在 GraphML 没有对应边 → 删除（真孤儿关系）。"""
    from niu_api.internal import lightrag_integrity, lightrag_repair
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_rel_vdb(storage / "vdb_relationships.json", [
            {"__id__": compute_mdhash_id("keep_src" + "keep_tgt", prefix="rel-"),
             "src_id": "keep_src", "tgt_id": "keep_tgt", "content": "keep"},
            {"__id__": compute_mdhash_id("orphan_src" + "orphan_tgt", prefix="rel-"),
             "src_id": "orphan_src", "tgt_id": "orphan_tgt", "content": "orphan"},
        ])
        _write_graphml_with_edges(storage / "graph_chunk_entity_relation.graphml", [
            ("keep_src", "keep_tgt"),
        ])
        monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", storage)
        monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))

        result = lightrag_repair.repair_relationship_sync()

        assert result["status"] == "ok"
        assert result["removed"] == 1
        vdb = json.loads((storage / "vdb_relationships.json").read_text())
        names = [(d["src_id"], d["tgt_id"]) for d in vdb["data"]]
        assert ("keep_src", "keep_tgt") in names
        assert ("orphan_src", "orphan_tgt") not in names


def test_repair_relationship_sync_self_loop_dropped(monkeypatch):
    """vdb 关系 lower 后 src==tgt（自环）→ GraphML 无对应边（LightRAG 不写自环边）→ 删除。

    这是 LightRAG 既有语义（_merge_edges_then_upsert L2024 if src_id == tgt_id: return None）。
    """
    from niu_api.internal import lightrag_integrity, lightrag_repair
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        _write_rel_vdb(storage / "vdb_relationships.json", [
            {"__id__": compute_mdhash_id("Apple" + "apple", prefix="rel-"),
             "src_id": "Apple", "tgt_id": "apple", "content": "self loop"},
        ])
        # GraphML 不写自环边
        _write_graphml_with_edges(storage / "graph_chunk_entity_relation.graphml", [])
        monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", storage)
        monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage))

        result = lightrag_repair.repair_relationship_sync()

        assert result["status"] == "error"  # 修复后无数据
        assert result["removed"] == 1
