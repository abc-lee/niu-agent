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
