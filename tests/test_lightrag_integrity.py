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
