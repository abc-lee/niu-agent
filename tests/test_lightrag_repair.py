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
    # 注意：vdb_relationships 的 data 需要有 src_id/tgt_id 才能通过 repair_relationship_sync
    for fname in ["vdb_entities.json", "vdb_relationships.json", "vdb_chunks.json"]:
        matrix_f32 = np.random.rand(2, 4).astype(np.float32)
        if fname == "vdb_relationships.json":
            data_list = [
                {"__id__": "e1", "content": f"{fname} desc1", "vector": _encode_vector(matrix_f32[0].astype(np.float16)),
                 "src_id": "e1", "tgt_id": "e2"},
                {"__id__": "e2", "content": f"{fname} desc2", "vector": _encode_vector(matrix_f32[1].astype(np.float16)),
                 "src_id": "e1", "tgt_id": "e2"},
            ]
        else:
            data_list = [
                {"__id__": "e1", "content": f"{fname} desc1", "vector": _encode_vector(matrix_f32[0].astype(np.float16))},
                {"__id__": "e2", "content": f"{fname} desc2", "vector": _encode_vector(matrix_f32[1].astype(np.float16))},
            ]
        vdb = {
            "embedding_dim": 4,
            "data": data_list,
            "matrix": "truncated",
        }
        (storage_dir / fname).write_text(json.dumps(vdb, ensure_ascii=False))

    monkeypatch.setattr(lightrag_repair, "_STORAGE_DIR", str(storage_dir))
    monkeypatch.setattr(lightrag_repair, "_embed_text", lambda x: [0.1, 0.2, 0.3, 0.4])

    # repair_all 现在会调用 repair_entity_sync + repair_relationship_sync，需要 GraphML 文件存在。
    # 写一个含 e1/e2 节点 + e1->e2 边的 GraphML：
    # - repair_entity_sync 匹配 vdb_entities 的 e1/e2 节点，避免被当孤儿删除
    # - repair_relationship_sync 匹配 vdb_relationships 的 e1->e2 边，避免被当孤儿删除
    from tests.test_lightrag_entity_sync import _write_graphml
    _write_graphml(storage_dir / "graph_chunk_entity_relation.graphml", [
        ("e1", "desc e1", "chunk-1"),
        ("e2", "desc e2", "chunk-2"),
    ])
    # 补一条 e1->e2 边（_write_graphml 不写边，用 ET 追加）
    import xml.etree.ElementTree as ET
    graphml_path = storage_dir / "graph_chunk_entity_relation.graphml"
    tree = ET.parse(graphml_path)
    root = tree.getroot()
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    graph = root.find(f"{ns}graph")
    edge = ET.SubElement(graph, f"{ns}edge")
    edge.set("source", "e1")
    edge.set("target", "e2")
    tree.write(graphml_path)

    result = lightrag_repair.repair_all()
    assert all(r["status"] == "ok" for r in result.values())
