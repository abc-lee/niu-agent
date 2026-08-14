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
    _repair_vdb_matrix_inplace,
    auto_repair_vdb_matrices,
    check_all,
)

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


# ---------- 修复：从 data.vector 重建 matrix ----------

def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def test_repair_rebuilds_matrix_from_data(storage_dir):
    """不一致（3 data + 5 matrix 行）→ 修复 → matrix 行数 == 3 且内容与 data.vector 归一化一致。
    vec 用非整数（float16 对整数精确表示 → 量化误差 0，容差论证需真实误差触发——R6-B P3 修正）。"""
    rng = np.random.default_rng(42)
    vecs = [rng.random(DIM).astype(np.float32) + i * 0.001 for i in range(3)]
    entries = [_make_entry(f"e{i}", v.tolist()) for i, v in enumerate(vecs)]
    path = storage_dir / "vdb_entities.json"
    _write_vdb(path, entries, _make_matrix(5, seed=4))
    r = _repair_vdb_matrix_inplace(path)
    assert r["status"] == "ok"
    assert r["data_count"] == 3
    assert r["matrix_rows"] == 3
    # 重读文件验证 matrix 内容
    data = json.loads(path.read_text(encoding="utf-8"))
    mat = np.frombuffer(base64.b64decode(data["matrix"]), dtype=np.float32).reshape(-1, DIM)
    assert mat.shape[0] == 3
    for i, v in enumerate(vecs):
        expected = _normalize(v)
        assert np.abs(mat[i] - expected).max() < 1e-3  # float16 量化误差 ~5e-4


def test_repair_keeps_other_fields(storage_dir):
    """修复只重写 matrix，保留 data/embedding_dim 等字段。"""
    vecs = [np.linspace(1, 768, DIM).astype(np.float32) for _ in range(2)]
    entries = [_make_entry(f"e{i}", v.tolist()) for i, v in enumerate(vecs)]
    path = storage_dir / "vdb_entities.json"
    _write_vdb(path, entries, _make_matrix(3, seed=5))
    before = json.loads(path.read_text(encoding="utf-8"))
    _repair_vdb_matrix_inplace(path)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["embedding_dim"] == before["embedding_dim"] == DIM
    assert after["data"] == before["data"]  # data 原样保留
    assert after["matrix"] != before["matrix"]


def test_repair_bad_vector_no_writeback(storage_dir):
    """任一条 vector 解码失败 → status=error 且文件内容不变（data 损坏需走全量重建）。"""
    entries = [_make_entry("e0", [0.1] * DIM), {"__id__": "ent-bad", "entity_name": "bad", "vector": "not-valid-base64!!"}]
    path = storage_dir / "vdb_entities.json"
    _write_vdb(path, entries, _make_matrix(3, seed=6))
    before = path.read_text(encoding="utf-8")
    r = _repair_vdb_matrix_inplace(path)
    assert r["status"] == "error"
    assert "解码失败" in r["message"]
    assert path.read_text(encoding="utf-8") == before  # 未写回


def test_repair_nan_vector_no_writeback(storage_dir):
    """含 NaN 的 vector（float16 可表示 NaN）→ norm=NaN 绕过 norm<=0 守卫 → 判坏
    status=error 且文件内容不变（P3：np.linalg.norm 对 NaN 分量返回 NaN，NaN<=0 为
    False——原守卫放行 NaN 行写入"已修复" matrix）。"""
    vec = [0.1] * DIM
    vec[0] = float("nan")
    entries = [_make_entry("e0", [0.1] * DIM), _make_entry("e_nan", vec)]
    path = storage_dir / "vdb_entities.json"
    _write_vdb(path, entries, _make_matrix(3, seed=6))
    before = path.read_text(encoding="utf-8")
    r = _repair_vdb_matrix_inplace(path)
    assert r["status"] == "error"
    assert "解码失败" in r["message"]
    assert path.read_text(encoding="utf-8") == before  # 未写回


def test_repair_missing_matrix_field_ok(storage_dir):
    """无 matrix 键（旧格式）→ status=ok——本函数对旧格式会实际重建并写回 matrix
    （entries 非空逐条解码重建）；真正跳过旧格式的是 auto_repair_vdb_matrices
    编排层的 matrix_rows is None 门控（R4-P3 docstring 修正）。"""
    entries = [_make_entry(f"e{i}", [0.1] * DIM) for i in range(2)]
    path = storage_dir / "vdb_entities.json"
    _write_vdb(path, entries, matrix=None)
    r = _repair_vdb_matrix_inplace(path)
    assert r["status"] == "ok"
    # 重建后 matrix 写回且一致
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("matrix")
    mat = np.frombuffer(base64.b64decode(data["matrix"]), dtype=np.float32).reshape(-1, DIM)
    assert mat.shape[0] == 2


def test_auto_repair_both_files(storage_dir):
    """auto_repair_vdb_matrices：entities 坏 + relationships 好 → repaired 1 + errors 0。"""
    bad = [_make_entry(f"e{i}", [0.1] * DIM) for i in range(3)]
    _write_vdb(storage_dir / "vdb_entities.json", bad, _make_matrix(5, seed=7))
    good = [_make_entry(f"r{i}", [0.1] * DIM) for i in range(2)]
    _write_vdb(storage_dir / "vdb_relationships.json", good, _make_matrix(2, seed=8))
    r = auto_repair_vdb_matrices()
    assert len(r["repaired"]) == 1
    assert r["repaired"][0]["target_file"] == "vdb_entities.json"
    assert r["errors"] == []
    # 修复后检测通过
    assert _check_vdb_internal(storage_dir) == []


def test_auto_repair_skips_consistent_file(storage_dir):
    """健康文件（matrix/data 一致）不被重建——mismatch 门控（R1-P1 修正）。"""
    good = [_make_entry(f"e{i}", [0.1] * DIM) for i in range(2)]
    _write_vdb(storage_dir / "vdb_entities.json", good, _make_matrix(2, seed=9))
    r = auto_repair_vdb_matrices()
    assert r["repaired"] == []
    assert r["errors"] == []


def test_auto_repair_missing_file_skipped(storage_dir):
    """不存在文件跳过——不产生 error（R1-P3 修正）。"""
    bad = [_make_entry(f"e{i}", [0.1] * DIM) for i in range(3)]
    _write_vdb(storage_dir / "vdb_entities.json", bad, _make_matrix(5, seed=10))
    # 只写 entities，不写 relationships
    r = auto_repair_vdb_matrices()
    assert len(r["repaired"]) == 1
    assert r["repaired"][0]["target_file"] == "vdb_entities.json"
    assert r["errors"] == []


def test_check_internal_matrix_format_error(storage_dir):
    """matrix 字节数不能被 4*embedding_dim 整除 → major vdb_matrix_format（R1-P3 修正）。"""
    entries = [_make_entry(f"e{i}", [0.1] * DIM) for i in range(2)]
    path = storage_dir / "vdb_entities.json"
    # 构造 2 条 + 1 个额外 float32（= 768*2+1 个 float 字节，不可整除）
    bad_matrix = np.array([0.1] * (DIM * 2 + 1), dtype=np.float32).tobytes()
    payload = {"embedding_dim": DIM, "data": entries,
               "matrix": base64.b64encode(bad_matrix).decode()}
    path.write_text(json.dumps(payload))
    errors = _check_vdb_internal(storage_dir)
    assert len(errors) == 1
    assert errors[0]["check"] == "vdb_matrix_format"
    assert errors[0]["severity"] == "major"


def test_repair_atomic_write_no_tmp_leftover(storage_dir):
    """修复成功写回后无 .tmp 临时文件残留（原子写路径，R2-P3 修正）。"""
    vecs = [np.linspace(1, 768, DIM).astype(np.float32) for _ in range(2)]
    entries = [_make_entry(f"e{i}", v.tolist()) for i, v in enumerate(vecs)]
    path = storage_dir / "vdb_entities.json"
    _write_vdb(path, entries, _make_matrix(3, seed=11))
    _repair_vdb_matrix_inplace(path)
    assert not (storage_dir / "vdb_entities.json.tmp").exists()


def test_repair_empty_data_clears_matrix(storage_dir):
    """data 空 + matrix 非空（孤儿 matrix）→ 修复清空 matrix 为 (0, dim)（R2-P3 修正）。"""
    path = storage_dir / "vdb_entities.json"
    payload = {"embedding_dim": DIM, "data": [], "matrix": _encode_matrix(_make_matrix(3, seed=12))}
    path.write_text(json.dumps(payload))
    r = _repair_vdb_matrix_inplace(path)
    assert r["status"] == "ok"
    assert r["data_count"] == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    mat = np.frombuffer(base64.b64decode(data["matrix"]), dtype=np.float32).reshape(-1, DIM)
    assert mat.shape[0] == 0


# ---------- 启动接线：run_resilience_phase1 自动修复 ----------

def test_run_resilience_phase1_auto_repairs(storage_dir, monkeypatch):
    """check_all 首次报 vdb_matrix_mismatch → auto_repair 被调 → 重跑 check_all 通过 → check_ok=True。"""
    import niu_api.internal.lightrag_manager as lm
    from niu_api.internal import lightrag_integrity

    mismatch_result = {
        "ok": False, "critical_errors": 0, "major_errors": 1, "minor_errors": 0,
        "errors": [{
            "check": "vdb_matrix_mismatch", "severity": "major",
            "target_file": "vdb_entities.json", "matrix_rows": 5, "data_count": 3,
            "msg": "vdb_entities.json matrix 行数(5) != data 条数(3)",
        }],
        "checks": {"vdb_internal": {"name": "vdb_internal", "errors": [{}]}},
    }
    clean_result = {
        "ok": True, "critical_errors": 0, "major_errors": 0, "minor_errors": 0,
        "errors": [], "checks": {"vdb_internal": {"name": "vdb_internal", "errors": []}},
    }
    calls = {"n": 0}

    def fake_check_all():
        calls["n"] += 1
        return mismatch_result if calls["n"] == 1 else clean_result

    repaired = {"repaired": [{"status": "ok", "target_file": "vdb_entities.json"}], "errors": []}
    repair_calls = {"n": 0}

    def fake_repair():
        repair_calls["n"] += 1
        return repaired

    monkeypatch.setattr(lightrag_integrity, "check_all", fake_check_all)
    monkeypatch.setattr(lightrag_integrity, "auto_repair_vdb_matrices", fake_repair)

    result = lm.run_resilience_phase1()
    assert result["check_ok"] is True
    assert result["need_repair"] is False
    assert calls["n"] == 2  # 检测 + 修复后重检
    assert repair_calls["n"] == 1  # auto_repair 恰被调一次（R8-P3 补强：防"丢掉调用只重跑"的回归实现）


def test_run_resilience_phase1_repair_failure_keeps_mismatch(storage_dir, monkeypatch):
    """auto_repair 抛异常 → 保留 mismatch 结果（need_repair=True，走 rfd 弹窗兜底）。"""
    import niu_api.internal.lightrag_manager as lm
    from niu_api.internal import lightrag_integrity

    mismatch_result = {
        "ok": False, "critical_errors": 0, "major_errors": 1, "minor_errors": 0,
        "errors": [{"check": "vdb_matrix_mismatch", "severity": "major", "target_file": "vdb_entities.json",
                    "matrix_rows": 5, "data_count": 3, "msg": "mismatch"}],
        "checks": {"vdb_internal": {"name": "vdb_internal", "errors": [{}]}},
    }
    calls = {"n": 0}

    def fake_check_all():
        calls["n"] += 1
        return mismatch_result

    monkeypatch.setattr(lightrag_integrity, "check_all", fake_check_all)
    monkeypatch.setattr(lightrag_integrity, "auto_repair_vdb_matrices",
                        lambda: (_ for _ in ()).throw(RuntimeError("repair boom")))

    result = lm.run_resilience_phase1()
    assert result["need_repair"] is True
    assert calls["n"] == 1  # 修复失败不重跑（保留原检测结果）


def test_run_resilience_phase1_no_mismatch_no_repair(storage_dir, monkeypatch):
    """无 mismatch → auto_repair 不被调用。"""
    import niu_api.internal.lightrag_manager as lm
    from niu_api.internal import lightrag_integrity

    clean_result = {
        "ok": True, "critical_errors": 0, "major_errors": 0, "minor_errors": 0,
        "errors": [], "checks": {},
    }
    called = {"n": 0}

    def fake_check_all():
        return clean_result

    def fake_repair():
        called["n"] += 1
        return {"repaired": [], "errors": []}

    monkeypatch.setattr(lightrag_integrity, "check_all", fake_check_all)
    monkeypatch.setattr(lightrag_integrity, "auto_repair_vdb_matrices", fake_repair)

    result = lm.run_resilience_phase1()
    assert result["check_ok"] is True
    assert called["n"] == 0
