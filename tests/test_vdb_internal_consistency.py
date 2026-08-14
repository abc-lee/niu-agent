"""VDB 内部一致性（matrix/data 行数）检测 + 自动修复测试。

背景：vdb_*.json 的 matrix 行数 > data 条数（孤儿向量）时，
nano-vectordb _cosine_query 的 filter_index[sort_index] 越界崩溃
（2026-08-14 实证：index 3225 is out of bounds for size 3225）。
既有 _check_vdb_missing 只查 GraphML⊆vdb 单向，读不到 matrix——本测试
覆盖新检测 _check_vdb_internal 与外科修复 _repair_vdb_matrix_inplace。
"""
import base64
import json
import zlib
from pathlib import Path

import numpy as np
import pytest

from niu_api.internal.lightrag_integrity import (
    _check_vdb_internal,
    check_all,
)

# 注：_repair_vdb_matrix_inplace / auto_repair_vdb_matrices 在 Task 2 实现后
#     追加 import（Task 1 绿相不可依赖未实现函数——R1-P1 修正）

DIM = 768


def _encode_vector(vec: list[float]) -> str:
    """data[].vector 真实压缩格式：base64(zlib(float16))。"""
    raw = np.array(vec, dtype=np.float32).astype(np.float16).tobytes()
    return base64.b64encode(zlib.compress(raw)).decode()


def _encode_matrix(mat: np.ndarray) -> str:
    """matrix 真实格式：base64(float32 raw bytes)。"""
    return base64.b64encode(np.array(mat, dtype=np.float32).tobytes()).decode()


def _make_entry(name: str, vec: list[float]) -> dict:
    return {
        "__id__": f"ent-{abs(hash(name)) & 0xFFFFFFFF:08x}",
        "__created_at__": 0,
        "entity_name": name,
        "source_id": "test",
        "file_path": "test.md",
        "vector": _encode_vector(vec),
    }


def _write_vdb(path: Path, entries: list[dict], matrix: np.ndarray | None = None) -> None:
    """写完整 vdb 文件（含 embedding_dim + matrix）。matrix=None 模拟旧格式（无 matrix 字段）。"""
    payload: dict = {"embedding_dim": DIM, "data": entries}
    if matrix is not None:
        payload["matrix"] = _encode_matrix(matrix)
    path.write_text(json.dumps(payload, ensure_ascii=False))


def _make_matrix(rows: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.random((rows, DIM)).astype(np.float32)


@pytest.fixture
def storage_dir(tmp_path, monkeypatch):
    from niu_api.internal import lightrag_integrity
    monkeypatch.setattr(lightrag_integrity, "_STORAGE_DIR", tmp_path)
    monkeypatch.setattr(lightrag_integrity, "_resolve_storage_dir", lambda: tmp_path)
    return tmp_path


# ---------- 检测：matrix 行数 vs data 条数 ----------

def test_check_internal_entities_matrix_more_rows(storage_dir):
    """entities：matrix 5 行 vs data 3 条（2 个孤儿向量）→ major vdb_matrix_mismatch。"""
    entries = [_make_entry(f"e{i}", [0.1] * DIM) for i in range(3)]
    _write_vdb(storage_dir / "vdb_entities.json", entries, _make_matrix(5, seed=1))
    errors = _check_vdb_internal(storage_dir)
    assert len(errors) == 1
    assert errors[0]["check"] == "vdb_matrix_mismatch"
    assert errors[0]["severity"] == "major"
    assert errors[0]["target_file"] == "vdb_entities.json"
    assert errors[0]["matrix_rows"] == 5
    assert errors[0]["data_count"] == 3


def test_check_internal_entities_matrix_fewer_rows(storage_dir):
    """entities：matrix 2 行 vs data 3 条 → 同样报（反向不一致）。"""
    entries = [_make_entry(f"e{i}", [0.1] * DIM) for i in range(3)]
    _write_vdb(storage_dir / "vdb_entities.json", entries, _make_matrix(2, seed=1))
    errors = _check_vdb_internal(storage_dir)
    assert len(errors) == 1
    assert errors[0]["check"] == "vdb_matrix_mismatch"
    assert errors[0]["matrix_rows"] == 2
    assert errors[0]["data_count"] == 3


def test_check_internal_consistent_no_error(storage_dir):
    """一致（3/3）→ 无错误。"""
    entries = [_make_entry(f"e{i}", [0.1] * DIM) for i in range(3)]
    _write_vdb(storage_dir / "vdb_entities.json", entries, _make_matrix(3, seed=1))
    assert _check_vdb_internal(storage_dir) == []


def test_check_internal_no_matrix_field_skipped(storage_dir):
    """无 matrix 字段（旧格式）→ 跳过不误报。"""
    entries = [_make_entry(f"e{i}", [0.1] * DIM) for i in range(3)]
    _write_vdb(storage_dir / "vdb_entities.json", entries, matrix=None)
    assert _check_vdb_internal(storage_dir) == []


def test_check_internal_relationships_mismatch(storage_dir):
    """relationships 同样检测。"""
    entries = [_make_entry(f"e{i}", [0.1] * DIM) for i in range(2)]
    _write_vdb(storage_dir / "vdb_relationships.json", entries, _make_matrix(4, seed=2))
    errors = _check_vdb_internal(storage_dir)
    assert len(errors) == 1
    assert errors[0]["target_file"] == "vdb_relationships.json"
    assert errors[0]["matrix_rows"] == 4


def test_check_internal_chunks_mismatch(storage_dir):
    """chunks 同样检测（R3-P1 修正：LightRAG local 查询会检索 chunks_vdb）。"""
    entries = [_make_entry(f"c{i}", [0.1] * DIM) for i in range(3)]
    _write_vdb(storage_dir / "vdb_chunks.json", entries, _make_matrix(5, seed=13))
    errors = _check_vdb_internal(storage_dir)
    assert len(errors) == 1
    assert errors[0]["target_file"] == "vdb_chunks.json"
    assert errors[0]["matrix_rows"] == 5
    assert errors[0]["data_count"] == 3


def test_check_internal_empty_matrix_field_mismatch(storage_dir):
    """matrix 键存在但为空串（0 行）+ 非空 data → mismatch（R3-P3 盲区钉住，R4-P3 补测）。"""
    entries = [_make_entry(f"e{i}", [0.1] * DIM) for i in range(3)]
    path = storage_dir / "vdb_entities.json"
    path.write_text(json.dumps({"embedding_dim": DIM, "data": entries, "matrix": ""}))
    errors = _check_vdb_internal(storage_dir)
    assert len(errors) == 1
    assert errors[0]["check"] == "vdb_matrix_mismatch"
    assert errors[0]["matrix_rows"] == 0
    assert errors[0]["data_count"] == 3


def test_check_all_includes_vdb_internal(storage_dir):
    """check_all 集成：不一致 → major_errors ≥ 1 + errors 含 vdb_matrix_mismatch + checks.vdb_internal。"""
    entries = [_make_entry(f"e{i}", [0.1] * DIM) for i in range(3)]
    _write_vdb(storage_dir / "vdb_entities.json", entries, _make_matrix(5, seed=3))
    result = check_all()
    assert result["ok"] is False
    assert result["major_errors"] >= 1
    mismatch = [e for e in result["errors"] if e.get("check") == "vdb_matrix_mismatch"]
    assert len(mismatch) == 1
    assert "vdb_internal" in result["checks"]
    assert result["checks"]["vdb_internal"]["errors"] == mismatch


def test_check_all_consistent_ok(storage_dir):
    """check_all 集成：一致 → 无 vdb_matrix_mismatch。"""
    entries = [_make_entry(f"e{i}", [0.1] * DIM) for i in range(3)]
    _write_vdb(storage_dir / "vdb_entities.json", entries, _make_matrix(3, seed=3))
    result = check_all()
    mismatch = [e for e in result["errors"] if e.get("check") == "vdb_matrix_mismatch"]
    assert mismatch == []
