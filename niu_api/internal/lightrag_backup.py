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


def _resolve_storage_dir() -> Path:
    """返回 _STORAGE_DIR 的 Path 形式（兼容 monkeypatch 注入 str 的场景）。"""
    return Path(_STORAGE_DIR)


def _resolve_backups_dir() -> Path:
    """返回 _BACKUPS_DIR 的 Path 形式（兼容 monkeypatch 注入 str 的场景）。"""
    return Path(_BACKUPS_DIR)


def _is_excluded(filename: str) -> bool:
    return any(filename.endswith(suffix) for suffix in _EXCLUDE_SUFFIXES)


def rolling_backup(filename: str) -> bool:
    """把 _STORAGE_DIR/filename 复制到 filename.bak（覆盖已有 .bak）。

    Returns:
        True 如果备份成功，False 如果原文件不存在或复制失败。
    """
    storage_dir = _resolve_storage_dir()
    src = storage_dir / filename
    if not src.exists():
        return False
    dst = storage_dir / f"{filename}.bak"
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
    storage_dir = _resolve_storage_dir()
    backups_dir = _resolve_backups_dir()
    if not storage_dir.exists():
        return None
    backups_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = backups_dir / timestamp
    try:
        backup_dir.mkdir()
        # 手动复制，排除 .bak/.corrupt.bak/.tmp
        for item in storage_dir.iterdir():
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
    backups_dir = _resolve_backups_dir()
    if not backups_dir.exists():
        return 0
    subdirs = sorted(
        [p for p in backups_dir.iterdir() if p.is_dir()],
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
    storage_dir = _resolve_storage_dir()
    if not storage_dir.exists():
        return 0
    removed = 0
    for p in storage_dir.glob("*.corrupt.bak"):
        try:
            p.unlink()
            removed += 1
            logger.info(f"[LightRAGBackup] 清理残留: {p}")
        except Exception as e:
            logger.warning(f"[LightRAGBackup] 清理失败 {p}: {e}")
    return removed
