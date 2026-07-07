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
